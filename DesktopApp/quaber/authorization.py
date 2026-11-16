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
                self.store.preferences().get("kubectl_path", ""))

    def cluster(self, identifier):
        cluster = next((c for c in self.store.clusters() if c["id"] == identifier), None)
        if cluster is None:
            raise AuthorizationRequired("Кластер удалён из настроек.")
        return cluster

    def block(self, identifier):
        with self._guard:
            self._blocked[identifier] = self.signature(self.cluster(identifier))

    def renew(self, identifier, expected_config=None):
        with self._guard:
            lock = self._locks.setdefault(identifier, threading.Lock())
        with lock:
            cluster = self.cluster(identifier)
            if not cluster.get("enabled"):
                raise AuthorizationRequired("Кластер отключён.")
            if expected_config is not None and cluster["encryptedKubeconfig"] != expected_config:
                return cluster
            if expected_config is None and not self.store.login_required(cluster):
                return cluster
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
                config = login(executable, cluster["server"], cluster["username"], password)
                self.store.update_logins({identifier: config}, expected={identifier: cluster})
                self.proxy.close(identifier)
            except Exception as exc:
                # A stale attempt is not a failed login for the replacement
                # account. Do not block automatic renewal of that new identity.
                if self.cluster(identifier) != cluster:
                    raise AuthorizationRequired("Запись кластера изменилась во время входа; старый результат отклонён.") from None
                self.block(identifier)
                safe = _login_message(str(exc), password)
                raise AuthorizationRequired(safe) from None
            return self.cluster(identifier)
