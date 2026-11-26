"""Small immutable marker values; never owns a Kubernetes resource."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PodVisual:
    identity: tuple
    status: str
    color: str
    recent: bool
    stale: bool
    tooltip: str
    schedule: object = None
    schedule_confirmed: bool = False


def condition_tooltip(resource, fallback):
    lines = []
    for condition in resource.get('status', {}).get('conditions', []):
        title = ' · '.join(str(condition.get(key) or '').strip() for key in ('type', 'status') if condition.get(key))
        details = ': '.join(str(condition.get(key) or '').strip() for key in ('reason', 'message') if condition.get(key))
        line = ': '.join(part for part in (title, details) if part)
        if line.strip(): lines.append(line)
    return '\n'.join(lines) or fallback
