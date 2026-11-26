"""Non-secret immutable identity/version evidence for rejected approved writes."""
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionConflict:
    kind: str
    namespace: str
    name: str
    expected_uid: str
    expected_version: str
    observed_uid: str | None
    observed_version: str | None


class ResourceVersionConflict(RuntimeError):
    def __init__(self, details):
        super().__init__('Pod изменился после подтверждения. Обновите его и подтвердите новое действие.')
        self.details = details
