"""Controlled backend used underneath the production PanelBackground."""
from dataclasses import dataclass
import itertools
from quaber.request_policy import request_intent
from quaber.monitoring import MonitoringCancelled


_missing = object()


@dataclass
class DeferredJob:
    id: int
    function: object
    done: object
    failed: object
    options: dict
    intent: object
    status: str = 'QUEUED'

    def __iter__(self):
        return iter((self.function, self.done, self.failed))

    def __getitem__(self, index):
        return tuple(self)[index]


class DeferredBackend:
    def __init__(self):
        self.jobs = []
        self.history = []
        self.admit = True
        self.stopped = False
        self._ids = itertools.count(1)

    def submit(self, function, done=None, failed=None, *, key=None, priority=False,
               monitor=True, demand='card', cancelled=None, finalized=None, **options):
        if not self.admit or self.stopped:
            return False
        intent = request_intent.get()
        options.update(key=key, priority=priority or intent.priority, monitor=monitor,
                       demand=demand, cancelled=cancelled, finalized=finalized)
        self.jobs.append(DeferredJob(next(self._ids), function, done, failed, options, intent))
        return True

    def finish(self, value=None, failure=None, execute=False, *, job_id=None, cancel=False):
        job = next((j for j in self.jobs if j.id == job_id), None) if job_id is not None else self.jobs[0]
        if job is None: raise LookupError(job_id)
        self.jobs.remove(job)
        self.history.append(job)
        outcome = 'CANCELLED' if cancel else 'FAILED' if failure is not None else 'ACCEPTED'
        try:
            if execute and not cancel and failure is None:
                reset = request_intent.set(job.intent)
                try:
                    value = job.function()
                except MonitoringCancelled:
                    outcome = 'CANCELLED'
                except Exception as exc:
                    failure, outcome = str(exc), 'FAILED'
                finally:
                    request_intent.reset(reset)
            job.status = outcome
            if outcome == 'CANCELLED':
                callback = job.options['cancelled']
                if callback: callback()
            elif outcome == 'FAILED':
                if job.failed: job.failed(failure)
            elif job.done: job.done(value)
        finally:
            job.status = outcome
            if job.options['finalized']: job.options['finalized'](outcome)
        return job

    def deliver_stale(self, job, *, value=None, failure=None):
        # Deliberately attempt late adoption through the real owner guard.
        callback = job.failed if failure is not None else job.done
        if callback: callback(failure if failure is not None else value)

    def cancel_monitoring(self):
        for job in list(self.jobs):
            if job.options['monitor']: self.finish(job_id=job.id, cancel=True)

    def stop(self):
        self.stopped = True
        for job in list(self.jobs): self.finish(job_id=job.id, cancel=True)
