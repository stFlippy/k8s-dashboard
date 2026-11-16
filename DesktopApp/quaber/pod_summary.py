"""Deployment ring groups use controller UID and ReplicaSet revisions."""

def deployment_rings(resource, pods, replica_sets):
    uid = resource.get("metadata", {}).get("uid")
    owned = [rs for rs in replica_sets if any(o.get("controller") and o.get("uid") == uid
             for o in rs.get("metadata", {}).get("ownerReferences", []))]
    target = resource.get("spec", {}).get("replicas", 1)
    result = {"previous": [], "current": pods, "previous_target": 0, "current_target": target, "rolling": False}
    if len(owned) < 2:
        return result
    def revision(rs):
        metadata = rs.get("metadata", {})
        try:
            number = int(metadata.get("annotations", {}).get("deployment.kubernetes.io/revision", 0))
        except (ValueError, TypeError):
            number = 0
        return number, metadata.get("creationTimestamp", ""), metadata.get("uid", "")
    latest = max(owned, key=revision)
    latest_uid = latest.get("metadata", {}).get("uid")
    def owner(pod):
        return next((o.get("uid") for o in pod.get("metadata", {}).get("ownerReferences", []) if o.get("controller")), None)
    previous_sets = [rs for rs in owned if rs is not latest and (rs.get("spec", {}).get("replicas", 0) > 0 or
                     any(owner(p) == rs.get("metadata", {}).get("uid") and
                         (p.get("metadata", {}).get("deletionTimestamp") or p.get("status", {}).get("phase") not in ("Succeeded", "Failed")) for p in pods))]
    if not previous_sets:
        return result
    previous_uids = {rs["metadata"]["uid"] for rs in previous_sets}
    return {"previous": [p for p in pods if owner(p) in previous_uids],
            "current": [p for p in pods if owner(p) == latest_uid],
            "previous_target": sum(rs.get("spec", {}).get("replicas", 0) for rs in previous_sets),
            "current_target": latest.get("spec", {}).get("replicas", 0), "rolling": True}
