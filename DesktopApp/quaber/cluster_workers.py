"""Configurable cluster I/O, defaulting to one worker per cluster."""
import contextvars
import threading
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, TimeoutError

_creation = threading.Lock()


class ClusterWorkers:
    def __init__(self, workers=1):
        self.lock = threading.Condition()
        self.pools = {}
        self.workers = workers
        self.local = threading.local()
        self.closed = False

    def configure(self, workers):
        with self.lock:
            self.workers = workers
            self.lock.notify_all()

    def run(self, key, function, *, permit=None):
        if getattr(self.local, 'key', None) == key:
            return function()
        context = contextvars.copy_context()
        delegated = permit.delegate_dispatch() if permit is not None else None
        def execute():
            self.local.key = key
            try:
                def invoke():
                    with delegated() if delegated is not None else nullcontext():
                        return function()
                return context.run(invoke)
            finally:
                self.local.key = None
        from .monitoring import _read_token, MonitoringCancelled
        token = _read_token.get()
        with self.lock:
            while True:
                if self.closed:
                    raise RuntimeError('I/O workers are closed.')
                entry = self.pools.get(key)
                if entry is None or entry['workers'] == self.workers:
                    break
                if not entry['outstanding']:
                    if entry['pool'] is not None: entry['pool'].shutdown(wait=False)
                    entry = None
                    break
                if token is not None and token.canceled.is_set():
                    raise MonitoringCancelled('Canceled while resizing cluster I/O.')
                self.lock.wait(.05)
            if entry is None:
                entry = self.pools[key] = dict(workers=self.workers, outstanding=0,
                    pool=ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='quaber-cluster') if self.workers else None)
            entry['outstanding'] += 1
            def settled(_):
                with self.lock:
                    entry['outstanding'] -= 1
                    self.lock.notify_all()
            if entry['pool'] is not None:
                try:
                    future = entry['pool'].submit(execute)
                except Exception:
                    entry['outstanding'] -= 1
                    self.lock.notify_all()
                    raise
                future.add_done_callback(settled)
            else:
                future = None
        if future is None:
            try: return execute()
            finally: settled(None)
        while True:
            try:
                return future.result(timeout=.05)
            except TimeoutError:
                if future.done():
                    raise
                # A dispatched write must settle before releasing its owner.
                from .write_guard import current_permit
                if token is not None and token.canceled.is_set() and current_permit() is None:
                    future.cancel()
                    raise MonitoringCancelled('Canceled cluster I/O subscription.')

    def close(self):
        with self.lock:
            self.closed = True
            pools, self.pools = list(self.pools.values()), {}
            self.lock.notify_all()
        for entry in pools:
            if entry['pool'] is not None: entry['pool'].shutdown(wait=True, cancel_futures=True)


def configured_threads(api):
    value = getattr(api, 'cluster_threads', 1)
    return value if type(value) is int and 0 <= value <= 50 else 1


def configure_cluster_threads(api, workers):
    if type(workers) is not int or not 0 <= workers <= 50:
        raise ValueError('Допустимо от 0 до 50 потоков на кластер.')
    with _creation:
        api.cluster_threads = workers
        if hasattr(api, '_io_workers'): api._io_workers.configure(workers)
        api.request_policy.set_cluster_concurrency(max(1, workers))


def cluster_io(api, cid, function, *, permit=None):
    with _creation:
        if not hasattr(api, '_io_workers'):
            api._io_workers = ClusterWorkers(configured_threads(api))
    return api._io_workers.run(cid, function, permit=permit)
