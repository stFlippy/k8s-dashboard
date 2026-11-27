"""Per-process physical dispatch budget. Waits are cancelable worker-only."""
import hashlib
import json
import random
import threading
import time
import contextvars
import math
import urllib.error
import logging
import sqlite3
from collections import deque
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, asdict, replace


@dataclass(frozen=True)
class RequestLimits:
    inflight: int | None = None
    cluster_inflight: int = 1
    monitoring_inflight: int | None = None
    global_rate: float | None = None
    global_burst: float | None = None
    cluster_rate: float = 2.
    cluster_burst: float = 2.
    queue: int = 48
    namespaces_ttl: int = 60
    capability_ttl: int = 300
    idle_minutes: int = 30
    foreground_seconds: int = 5


LIMITS = RequestLimits()


@dataclass(frozen=True)
class RequestIntent:
    priority: bool = False
    fresh: bool = False
    purpose: str = 'poll'


request_intent = contextvars.ContextVar('request_intent', default=RequestIntent())
_response_account = contextvars.ContextVar('response_account', default=None)


@contextmanager
def manual_read():
    token = request_intent.set(RequestIntent(True, True, 'manual'))
    try:
        yield
    finally:
        request_intent.reset(token)

credential_bindings = contextvars.ContextVar('request_credential_bindings', default={})


@contextmanager
def expected_credentials(bindings):
    token = credential_bindings.set(bindings)
    try:
        yield
    finally:
        credential_bindings.reset(token)


def credentials_revision(cluster):
    values = tuple(cluster.get(k) for k in ('server', 'username', 'encryptedKubeconfig', 'updatedAt', 'enabled', 'accountId', 'accountRevision'))
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()


class RequestDeferred(RuntimeError):
    pass


