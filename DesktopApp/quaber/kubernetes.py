"""Kubernetes REST operations through the corporate kubectl proxy."""

from __future__ import annotations

import json
import copy
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .kubectl import ProxyManager
from .storage import UserStore
from .metrics import ThrottleSamples, cpu_cores, memory_bytes


def _part(value: str) -> str:
    if not value or len(value) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
        raise ValueError("Некорректное имя ресурса Kubernetes.")
    return urllib.parse.quote(value, safe="")


class Kubernetes:
    def __init__(self, store: UserStore, proxy: ProxyManager):
        self.store, self.proxy = store, proxy
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.throttle_samples = ThrottleSamples()

    def cluster(self, cluster_id: str) -> dict:
        return next((item for item in self.store.clusters() if item["id"] == cluster_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError("Кластер не найден.")

    def request(self, cluster_id: str, path: str, method: str = "GET", payload: dict | None = None, text: bool = False, max_bytes: int = 4 * 1024 * 1024):
        cluster = self.cluster(cluster_id)
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(self.proxy.url(cluster) + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError("Ответ Kubernetes слишком большой.")
                decoded = body.decode("utf-8", "replace")
                return decoded if text else json.loads(decoded) if decoded else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            if exc.code == 401:
                raise PermissionError(f"Истекла авторизация кластера {cluster['name']}; выполните вход снова.") from exc
            if exc.code == 403:
                raise PermissionError(f"Kubernetes RBAC запретил операцию ({exc.code}): {detail[:500]}") from exc
            if exc.code == 409:
                raise RuntimeError("Объект Kubernetes изменился с момента просмотра. Обновите его и повторите действие.") from exc
            raise RuntimeError(f"Kubernetes HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Не удалось связаться с Kubernetes: {exc.reason}") from exc

    def namespaces(self, cluster_id: str) -> list[str]:
        return sorted(item["metadata"]["name"] for item in self.request(cluster_id, "/api/v1/namespaces").get("items", []))

    def deployments(self, cluster_id: str, namespace: str) -> list[dict]:
        return self.request(cluster_id, f"/apis/apps/v1/namespaces/{_part(namespace)}/deployments").get("items", [])

    def pods(self, cluster_id: str, namespace: str) -> list[dict]:
        return self.request(cluster_id, f"/api/v1/namespaces/{_part(namespace)}/pods").get("items", [])

    def replica_sets(self, cluster_id: str, namespace: str) -> list[dict]:
        return self.request(cluster_id, f"/apis/apps/v1/namespaces/{_part(namespace)}/replicasets").get("items", [])

    def resource(self, cluster_id: str, kind: str, namespace: str, name: str) -> dict:
        return self.request(cluster_id, self.path(kind, namespace, name))

    @staticmethod
    def path(kind: str, namespace: str, name: str = "") -> str:
        resources = {"pod": ("/api/v1", "pods"), "deployment": ("/apis/apps/v1", "deployments"),
                     "configmap": ("/api/v1", "configmaps"), "quota": ("/api/v1", "resourcequotas")}
        if kind not in resources:
            raise ValueError("Неизвестный тип ресурса.")
        base, plural = resources[kind]
        return f"{base}/namespaces/{_part(namespace)}/{plural}" + (f"/{_part(name)}" if name else "")

    def list_resources(self, cluster_id: str, kind: str, namespace: str) -> list[dict]:
        return self.request(cluster_id, self.path(kind, namespace)).get("items", [])

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
        edited = copy.deepcopy(edited)
        edited["metadata"].pop("managedFields", None)
        result = self.request(cluster_id, self.path(kind, namespace, name), "PUT", edited)
        self._audit(action or f"replace-{kind}", cluster_id, namespace, name)
        return result

    def scale(self, cluster_id: str, namespace: str, name: str, replicas: int) -> dict:
        if not isinstance(replicas, int) or replicas < 0 or replicas > 10_000:
            raise ValueError("Число реплик должно быть от 0 до 10000.")
        resource = self.resource(cluster_id, "deployment", namespace, name)
        resource.setdefault("spec", {})["replicas"] = replicas
        return self.replace(cluster_id, "deployment", namespace, name, resource, resource["metadata"]["resourceVersion"], "scale-deployment")

    def restart(self, cluster_id: str, namespace: str, name: str) -> dict:
        resource = self.resource(cluster_id, "deployment", namespace, name)
        template = resource.setdefault("spec", {}).setdefault("template", {})
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations["kubectl.kubernetes.io/restartedAt"] = datetime.now(timezone.utc).isoformat()
        return self.replace(cluster_id, "deployment", namespace, name, resource, resource["metadata"]["resourceVersion"], "restart-deployment")

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
        self.request(cluster_id, self.path("pod", namespace, name), "DELETE", {"preconditions": {"uid": uid}})
        self._audit("restart-pod" if controlled else "delete-pod", cluster_id, namespace, name)

    def logs(self, cluster_id: str, namespace: str, name: str, container: str, previous: bool = False, tail: int = 500) -> str:
        if not 1 <= tail <= 5000:
            raise ValueError("Количество строк логов вне диапазона.")
        query = urllib.parse.urlencode({"container": container, "previous": str(previous).lower(), "tailLines": tail,
                                        "timestamps": "true", "limitBytes": 4 * 1024 * 1024})
        return self.request(cluster_id, self.path("pod", namespace, name) + "/log?" + query, text=True)

    def events(self, cluster_id: str, namespace: str, kind: str, name: str) -> list[dict]:
        query = urllib.parse.urlencode({"fieldSelector": f"involvedObject.kind={kind},involvedObject.name={name}"})
        return self.request(cluster_id, f"/api/v1/namespaces/{_part(namespace)}/events?{query}").get("items", [])

    def metrics(self, cluster_id: str, namespace: str, name: str) -> dict:
        pod = self.resource(cluster_id, "pod", namespace, name)
        node = pod.get("spec", {}).get("nodeName")
        usage: dict[str, dict] = {}
        timestamp = None
        window = None
        source = None
        failure = ""
        try:
            metric = self.request(cluster_id, f"/apis/metrics.k8s.io/v1beta1/namespaces/{_part(namespace)}/pods/{_part(name)}")
            for container in metric.get("containers", []):
                usage[container["name"]] = {"cpu": cpu_cores(container.get("usage", {}).get("cpu")),
                                            "memory": memory_bytes(container.get("usage", {}).get("memory"))}
            timestamp, window, source = metric.get("timestamp"), metric.get("window"), "metrics-api"
        except Exception as exc:
            failure = str(exc)
        if not usage and node:
            try:
                summary = self.request(cluster_id, f"/api/v1/nodes/{_part(node)}/proxy/stats/summary", max_bytes=25_000_000)
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
                cadvisor = self.request(cluster_id, f"/api/v1/nodes/{_part(node)}/proxy/metrics/cadvisor", text=True, max_bytes=25_000_000)
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
