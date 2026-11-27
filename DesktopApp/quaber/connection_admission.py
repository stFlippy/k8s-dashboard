"""Stop automatic reads of a rejected credential revision, independently per connection."""
import threading
from .authorization import AuthorizationRequired


class ConnectionAdmission:
    def __init__(self):
        self._lock = threading.Lock()
        self._blocked = {}

    def block(self, cluster_id, revision):
        with self._lock:
            self._blocked[cluster_id] = revision
            while len(self._blocked) > 256:
                del self._blocked[next(iter(self._blocked))]

    def check(self, cluster_id, revision):
        with self._lock:
            blocked = self._blocked.get(cluster_id)
            if blocked == revision:
                raise AuthorizationRequired('Sign-in required. Update the connection credentials to resume monitoring.')
            if blocked is not None:
                self._blocked.pop(cluster_id, None)

    def is_blocked(self, cluster_id, revision):
        with self._lock:return self._blocked.get(cluster_id)==revision
