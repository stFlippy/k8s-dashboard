"""Best-effort exec preflight identity; Kubernetes exec has no instance CAS."""
from dataclasses import dataclass,asdict


@dataclass(frozen=True)
class ContainerInstance:
    pod_uid: str
    name: str
    category: str
    container_id: str
    restart_count: int
    started_at: str | None

    def parameters(self):
        return asdict(self)


def capture_instance(pod, name):
    metadata,status = pod.get('metadata',{}),pod.get('status',{})
    if (metadata.get('deletionTimestamp') or status.get('phase') not in ('Running','Pending')
            or not isinstance(metadata.get('uid'),str) or not metadata['uid']):
        raise PermissionError('Pod не находится в подходящем состоянии для Terminal. Обновите карточку.')
    for category,statuses in (('containers','containerStatuses'),('initContainers','initContainerStatuses'),
                              ('ephemeralContainers','ephemeralContainerStatuses')):
        if not any(c.get('name')==name for c in pod.get('spec',{}).get(category,[])):
            continue
        item = next((c for c in status.get(statuses,[]) if c.get('name')==name),{})
        container_id,restarts,running = item.get('containerID'),item.get('restartCount'),item.get('state',{}).get('running')
        if (not isinstance(container_id,str) or not container_id or type(restarts) is not int or restarts<0
                or not isinstance(running,dict) or set(item.get('state',{}))!={'running'}
                or running.get('startedAt') is not None and not isinstance(running.get('startedAt'),str)):
            raise PermissionError('Текущий запущенный экземпляр контейнера не подтверждён. Обновите карточку.')
        return ContainerInstance(metadata['uid'],name,category,container_id,restarts,running.get('startedAt'))
    raise PermissionError('Выбранный контейнер больше не существует в Pod. Обновите карточку.')
