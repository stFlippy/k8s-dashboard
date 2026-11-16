"""Kubernetes REST operations through the corporate kubectl proxy."""

from __future__ import annotations

import json
import copy
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .kubectl import ProxyManager
from .storage import UserStore
from .metrics import ThrottleSamples, cpu_cores, memory_bytes
from .authorization import ClusterAuthorization, AuthorizationRequired
from .safety import check_identity, validate_resources
from .cache import SnapshotCache
from .access import is_resource_write, scope_access
import threading
import time
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("quaber.kubernetes")


class ResourceNotFoundError(LookupError):
    """The resource vanished between listing it and opening its card."""


class ResponseTooLarge(ValueError):
    pass


class LogTooLarge(ResponseTooLarge):
    pass


def _part(value: str) -> str:
    if not value or len(value) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
        raise ValueError("Некорректное имя ресурса Kubernetes.")
    return urllib.parse.quote(value, safe="")


class Kubernetes:
    def __init__(self, store: UserStore, proxy: ProxyManager):
        self.store, self.proxy = store, proxy
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.throttle_samples = ThrottleSamples()
        self.authorization = ClusterAuthorization(store, proxy)
        self.authorization_required = None
        self.access_denied = None
        self.cache = SnapshotCache()
        self._request_slots = threading.BoundedSemaphore(8)
        self.stopped = False

    def cluster(self, cluster_id: str) -> dict:
        return next((item for item in self.store.clusters() if item["id"] == cluster_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError("Кластер не найден.")

    def request(self, cluster_id: str, path: str, method: str = "GET", payload: dict | None = None, text: bool = False, max_bytes: int | None = 4 * 1024 * 1024, _retried=False, content_type="application/json", *, sink=None, budget=30):
        if max_bytes is None and sink is None:
            raise ValueError("Неограниченный ответ разрешён только для потоковой записи в файл.")
        if not self._request_slots.acquire(timeout=5):
            raise TimeoutError("Очередь запросов Kubernetes заполнена.")
        try:
            return self._request(cluster_id, path, method, payload, text, max_bytes, _retried, content_type, sink, budget)
        finally:
            self._request_slots.release()

    def _request(self, cluster_id, path, method, payload, text, max_bytes, _retried, content_type, sink=None, budget=30):
        if self.stopped:
            raise RuntimeError("Приложение завершает работу; новые запросы запрещены.")
        logger.debug("Kubernetes %s %s", method, path)
        cluster = self.cluster(cluster_id)
        if not cluster.get("enabled"):
            raise PermissionError("Кластер отключён.")
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": content_type} if data is not None else {}
        req = urllib.request.Request(self.proxy.url(cluster) + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=10) as response:
                deadline = time.monotonic()+budget
                chunks, size = [], 0
                while True:
                    if self.stopped:
                        raise RuntimeError("Приложение завершает работу; чтение ответа прекращено.")
                    if time.monotonic() > deadline:
                        raise TimeoutError("Превышен общий срок чтения ответа Kubernetes.")
                    chunk = response.read(65536 if max_bytes is None else min(65536, max_bytes+1-size))
                    if not chunk:
                        break
                    if sink is None:
                        chunks.append(chunk)
                    else:
                        sink.write(chunk)
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        break
                    # Test streams and HTTPResponse both expose read; real reads are bounded.
                if max_bytes is not None and size > max_bytes:
                    raise ResponseTooLarge("Ответ Kubernetes слишком большой.")
                if sink is not None:
                    return size
                body = b"".join(chunks)
                decoded = body.decode("utf-8", "replace")
                return decoded if text else json.loads(decoded) if decoded else {}
        except urllib.error.HTTPError as exc:
            logger.warning("Kubernetes HTTP %s · %s %s", exc.code, method, path)
            try:
                detail = exc.read(2048).decode("utf-8", "replace")
            finally:
                exc.close()
            if exc.code == 401:
                if not _retried:
                    try:
                        self.authorization.renew(cluster_id, cluster["encryptedKubeconfig"])
                    except AuthorizationRequired:
                        if self.authorization_required:
                            self.authorization_required(cluster_id)
                        raise
                    return self._request(cluster_id, path, method, payload, text, max_bytes, True, content_type, sink, budget)
                self.authorization.block(cluster_id)
                if self.authorization_required:
                    self.authorization_required(cluster_id)
                raise AuthorizationRequired(f"Кластер {cluster['name']} отклонил повторный вход. Введите пароль.") from None
            if exc.code == 403:
                if is_resource_write(method, path) and self.access_denied:
                    self.access_denied(cluster_id, method, path)
                raise PermissionError(f"Kubernetes запретил {'изменение' if is_resource_write(method, path) else 'доступ'} (403): {detail[:500]}") from exc
            if exc.code == 429:
                raise RuntimeError("Kubernetes отклонил действие: бюджет доступности PDB или ограничение частоты. Force-delete не выполнялся.") from None
            if exc.code == 409:
                raise RuntimeError("Объект Kubernetes изменился с момента просмотра. Обновите его и повторите действие.") from exc
            if exc.code == 404:
                raise ResourceNotFoundError("Ресурс больше не существует в Kubernetes.") from None
            raise RuntimeError(f"Kubernetes HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Не удалось связаться с Kubernetes: {exc.reason}") from exc

    def is_cluster_administrator(self, cluster_id):
        review = self.request(cluster_id, "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "POST", {
            "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": {"group": "*", "resource": "*", "verb": "*"}}})
        status = review.get("status", {})
        if type(status.get("allowed")) is not bool:
            raise ValueError("Кластер не вернул результат проверки прав администратора.")
        return status["allowed"]

    def access_summary(self, cluster_id, namespaces=None):
        try:
            if self.is_cluster_administrator(cluster_id):
                return {"level": "Администратор", "detail": "Разрешены все действия со всеми ресурсами кластера. Проверены эффективные права, а не имя роли."}
            scopes = list(dict.fromkeys(namespaces or self.namespaces(cluster_id)))
            if not scopes:
                return {"level": "Не определён", "detail": "Нет namespace для проверки прав."}
            results = []
            for namespace in scopes[:100]:
                response = self.request(cluster_id, "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews", "POST", {
                    "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview", "spec": {"namespace": namespace}})
                level, detail, complete = scope_access(response.get("status", {}))
                results.append((namespace, level, detail, complete))
            levels = {row[1] for row in results}
            if "Редактирование" in levels:
                level = "Редактирование" if levels == {"Редактирование"} and all(row[3] for row in results) else "Редактирование (частично)"
            elif any(not row[3] for row in results):
                level = "Не определён"
            else:
                level = "Чтение" if "Чтение" in levels else "Нет доступа"
            detail = "По проверенным namespace:\n" + "\n".join(f"{ns}: {access} · {actions}" for ns, access, actions, _ in results)
            if len(scopes) > 100:
                detail += "\nПроверены первые 100 namespace."
                if level == "Чтение":
                    level = "Чтение (частично)"
            return {"level": level, "detail": detail}
        except Exception as exc:
            return {"level": "Не определён", "detail": f"Не удалось проверить эффективные права: {exc}"}

    def list_pages(self, cluster_id, path):
        result, token, seen = [], "", set()
        total_bytes = 0
        deadline = time.monotonic()+60
        for _ in range(200):
            if time.monotonic() > deadline:
                raise TimeoutError("Превышен срок постраничной загрузки списка.")
            query = urllib.parse.urlencode({"limit": 200, **({"continue": token} if token else {})})
            page = self.request(cluster_id, path+("&" if "?" in path else "?")+query)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ValueError("Kubernetes вернул некорректный список.")
            total_bytes += len(json.dumps(page).encode("utf-8"))
            if total_bytes > 32_000_000:
                raise ValueError("Общий размер списка превышает поддерживаемые 32 МБ.")
            result.extend(page["items"])
            if len(result) > 40000:
                raise ValueError("Список превышает поддерживаемые 40000 объектов.")
            token = page.get("metadata", {}).get("continue", "")
            if not token:
                return result
            if not isinstance(token, str) or token in seen or len(token) > 10000:
                raise ValueError("Некорректный continue в списке Kubernetes.")
            seen.add(token)
        raise ValueError("Слишком много страниц Kubernetes.")

    def namespaces(self, cluster_id):
        return sorted(item["metadata"]["name"] for item in self.snapshot_list(cluster_id, "/api/v1/namespaces"))

    def snapshot_list(self, cluster_id, path):
        cluster = self.cluster(cluster_id)
        if not cluster.get("enabled") or self.stopped:
            raise PermissionError("Кластер отключён или приложение завершает работу.")
        key = (cluster_id, cluster.get("updatedAt"), path)
        return self.cache.get(key, lambda: self.list_pages(cluster_id, path))

    def deployments(self, cluster_id, namespace):
        return self.list_resources(cluster_id, "deployment", namespace)

    def pods(self, cluster_id, namespace, *, fresh=False):
        if fresh:
            return self.list_pages(cluster_id, self.path("pod", namespace))
        return self.list_resources(cluster_id, "pod", namespace)

    def replica_sets(self, cluster_id, namespace, *, fresh=False):
        if fresh:
            return self.list_pages(cluster_id, f"/apis/apps/v1/namespaces/{_part(namespace)}/replicasets")
        return self.snapshot_list(cluster_id, f"/apis/apps/v1/namespaces/{_part(namespace)}/replicasets")

    def resource(self, cluster_id: str, kind: str, namespace: str, name: str) -> dict:
        return self.request(cluster_id, self.path(kind, namespace, name))

    @staticmethod
    def path(kind: str, namespace: str, name: str = "") -> str:
        resources = {"pod": ("/api/v1", "pods"), "deployment": ("/apis/apps/v1", "deployments"),
                     "configmap": ("/api/v1", "configmaps"), "quota": ("/api/v1", "resourcequotas"),
                     "statefulset": ("/apis/apps/v1", "statefulsets"), "daemonset": ("/apis/apps/v1", "daemonsets"),
                     "replicaset": ("/apis/apps/v1", "replicasets"), "job": ("/apis/batch/v1", "jobs"),
                     "replicationcontroller": ("/api/v1", "replicationcontrollers"),
                     "cronjob": ("/apis/batch/v1", "cronjobs")}
        if kind not in resources:
            raise ValueError("Неизвестный тип ресурса.")
        base, plural = resources[kind]
        return f"{base}/namespaces/{_part(namespace)}/{plural}" + (f"/{_part(name)}" if name else "")

    def list_resources(self, cluster_id: str, kind: str, namespace: str) -> list[dict]:
        return self.snapshot_list(cluster_id, self.path(kind, namespace))

    def _audit(self, action: str, cluster_id: str, namespace: str, name: str) -> None:
        try:
            self.store.audit(action, cluster_id, namespace, name)
        except OSError as exc:
            raise RuntimeError("Операция Kubernetes выполнена, но запись аудита не удалась. Проверьте результат перед повтором.") from exc

    def replace(self, cluster_id: str, kind: str, namespace: str, name: str, edited: dict, expected_version: str, action: str | None = None) -> dict:
        if not isinstance(edited, dict) or not expected_version:
            raise ValueError("Сначала просмотрите объект и его resourceVersion.")
        metadata = edited.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("name") != name or metadata.get("namespace") != namespace:
            raise ValueError("Имя и namespace объекта нельзя менять.")
        if metadata.get("resourceVersion") != expected_version:
            raise ValueError("resourceVersion в редакторе изменён; обновите объект.")
        current = self.resource(cluster_id, kind, namespace, name)
        if current.get("metadata", {}).get("resourceVersion") != expected_version:
            raise RuntimeError("Объект изменился после открытия редактора. Обновите его.")
        if edited.get("metadata", {}).get("uid") != current.get("metadata", {}).get("uid"):
            raise RuntimeError("UID ресурса изменился. Откройте карточку заново.")
        if kind == "deployment":
            for container in edited.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                validate_resources(container.get("resources", {}))
        edited = copy.deepcopy(edited)
        edited["metadata"].pop("managedFields", None)
        path = self.path(kind, namespace, name)
        self.request(cluster_id, path+"?dryRun=All", "PUT", edited)
        with self.store.operation(action or f"replace-{kind}", cluster_id, namespace, name,
                                  uid=current.get("metadata", {}).get("uid"), version=expected_version):
            return self.request(cluster_id, path, "PUT", edited)

    def scale(self, cluster_id, namespace, name, replicas, expected_uid=None, expected_version=None):
        if type(replicas) is not int or not 0 <= replicas <= 10000:
            raise ValueError("Число реплик должно быть от 0 до 10000.")
        resource = self.resource(cluster_id, "deployment", namespace, name)
        check_identity(resource, expected_uid, expected_version)
        if not expected_version:
            raise ValueError("Требуется resourceVersion просмотренной карточки.")
        payload = {"apiVersion": "autoscaling/v1", "kind": "Scale", "metadata": {
            "name": name, "namespace": namespace, "uid": expected_uid, "resourceVersion": expected_version},
            "spec": {"replicas": replicas}}
        path = self.path("deployment", namespace, name)+"/scale"
        self.request(cluster_id, path+"?dryRun=All", "PUT", payload)
        with self.store.operation("scale-deployment", cluster_id, namespace, name, uid=expected_uid,
                                  version=expected_version, oldReplicas=resource.get("spec", {}).get("replicas", 1), newReplicas=replicas):
            self.request(cluster_id, path, "PUT", payload)
        return self.resource(cluster_id, "deployment", namespace, name)

    def scaling_policy(self, cluster_id, namespace, name):
        try:
            hpas = self.snapshot_list(cluster_id, f"/apis/autoscaling/v2/namespaces/{_part(namespace)}/horizontalpodautoscalers")
            matches = [h.get("metadata", {}).get("name", "") for h in hpas
                       if h.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment"
                       and h.get("spec", {}).get("scaleTargetRef", {}).get("name") == name]
            return {"known": True, "controllers": matches}
        except Exception as exc:
            return {"known": False, "controllers": [], "error": str(exc)}

    def scaling_info(self, cluster_id, namespace, name):
        policy = self.scaling_policy(cluster_id, namespace, name)
        if not policy["known"]:
            return "HPA: проверить не удалось (API/RBAC). Автоскейлер может изменить число реплик."
        return ("HPA управляет репликами: " + ", ".join(policy["controllers"]) + ". Может изменить заданное число."
                if policy["controllers"] else "HPA для этого Deployment не найден.")

    def restart(self, cluster_id, namespace, name, expected_uid=None, expected_version=None):
        resource = self.resource(cluster_id, "deployment", namespace, name)
        check_identity(resource, expected_uid, expected_version)
        if not expected_version:
            raise ValueError("Требуется resourceVersion просмотренной карточки.")
        payload = {"metadata": {"uid": expected_uid, "resourceVersion": expected_version}, "spec": {"template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()}}}}}
        path = self.path("deployment", namespace, name)
        self.request(cluster_id, path+"?dryRun=All", "PATCH", payload, content_type="application/merge-patch+json")
        with self.store.operation("restart-deployment", cluster_id, namespace, name, uid=expected_uid, version=expected_version):
            return self.request(cluster_id, path, "PATCH", payload, content_type="application/merge-patch+json")

    def delete_pod(self, cluster_id: str, namespace: str, name: str, uid: str, mode: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", uid) or mode not in ("restart", "delete"):
            raise ValueError("Не указан UID Pod или режим удаления.")
        current = self.resource(cluster_id, "pod", namespace, name)
        metadata = current.get("metadata", {})
        if metadata.get("uid") != uid:
            raise RuntimeError("Pod изменился после подтверждения. Обновите его.")
        controlled = any(owner.get("controller") for owner in metadata.get("ownerReferences", []))
        if controlled != (mode == "restart"):
            raise ValueError("Режим удаления Pod не соответствует наличию контроллера.")
        with self.store.operation("evict-pod" if controlled else "delete-pod", cluster_id, namespace, name, uid=uid):
            if controlled:
                self.request(cluster_id, self.path("pod", namespace, name)+"/eviction", "POST", {
                    "apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": name, "namespace": namespace},
                    "deleteOptions": {"preconditions": {"uid": uid}}})
            else:
                self.request(cluster_id, self.path("pod", namespace, name), "DELETE", {"preconditions": {"uid": uid}})

    def logs(self, cluster_id: str, namespace: str, name: str, container: str, previous: bool = False, tail: int | None = 1000) -> str:
        if tail is not None and not 1 <= tail <= 5000:
            raise ValueError("Количество строк логов вне диапазона.")
        options = {"container": container, "previous": str(previous).lower(), "timestamps": "true"}
        options["limitBytes"] = 4 * 1024 * 1024 + (1 if tail is None else 0)
        if tail is not None:
            options.update(tailLines=tail)
        query = urllib.parse.urlencode(options)
        try:
            return self.request(cluster_id, self.path("pod", namespace, name) + "/log?" + query, text=True)
        except ResponseTooLarge as exc:
            raise LogTooLarge("Лог превышает 4 МиБ и не может быть показан целиком.") from exc

    def download_logs(self, cluster_id, namespace, name, container, previous, destination):
        """Stream to an atomic local file; never accumulate the full log in RAM."""
        destination = Path(destination)
        query = urllib.parse.urlencode({"container": container, "previous": str(previous).lower(), "timestamps": "true"})
        fd, temporary = tempfile.mkstemp(prefix=".quaber-log-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                count = self.request(cluster_id, self.path("pod", namespace, name)+"/log?"+query,
                                     text=True, max_bytes=None, sink=stream, budget=300)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return count
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def events(self, cluster_id: str, namespace: str, kind: str, name: str, uid=None) -> list[dict]:
        query = urllib.parse.urlencode({"fieldSelector": f"involvedObject.kind={kind},involvedObject.name={name}" + (f",involvedObject.uid={uid}" if uid else "")})
        return sorted(self.list_pages(cluster_id, f"/api/v1/namespaces/{_part(namespace)}/events?{query}"), key=lambda e: e.get("lastTimestamp") or e.get("metadata", {}).get("creationTimestamp", ""), reverse=True)

    def metrics(self, cluster_id: str, namespace: str, name: str) -> dict:
        pod = self.resource(cluster_id, "pod", namespace, name)
        node = pod.get("spec", {}).get("nodeName")
        usage: dict[str, dict] = {}
        timestamp = None
        window = None
        source = None
        failure = ""
        try:
            try:
                metrics = self.cache.get((cluster_id, namespace, "usage"), lambda: self.list_pages(cluster_id, f"/apis/metrics.k8s.io/v1beta1/namespaces/{_part(namespace)}/pods"))
                metric = next(m for m in metrics if m.get("metadata", {}).get("name") == name)
            except (PermissionError, ResourceNotFoundError, StopIteration):
                metric = self.request(cluster_id, f"/apis/metrics.k8s.io/v1beta1/namespaces/{_part(namespace)}/pods/{_part(name)}")
            for container in metric.get("containers", []):
                usage[container["name"]] = {"cpu": cpu_cores(container.get("usage", {}).get("cpu")),
                                            "memory": memory_bytes(container.get("usage", {}).get("memory"))}
            timestamp, window, source = metric.get("timestamp"), metric.get("window"), "metrics-api"
        except Exception as exc:
            failure = str(exc)
        if not usage and node:
            try:
                summary = self.cache.get((cluster_id, node, "summary"), lambda: self.request(cluster_id, f"/api/v1/nodes/{_part(node)}/proxy/stats/summary", max_bytes=25_000_000))
                metric_pod = next((item for item in summary.get("pods", []) if item.get("podRef", {}).get("namespace") == namespace and item.get("podRef", {}).get("name") == name), {})
                for container in metric_pod.get("containers", []):
                    cpu, memory = container.get("cpu", {}), container.get("memory", {})
                    nano = cpu.get("usageNanoCores")
                    usage[container["name"]] = {"cpu": nano / 1e9 if isinstance(nano, (int, float)) else None,
                                                "memory": memory.get("workingSetBytes", memory.get("usageBytes"))}
                    timestamp = timestamp or cpu.get("time") or memory.get("time")
                if usage:
                    source = "kubelet"
            except Exception as exc:
                failure = f"{failure or 'Metrics API недоступен'}; {exc}"
        throttle: dict[str, float | None] = {}
        throttling_error = None
        if node:
            try:
                cadvisor = self.cache.get((cluster_id, node, "cadvisor"), lambda: self.request(cluster_id, f"/api/v1/nodes/{_part(node)}/proxy/metrics/cadvisor", text=True, max_bytes=25_000_000))
                throttle = self.throttle_samples.parse(cadvisor, cluster_id, namespace, name)
                if not throttle:
                    throttling_error = "cAdvisor не вернул CPU CFS counters для контейнеров Pod."
            except Exception as exc:
                throttling_error = str(exc)
        else:
            throttling_error = "Pod ещё не назначен на ноду."
        containers = []
        for container in pod.get("spec", {}).get("containers", []):
            name_of_container = container.get("name", "")
            resources = container.get("resources", {})
            current = usage.get(name_of_container, {})
            containers.append({"name": name_of_container,
                               "cpu": {"usageCores": current.get("cpu"), "requestCores": cpu_cores(resources.get("requests", {}).get("cpu")),
                                       "limitCores": cpu_cores(resources.get("limits", {}).get("cpu"))},
                               "memory": {"usageBytes": current.get("memory"), "requestBytes": memory_bytes(resources.get("requests", {}).get("memory")),
                                          "limitBytes": memory_bytes(resources.get("limits", {}).get("memory"))},
                               "cpuThrottlingPercent": throttle.get(name_of_container)})
        return {"available": bool(usage), "timestamp": timestamp, "window": window, "source": source,
                "throttlingAvailable": bool(throttle), "throttlingError": throttling_error,
                "containers": containers, **({} if usage else {"error": failure or "Метрики Pod недоступны."})}
