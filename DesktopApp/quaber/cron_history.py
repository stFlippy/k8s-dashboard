"""UID-based CronJob history shared by matrix navigation and log selection."""
def owned_by(resource, kind, uid):
    return bool(uid) and any(o.get('controller') and o.get('kind') == kind and o.get('uid') == uid
                             for o in resource.get('metadata', {}).get('ownerReferences', []))


def job_order(job):
    from datetime import datetime, timezone
    meta = job.get('metadata', {})
    stamp = meta.get('annotations', {}).get('batch.kubernetes.io/cronjob-scheduled-timestamp') or meta.get('creationTimestamp', '')
    try: return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, TypeError): return datetime.min.replace(tzinfo=timezone.utc)


def latest_available_pod(cron_uid, jobs, pods):
    by_uid = {j.get('metadata', {}).get('uid'): j for j in jobs if owned_by(j, 'CronJob', cron_uid)}
    matches = []
    for pod in pods:
        owner = next((o for o in pod.get('metadata', {}).get('ownerReferences', []) if o.get('controller')), {})
        job = by_uid.get(owner.get('uid')) if owner.get('kind') == 'Job' else None
        if job is not None:
            matches.append((job_order(job), job_order(pod), pod.get('metadata', {}).get('name', ''), pod))
    return max(matches, key=lambda item: item[:3])[-1] if matches else None
