"""Kubernetes REST with default kubectl proxy and optional direct HTTPS."""

from __future__ import annotations

import json
from contextlib import contextmanager
import copy
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .kubectl import ProxyManager
from .storage import UserStore
from .metrics import ThrottleSamples
from .authorization import ClusterAuthorization, AuthorizationRequired
from .safety import check_identity, validate_resources
from .cache import SnapshotCache
from .access import is_resource_write, scope_access
import time
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass
from .monitoring import optional_read

logger = logging.getLogger("quaber.kubernetes")


class ResourceNotFoundError(LookupError):
    """The resource vanished between listing it and opening its card."""


class AggregateListForbidden(PermissionError):
    """Known HTTP 403 capability; scoped LIST remains subject to its own RBAC."""


class ResponseTooLarge(ValueError):
    retry_after = 60


class ResourceExpiredError(LookupError):
    """A collection continuation expired; restart from a complete new snapshot."""


class LogTooLarge(ResponseTooLarge):
    pass


@dataclass(frozen=True)
class ScaleOutcome:
    commit_state: str
    accepted: dict | None
    observed: dict | None
    refresh_error: Exception | None = None


class KubernetesAPIError(RuntimeError):
    """Structured read rejection; details are used for exact source classification."""
    def __init__(self, status, reason, path, message):
        super().__init__(f'Kubernetes HTTP {status}: {reason}')
        self.status, self.reason, self.path, self.message = status, reason, path, message


def _part(value: str) -> str:
    if not value or len(value) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
        raise ValueError("Некорректное имя ресурса Kubernetes.")
    return urllib.parse.quote(value, safe="")


GVR = {'pod': ('/api/v1', 'pods'), 'deployment': ('/apis/apps/v1', 'deployments'),
       'configmap': ('/api/v1', 'configmaps'), 'quota': ('/api/v1', 'resourcequotas'), 'event': ('/api/v1', 'events'),
       'statefulset': ('/apis/apps/v1', 'statefulsets'), 'daemonset': ('/apis/apps/v1', 'daemonsets'),
       'replicaset': ('/apis/apps/v1', 'replicasets'), 'job': ('/apis/batch/v1', 'jobs'),
       'replicationcontroller': ('/api/v1', 'replicationcontrollers'), 'cronjob': ('/apis/batch/v1', 'cronjobs')}
AGGREGATE_KINDS = ('deployment', 'pod', 'replicaset', 'statefulset', 'daemonset',
                   'replicationcontroller', 'job', 'cronjob', 'quota', 'event')

import contextvars
_snapshot_capture = contextvars.ContextVar('snapshot_capture', default=None)
_transport_binding = contextvars.ContextVar('transport_binding', default=None)


