"""A CronJob is one workload marker, independent of retained historical Pods."""
from .schedule import last_run
from .widgets import pod_status


def cron_state(resource, scope):
    last = last_run(resource.get('metadata', {}).get('uid'), scope.get('jobs', []))
    if last:
        job, status, started, _ = last
        if status == 'Succeeded' or job.get('status', {}).get('succeeded'):
            return 'Completed', f'Last run: Succeeded · {started}'
        if status == 'Failed':
            return 'Failed', f'Last run: Failed · {started}'
        uid = job.get('metadata', {}).get('uid')
        states = {pod_status(p) for p in scope.get('pods', []) if any(
            o.get('controller') and o.get('kind') == 'Job' and o.get('uid') == uid
            for o in p.get('metadata', {}).get('ownerReferences', []))}
        if 'Failed' in states:
            return 'Failed', f'Last run: Error · {started}'
        if 'Pending' in states:
            return 'Pending', f'Last run: Pending · {started}'
        if 'Running' in states or status == 'Running':
            return 'Running', f'Last run: Running · {started}'
        return 'Pending', f'Last run: Pending · {started}'
    status = resource.get('status', {})
    if status.get('active'):
        return 'Pending', 'Active Job: waiting for current run details.'
    if status.get('lastSuccessfulTime') and status.get('lastSuccessfulTime') == status.get('lastScheduleTime'):
        return 'Completed', 'Last run: Succeeded · ' + status['lastSuccessfulTime']
    if resource.get('spec', {}).get('suspend'):
        return 'Unknown', 'Schedule suspended.'
    return 'Scheduled', 'Last run: Unknown — history is unavailable.'
