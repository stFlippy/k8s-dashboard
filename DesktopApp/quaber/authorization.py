"""Serialize credential renewal and stop automatic retries after a rejected login."""
import threading

from .kubectl import locate_kubectl, login, _login_message


class AuthorizationRequired(PermissionError):
    pass


class ClusterAuthorization:
    def __init__(self, store, proxy):
        self.store, self.proxy = store, proxy
        self._guard = threading.Lock()
        self._locks = {}
        self._blocked = {}

    def signature(self, cluster):
        return (cluster.get("encryptedKubeconfig"), cluster.get("encryptedPassword"),
                self.store.preferences().get("kubectl_path", ""), cluster.get('accountId'), cluster.get('accountRevision'))

    def cluster(self, identifier):
        cluster = next((c for c in self.store.clusters() if c["id"] == identifier), None)
        if cluster is None:
            raise AuthorizationRequired("Кластер удалён из настроек.")
        return cluster

    def block(self, identifier, *, shared=False):
        with self._guard:
            cluster = self.cluster(identifier)
            group = [c for c in self.store.clusters() if c.get('accountId') == cluster['accountId']] if shared and cluster.get('accountId') else [cluster]
            for member in group:
                self._blocked[member["id"]] = self.signature(member)

    def renew(self, identifier, expected_config=None):
        initial = self.cluster(identifier)
        lock_key = initial.get('accountId') or identifier
        with self._guard:
            lock = self._locks.setdefault(lock_key, threading.Lock())
        from .auth_queue import connection_lock
        with connection_lock(self.store, identifier), lock:
            cluster = self.cluster(identifier)
            if not cluster.get("enabled"):
                raise AuthorizationRequired("Кластер отключён.")
            if expected_config is not None and cluster["encryptedKubeconfig"] != expected_config:
                return cluster
            if expected_config is None and not self.store.login_required(cluster):
                return cluster
            if cluster.get("authMode") == "kubeconfig":
                raise AuthorizationRequired("Обновите kubeconfig в настройках подключения.")
            signature = self.signature(cluster)
            with self._guard:
                blocked = self._blocked.get(identifier) == signature
            if blocked:
                raise AuthorizationRequired("Автоматический вход не удался. Введите пароль вручную.")
            try:
                password = self.store.saved_password(cluster)
            except Exception:
                self.block(identifier)
                raise AuthorizationRequired("Сохранённый пароль не удалось расшифровать. Введите пароль вручную.") from None
            if not password:
                raise AuthorizationRequired("Для входа в кластер требуется пароль.")
            try:
                executable = locate_kubectl(self.store.root.parent, self.store.preferences().get("kubectl_path", ""))
                from .auth_queue import serialized_login
                config = serialized_login(self.store, cluster, password,
                                          lambda: login(executable, cluster["server"], cluster["username"], password))
                self.store.update_logins({identifier: config}, expected={identifier: cluster})
                self.proxy.close(identifier)
                # Only this successful compare-and-update path may attest an
                # application-initiated revision transition for the active read.
                from .monitoring import _read_token
                from .request_policy import credentials_revision
                read_token = _read_token.get()
                renewed = self.cluster(identifier)
                if (read_token is not None
                        and all(renewed.get(k) == cluster.get(k) for k in
                                ('server', 'username', 'enabled', 'encryptedPassword', 'authMode'))):
                    read_token.renewals.append((identifier, credentials_revision(cluster), credentials_revision(renewed)))
            except Exception as exc:
                # A stale attempt is not a failed login for the replacement
                # account. Do not block automatic renewal of that new identity.
                if self.cluster(identifier) != cluster:
                    raise AuthorizationRequired("Запись кластера изменилась во время входа; старый результат отклонён.") from None
                safe = _login_message(str(exc), password)
                rejected = any(word in safe.lower() for word in ("unauthorized", "invalid credentials", "invalid password", "incorrect", "rejected", "неверн", "отклонён"))
                if not rejected and any(word in safe.lower() for word in ('connect','network','timeout','timed out','refused','сеть','подключиться','таймаут')):
                    raise ConnectionError(safe) from None
                self.block(identifier, shared=rejected)
                raise AuthorizationRequired(safe) from None
            return self.cluster(identifier)