class Kubernetes:
    def __init__(self, store: UserStore, proxy: ProxyManager, *, opener=None, cache=None, policy=None, authorization=None):
        self.store, self.proxy = store, proxy
        self.opener = opener if opener is not None else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.throttle_samples = ThrottleSamples()
        self.authorization = authorization if authorization is not None else ClusterAuthorization(store, proxy)
        self.authorization_required = None
        self.access_denied = None
        self.cache = cache if cache is not None else SnapshotCache()
        from .request_policy import RequestPolicy
        from .endpoint_budget import EndpointBudget
        from .connection_admission import ConnectionAdmission
        self.request_policy = policy if policy is not None else RequestPolicy(endpoint_budget=EndpointBudget())
        self._connection_admission = ConnectionAdmission()
        self.stopped = False
        self.transport_mode = 'proxy'

    def set_transport_mode(self, mode):
        if mode not in ('proxy', 'https'):
            raise ValueError('Неизвестный режим подключения.')
        if mode != getattr(self, 'transport_mode', 'proxy'):
            self.transport_mode = mode
            self.cache.invalidate()
        else:
            self.transport_mode = mode

    def cluster(self, cluster_id: str) -> dict:
        return next((item for item in self.store.clusters() if item["id"] == cluster_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError("Кластер не найден.")

    def _check_credentials(self, cluster_id, cluster=None):
        monitoring = getattr(self, 'monitoring', None)
        if monitoring is not None: monitoring.check()
        from .request_policy import credential_bindings, credentials_revision
        current = cluster if cluster is not None else self.cluster(cluster_id)
        admission = getattr(self, '_connection_admission', None)
        if admission is not None:
            admission.check(cluster_id, credentials_revision(current))
        expected = credential_bindings.get().get(cluster_id)
        if expected is not None and credentials_revision(current) != expected:
            from .monitoring import MonitoringCancelled
            raise MonitoringCancelled('Credentials changed before physical dispatch.')

    def _block_reads(self, cluster_id, cluster):
        from .connection_admission import ConnectionAdmission
        from .request_policy import credentials_revision
        if not hasattr(self, '_connection_admission'):
            self._connection_admission = ConnectionAdmission()
        self._connection_admission.block(cluster_id, credentials_revision(cluster))
        callback = getattr(self, 'authorization_required', None)
        if callback:
            callback(cluster_id)

    def request(self, cluster_id: str, path: str, method: str = "GET", payload: dict | None = None, text: bool = False, max_bytes: int | None = 4 * 1024 * 1024, _retried=False, content_type="application/json", *, sink=None, budget=30, permit=None):
        if not isinstance(method, str):
            raise ValueError('An explicit HTTP method is required.')
        method = method.upper()
        review_kinds = {'/apis/authorization.k8s.io/v1/selfsubjectaccessreviews': 'SelfSubjectAccessReview',
                        '/apis/authorization.k8s.io/v1/selfsubjectrulesreviews': 'SelfSubjectRulesReview'}
        review = (method == 'POST' and path in review_kinds and isinstance(payload, dict)
                  and payload.get('kind') == review_kinds[path] and payload.get('apiVersion') == 'authorization.k8s.io/v1')
        if method in ('POST', 'PUT', 'PATCH', 'DELETE') and not review:
            from .write_guard import require_permit
            require_permit(permit)
            identity, action = permit.intent.identity, permit.intent.action
            expected = self.path(identity[1], identity[2], identity[3])
            actual = urllib.parse.urlsplit(path).path
            suffix = '/scale' if action == 'scale' else '/eviction' if action == 'delete' and method == 'POST' else ''
            methods = {'yaml': {'PUT'}, 'resources': {'PUT'}, 'scale': {'PUT'}, 'restart': {'PATCH'}, 'delete': {'DELETE', 'POST'}}
            if cluster_id != identity[0] or actual != expected+suffix or method not in methods.get(action, set()):
                raise PermissionError('Permit does not cover this method/target/subresource.')
            body = payload if isinstance(payload, dict) else {}
            if method == 'DELETE':
                uid = body.get('preconditions', {}).get('uid')
            elif action == 'delete':
                uid = body.get('deleteOptions', {}).get('preconditions', {}).get('uid')
            else:
                uid = body.get('metadata', {}).get('uid')
                if not body.get('metadata', {}).get('resourceVersion'):
                    raise PermissionError('Mutation dispatch requires resourceVersion.')
            if uid != identity[4]:
                raise PermissionError('Mutation dispatch requires the confirmed UID.')
            permit.check_rest_parameters(method, body)
        monitoring = getattr(self, 'monitoring', None)
        if monitoring is not None:
            monitoring.check()
        if max_bytes is None and sink is None:
            raise ValueError("Неограниченный ответ разрешён только для потоковой записи в файл.")
        from .cluster_workers import cluster_io
        reset = _transport_binding.set(getattr(self, 'transport_mode', 'proxy'))
        try:
            return cluster_io(self, cluster_id, lambda: self._request(
                cluster_id, path, method, payload, text, max_bytes, _retried, content_type, sink, budget, permit), permit=permit)
        finally:
            _transport_binding.reset(reset)

    def close(self):
        self.stopped = True
        if hasattr(self, '_io_workers'):
            self._io_workers.close()
        if hasattr(self, '_https_transport'):
            self._https_transport.close()

    @contextmanager
    def _dispatch(self, cluster_id, req, permit=None, *, opener=None):
        from .request_policy import RequestPolicy
        if not hasattr(self, 'request_policy'):
            self.request_policy = RequestPolicy()
        monitoring = getattr(self, 'monitoring', None)
        def check():
            if self.stopped:
                raise RuntimeError('Application is closing.')
            if monitoring is not None:
                monitoring.check()
            self._check_credentials(cluster_id)
            if permit is not None:
                permit.check()
            from .request_policy import credential_bindings, credentials_revision
            expected = credential_bindings.get().get(cluster_id)
            if expected is not None and credentials_revision(self.cluster(cluster_id)) != expected:
                from .monitoring import MonitoringCancelled
                raise MonitoringCancelled('Credentials changed before physical dispatch.')
        policy = self.request_policy
        policy.bind_endpoint(cluster_id, self.cluster(cluster_id).get('server', ''))
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            full_log = urllib.parse.urlsplit(req.full_url).path.endswith('/log') and 'tailLines' not in query
            heavy = full_log or any(part in req.full_url for part in ('/proxy/stats/summary', '/proxy/metrics/cadvisor', '/api/v1/query_range'))
            with policy.slot(cluster_id, check, monitor=not is_resource_write(req.get_method(), req.selector),
                             long_download=full_log, heavy=heavy):
                check()
                try:
                    check()
                    if permit is not None:
                        dry_run = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get('dryRun') == ['All']
                        permit.check_rest_parameters(req.get_method(), json.loads(req.data))
                        permit.check(commit=not dry_run)
                except Exception:
                    with policy.condition:
                        policy.stats['canceled'] += 1
                    raise
                policy.dispatched(cluster_id)
                with (self.opener if opener is None else opener).open(req, timeout=10) as response:
                    policy.status(getattr(response, 'status', 200))
                    if permit is not None and not dry_run:
                        permit.state = 'accepted'
                    yield response
                    if permit is None:
                        check()
            policy.succeeded(cluster_id)
        except urllib.error.HTTPError as exc:
            policy.status(exc.code)
            # A gateway/server failure after real dispatch does not prove the
            # write was rejected. Preserve UNKNOWN (or an already accepted 2xx).
            if (permit is not None and permit.state != 'accepted'
                    and (permit.state != 'commit-dispatched' or exc.code < 500)):
                permit.state = 'rejected'
            raise
        except urllib.error.URLError:
            raise

    def _request(self, cluster_id, path, method, payload, text, max_bytes, _retried, content_type, sink=None, budget=30, permit=None):
        monitoring = getattr(self, 'monitoring', None)
        if monitoring is not None:
            monitoring.check()
        if self.stopped:
            raise RuntimeError("Приложение завершает работу; новые запросы запрещены.")
        logger.debug("Kubernetes %s %s", method, urllib.parse.urlsplit(path).path)
        cluster = self.cluster(cluster_id)
        self._check_credentials(cluster_id, cluster)
        if not cluster.get("enabled"):
            raise PermissionError("Кластер отключён.")
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": content_type} if data is not None else {}
        opener = None
        if (_transport_binding.get() or getattr(self, 'transport_mode', 'proxy')) == 'https':
            from .https_transport import HTTPSTransport
            # Per-cluster workers can enter concurrently on first use.
            from .cluster_workers import _creation
            with _creation:
                if not hasattr(self, '_https_transport'):
                    self._https_transport = HTTPSTransport(self.store)
            connection = self._https_transport.connection(cluster)
            headers.update(connection.headers)
            address, opener = connection.server, connection.opener
        else:
            address = self.proxy.url(cluster)
        req = urllib.request.Request(address + path, data=data, headers=headers, method=method)
        try:
            if monitoring is not None:
                monitoring.check()
            with self._dispatch(cluster_id, req, permit, **({'opener': opener} if opener is not None else {})) as response:
                deadline = time.monotonic()+budget
                chunks, size = [], 0
                while True:
                    if monitoring is not None:
                        monitoring.check()
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
                    self.request_policy.received(len(chunk))
                    if max_bytes is not None and size > max_bytes:
                        break
                    # Test streams and HTTPResponse both expose read; real reads are bounded.
                if max_bytes is not None and size > max_bytes:
                    raise ResponseTooLarge("Ответ Kubernetes слишком большой.")
                if sink is not None:
                    return size
                body = b"".join(chunks)
                decoded = body.decode("utf-8", "replace")
                result = decoded if text else json.loads(decoded) if decoded else {}
                from .monitoring import _read_token
                token = _read_token.get()
                if token is not None:
                    token.fetched.set()
                    from .resume import core_read
                    if method == 'GET' and not text and core_read(path):
                        token.core_reads.append(cluster_id)
                if method == 'GET' and not text and callable(getattr(getattr(self, 'monitoring', None), 'record_read', None)):
                    from .request_policy import credentials_revision
                    self.monitoring.record_read(cluster_id, path, 200, credentials_revision(cluster))
                return result
        except urllib.error.HTTPError as exc:
            if method == 'GET' and callable(getattr(getattr(self, 'monitoring', None), 'record_read', None)):
                from .request_policy import credentials_revision
                self.monitoring.record_read(cluster_id, path, exc.code, credentials_revision(cluster))
            logger.warning("Kubernetes HTTP %s · %s %s", exc.code, method, urllib.parse.urlsplit(path).path)
            try:
                error_body = exc.read(2048)
                self.request_policy.received(len(error_body))
                detail = error_body.decode("utf-8", "replace")
            finally:
                exc.close()
            if exc.code >= 400:
                from .app_logging import log_problem
                log_problem(f"{cluster.get('name', cluster_id)} · {method} {urllib.parse.urlsplit(path).path}",
                            f'HTTP {exc.code}: {detail[:4000]}')
            if exc.code == 401:
                if permit is not None:
                    raise PermissionError('Operation rejected by authorization; renew access and confirm a new intent.') from None
                if not _retried:
                    from .monitoring import _read_token
                    read_token = _read_token.get()
                    if read_token is not None and not read_token.allow_renewal:
                        self._block_reads(cluster_id, cluster)
                        raise AuthorizationRequired('Sign-in required: renewed credentials were rejected; retry explicitly.') from None
                    try:
                        self.authorization.renew(cluster_id, cluster["encryptedKubeconfig"])
                    except AuthorizationRequired:
                        self._block_reads(cluster_id, cluster)
                        raise
                    return self._request(cluster_id, path, method, payload, text, max_bytes, True, content_type, sink, budget)
                self.authorization.block(cluster_id)
                self._block_reads(cluster_id, cluster)
                raise AuthorizationRequired(f"Кластер {cluster['name']} отклонил повторный вход. Введите пароль.") from None
            if exc.code == 403:
                if is_resource_write(method, path) and self.access_denied:
                    self.access_denied(cluster_id, method, path)
                raise PermissionError(f"Kubernetes запретил {'изменение' if is_resource_write(method, path) else 'доступ'} (403): {detail[:500]}") from exc
            if exc.code == 429:
                raise RuntimeError("Kubernetes отклонил действие: бюджет доступности PDB или ограничение частоты. Force-delete не выполнялся." if is_resource_write(method, path) else
                                   "Kubernetes ограничил частоту чтений (429); повтор отложен по Retry-After.") from None
            if exc.code == 409:
                raise RuntimeError("Объект Kubernetes изменился с момента просмотра. Обновите его и повторите действие.") from exc
            if exc.code == 404:
                raise ResourceNotFoundError("Ресурс больше не существует в Kubernetes.") from None
            if exc.code == 410:
                raise ResourceExpiredError("Срок действия снимка Kubernetes истёк.") from exc
            if exc.code == 400:
                try:
                    body = json.loads(detail)
                except (ValueError, TypeError):
                    body = {}
                raise KubernetesAPIError(400, body.get('reason','BadRequest'),
                                         urllib.parse.urlsplit(path).path, body.get('message','')) from exc
            raise RuntimeError(f"Kubernetes HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            if method == 'GET' and callable(getattr(getattr(self, 'monitoring', None), 'record_read', None)):
                from .request_policy import credentials_revision
                self.monitoring.record_read(cluster_id, path, 0, credentials_revision(cluster))
            raise ConnectionError(f"Не удалось связаться с Kubernetes: {exc.reason}") from exc

    def is_cluster_administrator(self, cluster_id):
        review = self.request(cluster_id, "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "POST", {
            "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": {"group": "*", "resource": "*", "verb": "*"}}})
        status = review.get("status", {})
        if type(status.get("allowed")) is not bool:
            raise ValueError("Кластер не вернул результат проверки прав администратора.")
        return status["allowed"]

    def access_summary(self, cluster_id, namespaces=None, *, force=False):
        from .request_policy import credentials_revision, request_intent
        self._check_credentials(cluster_id)
        key = (cluster_id, credentials_revision(self.cluster(cluster_id)), 'access-summary',
               tuple(sorted(set(namespaces or ()))))
        return copy.deepcopy(self.cache.get(key, lambda: self._access_summary(cluster_id, namespaces),
                                            ttl=60, max_age=0 if force or request_intent.get().fresh else None))

    def _access_summary(self, cluster_id, namespaces=None):
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
        limit, restarts = 200, 0
        for _ in range(200):
            if time.monotonic() > deadline:
                raise TimeoutError("Превышен срок постраничной загрузки списка.")
            query = urllib.parse.urlencode({"limit": limit, **({"continue": token} if token else {})})
            try:
                page = self.request(cluster_id, path+("&" if "?" in path else "?")+query)
            except (ResponseTooLarge, ResourceExpiredError) as exc:
                if restarts >= 3 or (isinstance(exc, ResponseTooLarge) and limit == 1):
                    raise
                restarts += 1
                if isinstance(exc, ResponseTooLarge):
                    total_bytes += 4 * 1024 * 1024 + 1
                    limit = 50 if limit == 200 else 10 if limit == 50 else 1
                result, token, seen = [], "", set()
                continue
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ValueError("Kubernetes вернул некорректный список.")
            total_bytes += len(json.dumps(page).encode("utf-8"))
            if total_bytes > 32_000_000:
                raise ResponseTooLarge("Общий размер списка превышает поддерживаемые 32 МБ.")
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

    def snapshot_list(self, cluster_id, path, *, max_age=None):
        from .request_policy import request_intent, LIMITS
        if request_intent.get().fresh:
            max_age = 0
        cluster = self.cluster(cluster_id)
        self._check_credentials(cluster_id, cluster)
        if not cluster.get("enabled") or self.stopped:
            raise PermissionError("Кластер отключён или приложение завершает работу.")
        from .request_policy import credentials_revision
        key = (cluster_id, credentials_revision(cluster), path)
        from .cache import MISSING
        for kind in AGGREGATE_KINDS:
            base, plural = GVR[kind]
            match = re.fullmatch(re.escape(base)+r'/namespaces/([^/]+)/'+re.escape(plural), path)
            if match:
                complete = self.cache.existing((cluster_id, key[1], f'{base}/{plural}'), max_age=max_age)
                if complete is not MISSING:
                    namespace = urllib.parse.unquote(match[1])
                    result = self.cache.namespace_projection((cluster_id, key[1], f'{base}/{plural}'), complete, namespace)
                    self._check_credentials(cluster_id)
                    self._capture_snapshot_time(cluster_id, path, (cluster_id, key[1], f'{base}/{plural}'))
                    return result
                break
        result = self.cache.get(key, lambda: self.list_pages(cluster_id, path),
                                ttl=LIMITS.namespaces_ttl if path == '/api/v1/namespaces' else None, max_age=max_age)
        self._check_credentials(cluster_id)
        self._capture_snapshot_time(cluster_id, path, key)
        return result

    def _capture_snapshot_time(self, cluster_id, requested_path, key):
        from datetime import timedelta
        with self.cache.lock:
            entry = self.cache.entries.get(key)
            age = max(0., self.cache.clock()-entry[3]) if entry and not entry[2] else None
        captured = (datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat() if age is not None else None
        _snapshot_capture.set((cluster_id, requested_path, captured))

    @staticmethod
    def snapshot_captured_at(cluster_id, path):
        value = _snapshot_capture.get()
        return value[2] if value is not None and value[:2] == (cluster_id, path) else None

    def all_namespace_resources(self, cluster_id, kind):
        if kind not in AGGREGATE_KINDS: raise ValueError('Unsupported aggregate collection.')
        base, plural = GVR[kind]
        from .request_policy import credentials_revision
        key = (cluster_id, credentials_revision(self.cluster(cluster_id)), kind)
        capabilities = getattr(self, '_aggregate_capabilities', {})
        self._aggregate_capabilities = capabilities
        now = time.monotonic()
        cached = capabilities.get(key)
        if cached and cached[0] > now:
            raise AggregateListForbidden('Cluster-wide LIST unavailable; using permitted namespace scope.')
        try:
            return self.snapshot_list(cluster_id, f'{base}/{plural}')
        except PermissionError as exc:
            if not isinstance(exc.__cause__, urllib.error.HTTPError) or exc.__cause__.code != 403:
                raise
            from .request_policy import LIMITS
            capabilities[key] = (now+LIMITS.capability_ttl, False)
            while len(capabilities) > 128:
                del capabilities[next(iter(capabilities))]
            raise AggregateListForbidden('Cluster-wide LIST forbidden; using permitted namespace scope.') from exc

    def deployments(self, cluster_id, namespace):
        return self.list_resources(cluster_id, "deployment", namespace)

    def pods(self, cluster_id, namespace, *, fresh=False):
        if fresh:
            return self.snapshot_list(cluster_id, self.path('pod', namespace), max_age=1)
        return self.list_resources(cluster_id, "pod", namespace)

    def replica_sets(self, cluster_id, namespace, *, fresh=False):
        if fresh:
            return self.snapshot_list(cluster_id, self.path('replicaset', namespace), max_age=1)
        return self.snapshot_list(cluster_id, f"/apis/apps/v1/namespaces/{_part(namespace)}/replicasets")

    def resource(self, cluster_id: str, kind: str, namespace: str, name: str) -> dict:
        return self.request(cluster_id, self.path(kind, namespace, name))

    @staticmethod
    def path(kind: str, namespace: str, name: str = "") -> str:
        if kind not in GVR:
            raise ValueError("Неизвестный тип ресурса.")
        base, plural = GVR[kind]
        return f"{base}/namespaces/{_part(namespace)}/{plural}" + (f"/{_part(name)}" if name else "")

    def list_resources(self, cluster_id: str, kind: str, namespace: str) -> list[dict]:
        return self.snapshot_list(cluster_id, self.path(kind, namespace))


    def invalidate_resource(self, cluster_id, kind, namespace, name):
        from .collection_reader import pod_owner_kinds
        dependencies = {
            'deployment': {'deployment', 'replicaset', 'pod'},
            'statefulset': {'statefulset', 'pod'}, 'daemonset': {'daemonset', 'pod'},
            'replicaset': {'replicaset', 'pod'}, 'replicationcontroller': {'replicationcontroller', 'pod'},
            'cronjob': {'cronjob', 'job', 'pod'}, 'job': {'job', 'pod'},
            'pod': {'pod', 'deployment', 'replicaset', 'statefulset', 'daemonset',
                    'replicationcontroller', 'job'},
        }
        kinds = dependencies.get(kind, {kind})
        if kind == 'pod':
            kinds = pod_owner_kinds(self.cache, cluster_id, namespace, name)
        namespace_path = f'/namespaces/{_part(namespace)}/'
        aggregate_paths = {f'{GVR[k][0]}/{GVR[k][1]}' for k in kinds if k in GVR}
        scoped_paths = {f'{GVR[k][0]}{namespace_path}{GVR[k][1]}' for k in kinds if k in GVR}
        self.cache.invalidate(lambda key: isinstance(key, tuple) and key[0] == cluster_id
                              and any(isinstance(part, str) and
                                      (part in aggregate_paths or any(part == path or part.startswith(path+'/') for path in scoped_paths)
                                       or (kind == 'pod' and part in ('summary', 'cadvisor-counters')))
                                      for part in key[1:]))

    def replace(self, cluster_id: str, kind: str, namespace: str, name: str, edited: dict, expected_version: str, action: str | None = None, *, permit=None) -> dict:
        from .write_guard import require_permit
        require_permit(permit)
        if permit.intent.action not in ('yaml', 'resources'):
            raise PermissionError('Permit action does not allow replacement.')
        permit.check_parameters(edited)
        if not isinstance(edited, dict) or not expected_version:
            raise ValueError("Сначала просмотрите объект и его resourceVersion.")
        metadata = edited.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("name") != name or metadata.get("namespace") != namespace:
            raise ValueError("Имя и namespace объекта нельзя менять.")
        if metadata.get("resourceVersion") != expected_version:
            raise ValueError("resourceVersion в редакторе изменён; обновите объект.")
        require_permit(permit, (cluster_id, kind, namespace, name, metadata.get('uid')))
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
        self.request(cluster_id, path+"?dryRun=All", "PUT", edited, permit=permit)
        with self.store.operation(action or f"replace-{kind}", cluster_id, namespace, name,
                                  uid=current.get("metadata", {}).get("uid"), version=expected_version):
            return self.request(cluster_id, path, "PUT", edited, permit=permit)

    def scale(self, cluster_id, namespace, name, replicas, expected_uid=None, expected_version=None, *, permit=None):
        outcome = self.scale_controller(cluster_id,'deployment',namespace,name,replicas,expected_uid,expected_version,permit=permit)
        if outcome.refresh_error is not None:
            raise RuntimeError('Replica change was accepted; refresh or audit failed. Check the resource before another action.') from outcome.refresh_error
        return outcome.observed

    def scale_controller(self, cluster_id, kind, namespace, name, replicas, expected_uid=None, expected_version=None, *, permit=None):
        from .write_guard import require_permit
        if kind not in ('deployment','statefulset'): raise ValueError('Unsupported scalable controller.')
        require_permit(permit, (cluster_id, kind, namespace, name, expected_uid), 'scale')
        permit.check_parameters({'replicas': replicas, 'resourceVersion': expected_version})
        if type(replicas) is not int or not 0 <= replicas <= 10000:
            raise ValueError("Число реплик должно быть от 0 до 10000.")
        resource = self.resource(cluster_id, kind, namespace, name)
        check_identity(resource, expected_uid, expected_version)
        if not expected_version:
            raise ValueError("Требуется resourceVersion просмотренной карточки.")
        policy = self.scaling_policy(cluster_id, namespace, name, fresh=True,kind=kind)
        if not policy.get('known') or policy.get('controllers'):
            raise PermissionError('Replica changes require a fresh known HPA policy without an active controller.')
        payload = {"apiVersion": "autoscaling/v1", "kind": "Scale", "metadata": {
            "name": name, "namespace": namespace, "uid": expected_uid, "resourceVersion": expected_version},
            "spec": {"replicas": replicas}}
        path = self.path(kind, namespace, name)+"/scale"
        self.request(cluster_id, path+"?dryRun=All", "PUT", payload, permit=permit)
        accepted = None
        try:
            with self.store.operation('scale-'+kind, cluster_id, namespace, name, uid=expected_uid,
                                      version=expected_version, oldReplicas=resource.get('spec',{}).get('replicas',1),newReplicas=replicas):
                accepted = self.request(cluster_id,path,'PUT',payload,permit=permit)
            return ScaleOutcome('accepted',accepted,self.resource(cluster_id,kind,namespace,name))
        except Exception as exc:
            if permit.state != 'accepted': raise
            return ScaleOutcome('accepted',accepted,None,exc)

    def scaling_policy(self, cluster_id, namespace, name, *, fresh=False,kind='deployment'):
        if kind not in ('deployment','statefulset'): raise ValueError('Unsupported scale policy kind.')
        try:
            path = f"/apis/autoscaling/v2/namespaces/{_part(namespace)}/horizontalpodautoscalers"
            hpas = self.list_pages(cluster_id, path) if fresh else self.snapshot_list(cluster_id, path)
            matches = [h.get("metadata", {}).get("name", "") for h in hpas
                       if h.get("spec", {}).get("scaleTargetRef", {}).get("kind") == {'deployment':'Deployment','statefulset':'StatefulSet'}[kind]
                       and h.get("spec", {}).get("scaleTargetRef", {}).get("name") == name]
            return {"known": True, "controllers": matches}
        except Exception as exc:
            return {"known": False, "controllers": [], "error": str(exc)}


    def restart(self, cluster_id, namespace, name, expected_uid=None, expected_version=None, *, permit=None):
        from .write_guard import require_permit
        require_permit(permit, (cluster_id, 'deployment', namespace, name, expected_uid), 'restart')
        permit.check_parameters(None)
        resource = self.resource(cluster_id, "deployment", namespace, name)
        check_identity(resource, expected_uid, expected_version)
        if not expected_version:
            raise ValueError("Требуется resourceVersion просмотренной карточки.")
        payload = {"metadata": {"uid": expected_uid, "resourceVersion": expected_version}, "spec": {"template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()}}}}}
        path = self.path("deployment", namespace, name)
        self.request(cluster_id, path+"?dryRun=All", "PATCH", payload, content_type="application/merge-patch+json", permit=permit)
        with self.store.operation("restart-deployment", cluster_id, namespace, name, uid=expected_uid, version=expected_version):
            return self.request(cluster_id, path, "PATCH", payload, content_type="application/merge-patch+json", permit=permit)

    def execute_pod(self, cluster_id, namespace, name, uid, container, command, *, expected_instance=None, permit=None):
        """Admit exec through the same budget and one-shot permit as REST writes."""
        from .write_guard import require_permit
        require_permit(permit, (cluster_id, 'pod', namespace, name, uid), 'exec')
        try:
            return self._execute_pod(cluster_id, namespace, name, uid, container, command,
                                     expected_instance=expected_instance,permit=permit)
        except Exception as exc:
            if permit.state == 'issued': permit.state = 'canceled-before-dispatch'
            elif permit.state == 'commit-dispatched':
                from .access import server_forbidden
                if server_forbidden(exc): permit.state = 'rejected'
            raise

    def _execute_pod(self, cluster_id, namespace, name, uid, container, command, *, expected_instance,permit):
        from .write_guard import require_permit
        from .request_policy import expected_credentials, RequestPolicy
        identity = (cluster_id, 'pod', namespace, name, uid)
        require_permit(permit, identity, 'exec')
        from .container_instance import ContainerInstance,capture_instance
        if not isinstance(expected_instance,ContainerInstance) or expected_instance.pod_uid!=uid or expected_instance.name!=container:
            raise PermissionError('Terminal requires the approved container instance.')
        parameters = {'container':container,'command':command,'instance':expected_instance.parameters()}
        permit.check_parameters(parameters)
        if not container or not command:
            raise ValueError('Container and command are required.')
        if not hasattr(self, 'request_policy'): self.request_policy = RequestPolicy()
        self.request_policy.bind_endpoint(cluster_id, self.cluster(cluster_id).get('server', ''))
        with expected_credentials({cluster_id: permit.intent.credentials}):
            current = self.resource(cluster_id, 'pod', namespace, name)
            check_identity(current, uid)
            if capture_instance(current,container) != expected_instance:
                raise PermissionError('Экземпляр контейнера изменился. Команда не отправлена; обновите карточку и проверьте ввод.')
            def check():
                permit.check(identity, 'exec')
                self._check_credentials(cluster_id)
                if self.stopped: raise RuntimeError('Application is closing.')
            with self.store.operation('exec', cluster_id, namespace, name, uid=uid, container=container):
                with self.request_policy.slot(cluster_id, check, monitor=False):
                    check()
                    cluster = self.cluster(cluster_id)
                    if not cluster.get('enabled'): raise PermissionError('Cluster is disabled.')
                    permit.check_parameters(parameters)
                    permit.check(commit=True)
                    self.request_policy.dispatched(cluster_id)
                    result = self.proxy.execute(cluster, namespace, name, container, command)
                    permit.state = 'accepted'
                    self.request_policy.succeeded(cluster_id)
                    return result

    def delete_pod(self, cluster_id: str, namespace: str, name: str, uid: str, mode: str, *, expected_version=None, permit=None) -> None:
        from .write_guard import require_permit
        require_permit(permit, (cluster_id, 'pod', namespace, name, uid), 'delete')
        if not isinstance(expected_version,str) or not expected_version:
            raise PermissionError('Removal requires the resourceVersion approved by the user.')
        permit.check_parameters({'mode': mode,'resourceVersion':expected_version})
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", uid) or mode not in ("restart", "delete"):
            raise ValueError("Не указан UID Pod или режим удаления.")
        current = self.resource(cluster_id, "pod", namespace, name)
        metadata = current.get("metadata", {})
        if metadata.get("uid") != uid or metadata.get('resourceVersion') != expected_version:
            from .conflicts import ResourceVersionConflict,VersionConflict
            raise ResourceVersionConflict(VersionConflict('pod',namespace,name,uid,expected_version,
                                                          metadata.get('uid'),metadata.get('resourceVersion')))
        controlled = any(owner.get("controller") for owner in metadata.get("ownerReferences", []))
        if controlled != (mode == "restart"):
            raise ValueError("Режим удаления Pod не соответствует наличию контроллера.")
        with self.store.operation("evict-pod" if controlled else "delete-pod", cluster_id, namespace, name, uid=uid):
            if controlled:
                self.request(cluster_id, self.path("pod", namespace, name)+"/eviction", "POST", {
                    "apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": name, "namespace": namespace},
                    "deleteOptions": {"preconditions": {"uid": uid,'resourceVersion':expected_version}}}, permit=permit)
            else:
                self.request(cluster_id, self.path("pod", namespace, name), "DELETE",
                             {"preconditions": {"uid": uid,'resourceVersion':expected_version}}, permit=permit)

    @optional_read
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

    @optional_read
    def download_logs(self, cluster_id, namespace, name, container, previous, destination, *, cancel=None):
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
            if cancel is not None and cancel.is_set():
                from .monitoring import MonitoringCancelled
                raise MonitoringCancelled('Log source changed before publication.')
            os.replace(temporary, destination)
            return count
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @optional_read
    def events(self, cluster_id: str, namespace: str, kind: str, name: str, uid=None) -> list[dict]:
        query = urllib.parse.urlencode({"fieldSelector": f"involvedObject.kind={kind},involvedObject.name={name}" + (f",involvedObject.uid={uid}" if uid else "")})
        return sorted(self.list_pages(cluster_id, f"/api/v1/namespaces/{_part(namespace)}/events?{query}"), key=lambda e: e.get("lastTimestamp") or e.get("metadata", {}).get("creationTimestamp", ""), reverse=True)

    @optional_read
    def metrics_many(self, cluster_id, namespace, pods):
        from .metrics_reader import MetricsReader
        return MetricsReader(self).many(cluster_id, namespace, pods)

    def metrics_history(self, cluster_id, namespace, pod, *, expected_uid=None):
        from .metrics_backend import HistoryReader
        return HistoryReader(self).load(cluster_id, namespace, pod, expected_uid=expected_uid)

    @optional_read
    def metrics(self, cluster_id, namespace, name, *, include_throttling=True, pod_snapshot=None, expected_uid=None,
                _usage_index=None, _summary_indexes=None):
        from .metrics_reader import MetricsReader
        return MetricsReader(self).load(cluster_id, namespace, name, include_throttling=include_throttling,
            pod_snapshot=pod_snapshot, expected_uid=expected_uid,
            _usage_index=_usage_index, _summary_indexes=_summary_indexes)
