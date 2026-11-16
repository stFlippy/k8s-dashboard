"""Build namespace owner indexes once, shared by rendering and selectors."""
from collections import defaultdict


def index_scope(scope):
    """Index all visible workloads; resolve ownership by UID, never name prefixes."""
    deployments = {d.get("metadata", {}).get("name"): dict(d, kind="Deployment") for d in scope.get("deployments", [])}
    sets = {r.get("metadata", {}).get("uid"): next((o.get("uid") for o in r.get("metadata", {}).get("ownerReferences", [])
             if o.get("controller") and o.get("kind") == "Deployment"), None) for r in scope.get("replica_sets", [])}
    parent_refs = {r.get("metadata", {}).get("uid"): next((o for o in r.get("metadata", {}).get("ownerReferences", [])
                   if o.get("controller") and o.get("kind") in ("Deployment", "CronJob")), {})
                   for r in scope.get("replica_sets", []) + scope.get("jobs", [])}
    kinds = (("stateful_sets", "StatefulSet"), ("daemon_sets", "DaemonSet"),
             ("replica_sets", "ReplicaSet"), ("replication_controllers", "ReplicationController"),
             ("jobs", "Job"), ("cron_jobs", "CronJob"))
    cron_for_job = {j.get("metadata", {}).get("uid"): next((o.get("uid") for o in j.get("metadata", {}).get("ownerReferences", [])
                    if o.get("controller") and o.get("kind") == "CronJob"), None) for j in scope.get("jobs", [])}
    by_uid = {d.get("metadata", {}).get("uid"): d for d in deployments.values()}
    for field, kind in kinds:
        for resource in scope.get(field, []):
            uid = resource.get("metadata", {}).get("uid")
            if (kind == "ReplicaSet" and sets.get(uid)) or (kind == "Job" and cron_for_job.get(uid)):
                continue
            key = f"{kind}/{resource.get('metadata', {}).get('name', '')}"
            deployments[key] = dict(resource, kind=kind)
            by_uid[uid] = deployments[key]
    pods = defaultdict(list)
    for pod in scope.get("pods", []):
        owner = next((o for o in pod.get("metadata", {}).get("ownerReferences", []) if o.get("controller")), {})
        uid = owner.get("uid")
        if owner.get("kind") == "ReplicaSet":
            uid = sets.get(uid) or uid
        elif owner.get("kind") == "Job":
            uid = cron_for_job.get(uid) or uid
        if not owner or owner.get("kind") == "Node":
            uid = pod.get("metadata", {}).get("uid") or "pod/"+pod.get("metadata", {}).get("name", "")
            key = f"Pod/{pod.get('metadata', {}).get('name', '')}"
            deployments[key] = dict(pod, kind="Pod")
            by_uid[uid] = deployments[key]
        elif uid not in by_uid:
            # Owner metadata can still be shown when list permission is absent.
            resolved = parent_refs.get(owner.get("uid")) or owner
            kind, name = resolved.get("kind", "Owner"), resolved.get("name", "")
            key = name if kind == "Deployment" else f"{kind}/{name}"
            deployments[key] = {"kind": kind, "metadata": {"uid": uid, "name": name}, "_unresolved": True}
            by_uid[uid] = deployments[key]
        if uid:
            pods[uid].append(pod)
    return deployments, pods


WORKLOAD_ORDER = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job", "CronJob", "Pod")
OPTIONAL_LISTS = (("stateful_sets", "statefulset"), ("daemon_sets", "daemonset"),
                  ("replication_controllers", "replicationcontroller"), ("jobs", "job"), ("cron_jobs", "cronjob"))


def empty_workload(resource, pods):
    kind = resource.get("kind", "Deployment")
    spec, status = resource.get("spec", {}), resource.get("status", {})
    if pods:
        return "", False
    if resource.get("metadata", {}).get("deletionTimestamp"):
        return "Удаляется", False
    if kind == "CronJob":
        if spec.get("suspend"):
            return "Приостановлен", False
        if status.get("active"):
            return "Job без Pod", True
        return "Ожидание запуска", False
    if kind == "Job":
        if spec.get("suspend") or spec.get("parallelism") == 0:
            return "Приостановлен", False
        if any(c.get("type") == "Complete" and c.get("status") == "True" for c in status.get("conditions", [])):
            return "Завершён", False
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in status.get("conditions", [])):
            return "Ошибка Job", True
        return "Нет Pod", True
    target = status.get("desiredNumberScheduled", 0) if kind == "DaemonSet" else spec.get("replicas", 1)
    if resource.get("_unresolved"):
        return "Нет данных", False
    if kind == "DaemonSet" and target == 0:
        return "Нет назначений", False
    return ("0 (scale=0)", False) if target == 0 else (f"0/{target}\nНет Pod", True)
