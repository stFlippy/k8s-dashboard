"""Display-only Pod partitions and owner-object counts; no snapshot mutation."""
from collections import Counter
from .pod_recency import recent_activity_deadline
from .resource_index import empty_workload
from .widgets import pod_status

PARTITIONS = ('Running', 'Recent', 'Pending', 'Failed', 'Unknown', 'Completed', 'Terminating', 'Other')


def classify_pod(pod, now):
    status = pod_status(pod)
    if status == 'Running' and recent_activity_deadline(pod, now) is not None:
        return 'Recent'
    return status if status in PARTITIONS else 'Other'


def pod_key(pod):
    meta = pod.get('metadata', {})
    return ('uid', meta['uid']) if meta.get('uid') else ('name', meta.get('name', ''))


def workload_uid(resource):
    meta = resource.get('metadata', {})
    return meta.get('uid') or ('pod/'+meta.get('name', '') if resource.get('kind') == 'Pod' else None)


def batch_origin(pod, scope, jobs_by_uid=None):
    """Controller identity only; never infer ownership from an object name."""
    owner = next((o for o in pod.get('metadata', {}).get('ownerReferences', []) if o.get('controller')), {})
    if owner.get('kind') != 'Job':
        return None
    if jobs_by_uid is None:
        jobs_by_uid = {j.get('metadata', {}).get('uid'): j for j in scope.get('jobs', []) if j.get('metadata', {}).get('uid')}
    job = jobs_by_uid.get(owner.get('uid'))
    if job is None:
        return 'Unknown Job'
    parent = next((o for o in job.get('metadata', {}).get('ownerReferences', []) if o.get('controller')), {})
    if parent.get('uid') in jobs_by_uid: return 'Unknown Job'
    return 'CronJob' if parent.get('kind') == 'CronJob' and parent.get('uid') else 'Job'


def _failed_job(job):
    status = job.get('status', {})
    return bool(status.get('failed')) or any(c.get('type') == 'Failed' and c.get('status') == 'True'
                                           for c in status.get('conditions', []))


def scope_summary(scope, index, selected, now):
    """One namespace, with local workload/search scope applied before status filters."""
    entries, pods_index = index
    jobs_by_uid = {j.get('metadata', {}).get('uid'): j for j in scope.get('jobs', []) if j.get('metadata', {}).get('uid')}
    failed_cron = {o.get('uid') for job in jobs_by_uid.values() if _failed_job(job)
                   for o in job.get('metadata', {}).get('ownerReferences', [])
                   if o.get('controller') and o.get('kind') == 'CronJob'}
    result = {'pods': Counter({k: 0 for k in PARTITIONS}), 'batch': Counter(),
              'workloads': Counter(), 'batch_failed': Counter(), 'issues': 0,
              'unavailable': set(scope.get('_type_errors', {})), 'stale': bool(scope.get('_stale'))}
    seen_pods, seen_owners = set(), set()
    for name, resource in entries.items():
        if name not in selected:
            continue
        kind = resource.get('kind', 'Deployment')
        uid = workload_uid(resource)
        pods = pods_index.get(uid, [])
        if not resource.get('_unresolved'):
            identity = (kind, uid or resource.get('metadata', {}).get('name'))
            if identity not in seen_owners:
                seen_owners.add(identity)
                result['workloads'][kind] += 1
                if empty_workload(resource, pods)[1]: result['issues'] += 1
                if kind in ('Job', 'CronJob'):
                    failed = any(pod_status(p) == 'Failed' for p in pods)
                    if kind == 'Job': failed |= _failed_job(resource)
                    else:
                        failed |= uid in failed_cron
                    if failed: result['batch_failed'][kind] += 1
        else:
            result['unavailable'].add(kind)
        for pod in pods:
            identity = pod_key(pod)
            if identity in seen_pods: continue
            seen_pods.add(identity)
            origin = batch_origin(pod, scope, jobs_by_uid)
            if origin:
                result['batch'][classify_pod(pod, now)] += 1
                if origin == 'Unknown Job': result['unavailable'].add('batch owner')
            else:
                result['pods'][classify_pod(pod, now)] += 1
    return result