class RequestPolicy:
    def __init__(self, clock=time.monotonic, wall=time.time, wait=None, jitter=random.uniform, limits=LIMITS, endpoint_budget=None):
        self.limits = limits
        self.clock, self.wall = clock, wall
        self.condition = threading.Condition()
        self.wait = wait or self.condition.wait
        self.jitter = jitter
        self.global_capacity = (limits.global_burst if limits.global_burst is not None else
                                max(1., limits.global_rate) if limits.global_rate is not None else math.inf)
        self.global_tokens, self.global_at = self.global_capacity, clock()
        self.clusters = {}
        self.inflight = self.monitoring = 0
        self.downloads = 0
        self.last_cluster = None
        self.queue = deque()
        self.stats = dict(attempts=0, success=0, errors=0, canceled=0, bytes=0, peak=0)
        self.by_cluster = {}
        self.by_purpose = {}
        self.statuses = {}
        self.endpoint_budget = endpoint_budget
        self.endpoints = {}
        self.heavy = 0
        self.heavy_tickets = set()
        self.endpoint_ready_after = {}

    def bind_endpoint(self, cid, server):
        from .endpoint_budget import endpoint_key
        key = endpoint_key(server)
        if key:
            with self.condition:
                self.endpoints[cid] = key

    @property
    def cluster_concurrency(self):
        return getattr(self, '_cluster_concurrency', self.limits.cluster_inflight)

    def set_cluster_concurrency(self, value):
        with self.condition:
            self._cluster_concurrency = value
            if self.endpoint_budget is not None:
                self.endpoint_budget.max_inflight = max(2, value)
            self.condition.notify_all()

    def set_cluster_rate(self, value):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError('Допустимо от 1 до 100 запросов/с на кластер.')
        with self.condition:
            # Preserve earned tokens; changing a setting must not grant a burst.
            for cluster in self.clusters.values():
                self._refill(cluster)
            self.limits = replace(self.limits, cluster_rate=float(value))
            if self.endpoint_budget is not None:
                self.endpoint_budget.rate = float(value)
            self.condition.notify_all()

    def dispatched(self, cid):
        with self.condition:
            self.stats['attempts'] += 1
            purpose = request_intent.get().purpose
            self.by_cluster[cid] = self.by_cluster.get(cid, 0)+1
            self.by_purpose[purpose] = self.by_purpose.get(purpose, 0)+1

    def diagnostics(self):
        """Local counters only: no URL, credentials, payload or command text."""
        with self.condition:
            return dict(self.stats, inflight=self.inflight, monitoring=self.monitoring,
                        queued=len(self.queue), downloads=self.downloads,
                        by_cluster=dict(self.by_cluster), by_purpose=dict(self.by_purpose),
                        by_status=dict(self.statuses), heavy=self.heavy,
                        endpoint_coordination=self.endpoint_budget is not None,
                        limits=dict(asdict(self.limits), cluster_inflight=self.cluster_concurrency))

    def received(self, size):
        with self.condition:
            self.stats['bytes'] += size
        account = _response_account.get()
        if account is not None:
            account[0] += size

    def status(self, status):
        with self.condition:
            self.statuses[status] = self.statuses.get(status, 0)+1

    def _cluster(self, cid):
        return self.clusters.setdefault(cid, dict(tokens=self.limits.cluster_burst, at=self.clock(), active=0,
                                                  until=0., failures=0, probe=False, heavy=0, downloads=0))

    def _refill(self, cluster):
        now = self.clock()
        if self.limits.global_rate is not None:
            self.global_tokens = min(self.global_capacity, self.global_tokens+self.limits.global_rate*(now-self.global_at))
        self.global_at = now
        rate=min(2.,self.limits.cluster_rate) if cluster.get('degraded') else self.limits.cluster_rate
        cluster['tokens'] = min(self.limits.cluster_burst, cluster['tokens']+rate*(now-cluster['at']))
        cluster['at'] = now

    def _next_ticket(self, candidates):
        if not candidates: return None
        # Preserve FIFO inside each cluster and rotate across all ready clusters,
        # including C when A/B both have long queues. Writes keep their reserve.
        monitor = min(entry[2] for entry in candidates)
        candidates = [entry for entry in candidates if entry[2] == monitor]
        priority = max(entry[4] if len(entry) > 4 else False for entry in candidates)
        candidates = [entry for entry in candidates if (entry[4] if len(entry) > 4 else False) == priority]
        order = list(self.clusters)
        start = (order.index(self.last_cluster)+1) % len(order) if self.last_cluster in order else 0
        ready = {entry[1] for entry in candidates}
        cid = next(order[(start+i) % len(order)] for i in range(len(order))
                   if order[(start+i) % len(order)] in ready)
        return next(entry[0] for entry in candidates if entry[1] == cid)

    @contextmanager
    def slot(self, cid, check, *, monitor=True, deadline=None, long_download=False, heavy=False):
        deadline = self.clock()+30 if deadline is None else deadline
        ticket = object()
        acquired = False
        lease, endpoint = None, self.endpoints.get(cid)
        with self.condition:
            if len(self.queue) >= self.limits.queue:
                raise RequestDeferred('Physical request queue is full.')
            entry = (ticket, cid, monitor, long_download, request_intent.get().priority)
            self.queue.append(entry)
            if heavy:
                self.heavy_tickets.add(ticket)
            try:
                while True:
                    check()
                    cluster = self._cluster(cid)
                    self._refill(cluster)
                    if monitor and cluster['until'] > self.clock():
                        raise RequestDeferred('Cluster cooldown is active; skip/coalesce this monitor tick.')
                    candidates = [(t, c, m, d, p) for t, c, m, d, p in self.queue
                                  if self._cluster(c)['active'] < self.cluster_concurrency
                                  and not (self._cluster(c).get('draining') and self._cluster(c)['active'])
                                  and self._cluster(c)['until'] <= self.clock()
                                  and not self._cluster(c)['probe']
                                  and self._cluster(c)['tokens'] >= 1
                                  and (not m or self.limits.monitoring_inflight is None or self.monitoring < self.limits.monitoring_inflight)
                                  and (t not in self.heavy_tickets or self._cluster(c).get('heavy', 0) == 0)
                                  and self.endpoint_ready_after.get(self.endpoints.get(c), 0) <= self.clock()
                                  and (not d or self._cluster(c).get('downloads', 0) == 0)]
                    # A waiting monitor cannot block the reserved write slot.
                    # Among ready clusters, alternate rather than draining one queue.
                    eligible = self._next_ticket(candidates)
                    if (eligible is ticket and (self.limits.inflight is None or self.inflight < self.limits.inflight) and cluster['active'] < self.cluster_concurrency
                            and (not monitor or self.limits.monitoring_inflight is None or self.monitoring < self.limits.monitoring_inflight)
                            and self.global_tokens >= 1 and cluster['tokens'] >= 1
                            and (not heavy or cluster.get('heavy', 0) == 0)
                            and cluster['until'] <= self.clock()):
                        check()
                        if self.endpoint_budget is not None and endpoint:
                            try:
                                lease = self.endpoint_budget.acquire(endpoint, monitor=monitor, heavy=heavy, **({'rate':2.} if cluster.get('degraded') else {}))
                            except (OSError, sqlite3.Error) as exc:
                                raise RequestDeferred('Shared endpoint budget is temporarily unavailable.') from exc
                            if lease is None:
                                self.endpoint_ready_after[endpoint] = self.clock() + .05
                                if self.clock() >= deadline:
                                    raise RequestDeferred('Shared endpoint budget deadline reached.')
                                from PyQt5.QtCore import QCoreApplication, QThread
                                app = QCoreApplication.instance()
                                if app is not None and QThread.currentThread() is app.thread():
                                    raise RequestDeferred('A shared endpoint wait must run in a worker.')
                                self.wait(min(.05, max(0., deadline-self.clock())))
                                continue
                            self.endpoint_ready_after.pop(endpoint, None)
                        self.global_tokens -= 1
                        cluster['tokens'] -= 1
                        cluster['active'] += 1
                        cluster['heavy'] = cluster.get('heavy', 0) + int(heavy)
                        cluster['downloads'] = cluster.get('downloads', 0) + int(long_download)
                        if cluster['failures'] >= 3:
                            cluster['probe'] = True
                        self.inflight += 1
                        self.monitoring += int(monitor)
                        self.downloads += int(long_download)
                        self.heavy += int(heavy)
                        self.last_cluster = cid
                        self.stats['peak'] = max(self.stats['peak'], self.inflight)
                        acquired = True
                        generation=cluster.get('generation',0)
                        break
                    if self.clock() >= deadline:
                        raise RequestDeferred('Request budget/cooldown deadline reached.')
                    # A caller must queue work rather than block Qt's GUI thread.
                    from PyQt5.QtCore import QCoreApplication, QThread
                    app = QCoreApplication.instance()
                    if app is not None and QThread.currentThread() is app.thread():
                        raise RequestDeferred('A waiting physical request must run in a worker.')
                    self.wait(min(.05, max(0., deadline-self.clock())))
            except Exception:
                self.stats['canceled'] += 1
                raise
            finally:
                self.queue.remove(entry)
                self.heavy_tickets.discard(ticket)
        account = [0]
        reset = _response_account.set(account)
        try:
            check()
            yield generation
        except urllib.error.HTTPError as exc:
            self.failed(cid, exc.code, exc.headers.get('Retry-After') if exc.headers else None, generation=generation)
            raise
        except (urllib.error.URLError, TimeoutError):
            self.failed(cid,generation=generation)
            raise
        finally:
            _response_account.reset(reset)
            if lease is not None:
                try:
                    self.endpoint_budget.release(endpoint, lease, account[0])
                except (OSError, sqlite3.Error):
                    # Never turn a successful mutation into a retryable failure.
                    # A crashed/failed lease expires and is recovered by peers.
                    logging.getLogger(__name__).warning('Endpoint budget lease cleanup deferred.')
            if acquired:
                with self.condition:
                    cluster['active'] -= 1
                    cluster['heavy'] -= int(heavy)
                    cluster['downloads'] -= int(long_download)
                    if cluster['active'] == 0:
                        cluster['probe'] = False
                        cluster['draining'] = False
                    self.inflight -= 1
                    self.monitoring -= int(monitor)
                    self.downloads -= int(long_download)
                    self.heavy -= int(heavy)
                    self.condition.notify_all()

    def failed(self, cid, status=None, retry_after=None, *, generation=None):
        with self.condition:
            self.stats['errors'] += 1
        if status is not None and status != 429 and status < 500:
            return
        with self.condition:
            cluster = self._cluster(cid)
            if generation is not None and generation!=cluster.get('generation',0):return
            cluster['failures'] += 1
            cluster['healthy_reads']=0
            if not cluster.get('degraded'):
                cluster['degraded']=True
                cluster['generation']=cluster.get('generation',0)+1
                cluster['draining']=bool(cluster['active'])
                cluster['tokens']=min(cluster['tokens'],1.)
                self.condition.notify_all()
                if not (status == 429 and retry_after):
                    return
            cluster['probe'] = False
            cluster['generation']=cluster.get('generation',0)+1
            cluster['draining']=bool(cluster['active'])
            delay = min(120., min(60., 5.*2**min(4, cluster['failures']-1))*self.jitter(.8, 1.2))
            if status == 429 and retry_after:
                try:
                    seconds = float(retry_after)
                    if math.isfinite(seconds) and seconds >= 0:
                        delay = max(delay, seconds)
                except (ValueError, TypeError):
                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp()-self.wall())
                    except (ValueError, TypeError, OverflowError):
                        pass
            cluster['until'] = max(cluster['until'], self.clock()+delay)

    def succeeded(self, cid, size=0, *, generation=None):
        with self.condition:
            cluster=self._cluster(cid)
            self.stats['success'] += 1
            self.stats['bytes'] += size
            if generation is not None and generation!=cluster.get('generation',0):return
            cluster['healthy_reads']=cluster.get('healthy_reads',0)+1
            if cluster.get('degraded') and cluster['healthy_reads']>=3:
                cluster['degraded']=False
                cluster['failures']=0
                cluster['generation']=cluster.get('generation',0)+1
                cluster['draining']=bool(cluster['active'])
            self._cluster(cid)['probe'] = False

    def is_degraded(self,cid):
        with self.condition:return bool(self._cluster(cid).get('degraded'))
