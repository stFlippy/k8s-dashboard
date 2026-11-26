"""Resume attempt identity and physical-read evidence, independent of widgets."""
from dataclasses import dataclass
from urllib.parse import urlsplit
import uuid
import re


@dataclass(frozen=True)
class ResumeAttempt:
    attempt_id: str
    epoch: int
    revisions: tuple
    started: float
    deadline: float
    scopes: tuple = ()

    @classmethod
    def start(cls, epoch, revisions, now, scopes=()):
        return cls(uuid.uuid4().hex, epoch, tuple(revisions), now, now + 30, tuple(scopes))


@dataclass(frozen=True)
class ReadScope:
    cluster_id: str
    resources: tuple
    namespaces: tuple | None = None
    name: str | None = None

    def accepts(self, path):
        match = re.fullmatch(r'/(?:api/v1|apis/apps/v1|apis/batch/v1)/'
                             r'(?:namespaces/([^/]+)/)?([^/]+)(?:/([^/]+))?', urlsplit(path).path)
        if not match:
            return False
        namespace, resource, name = match.groups()
        return (resource in self.resources and
                (self.namespaces is None or namespace in self.namespaces or resource == 'namespaces') and
                (self.name is None or name == self.name))


@dataclass(frozen=True)
class PhysicalRead:
    attempt_id: str
    epoch: int
    cluster_id: str
    revision: str
    path: str
    status: int
    optional: bool
    renewals: tuple = ()


def core_read(path):
    path = path.split('?', 1)[0]
    # Unknown endpoints fail closed. Node proxies/stats and ancillary APIs
    # cannot establish that a demanded workload read completed.
    return bool(re.fullmatch(
        r'(?:/api/v1/namespaces|/(?:api/v1|apis/apps/v1|apis/batch/v1)/'
        r'(?:namespaces/[^/]+/)?(?:pods|deployments|replicasets|statefulsets|daemonsets|jobs|cronjobs|configmaps)'
        r'(?:/[^/]+)?)', path))
