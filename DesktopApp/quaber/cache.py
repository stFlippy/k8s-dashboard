"""Bounded single-flight cache for expensive read-only API snapshots."""
import threading
import time
import json
from collections import OrderedDict
from concurrent.futures import Future

MISSING = object()


class SnapshotCache:
    def __init__(self, ttl=5, capacity=128, clock=time.monotonic):
        self.ttl, self.capacity = ttl, capacity
        self.clock = clock
        self.epoch = 0
        self.versions = {}
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.active = {}
        self.active_tokens = {}
        self.weights = {}
        self.total_weight = 0
        self.projections = {}
        self.stats = {'hits': 0, 'misses': 0, 'joined': 0}

    def get(self, key, load, *, ttl=None, max_age=None):
        from .monitoring import _read_token, MonitoringCancelled
        token = _read_token.get()
        def check():
            if token is not None and token.canceled.is_set():
                raise MonitoringCancelled('Canceled snapshot is not publishable.')
        check()
        with self.lock:
            generation = (self.epoch, self.versions.get(key, 0))
            active_key = (generation, key)
            entry = self.entries.get(key)
            if entry and entry[0] > self.clock() and (entry[2] or max_age is None or self.clock()-entry[3] < max_age):
                self.stats['hits'] += 1
                self.entries.move_to_end(key)
                if entry[2]:
                    raise entry[1]
                return entry[1]
            future = self.active.get(active_key)
            origin = self.active_tokens.get(active_key)
            if origin is not None and origin.canceled.is_set(): future = None
            owner = future is None
            if owner:
                if len(self.active) >= self.capacity:
                    raise RuntimeError("Лимит одновременных чтений метрик исчерпан.")
                future = self.active[active_key] = Future()
                self.active_tokens[active_key] = token
                self.stats['misses'] += 1
            else:
                self.stats['joined'] += 1
        if not owner:
            try:
                value = self._wait(future, origin)
            except MonitoringCancelled:
                check()
                return self.get(key, load, ttl=ttl, max_age=max_age)
            check()
            return value
        try:
            started = self.clock()
            value = load()
            check()
            # Encoding large lists must not serialize unrelated cache readers.
            weight = len(value.encode("utf-8")) if isinstance(value, str) else len(json.dumps(value).encode("utf-8"))
        except Exception as exc:
            with self.lock:
                if not isinstance(exc, MonitoringCancelled) and generation == (self.epoch, self.versions.get(key, 0)):
                    self._forget_weight(key)
                    self.entries[key] = (self.clock()+getattr(exc, 'retry_after', 1), exc, True)
                    self.entries.move_to_end(key)
                    self._trim()
                if self.active.get(active_key) is future:
                    self.active.pop(active_key)
                    self.active_tokens.pop(active_key, None)
                future.set_exception(exc)
            raise
        with self.lock:
            if token is not None and token.canceled.is_set():
                if self.active.get(active_key) is future:
                    self.active.pop(active_key)
                    self.active_tokens.pop(active_key, None)
                exc = MonitoringCancelled('Canceled snapshot is not publishable.')
                future.set_exception(exc)
                raise exc
            if generation == (self.epoch, self.versions.get(key, 0)):
                self._forget_weight(key)
                self.entries[key] = (self.clock()+(self.ttl if ttl is None else ttl), value, False, started)
                self.weights[key] = weight
                self.total_weight += weight
                self.entries.move_to_end(key)
                self._trim()
            if self.active.get(active_key) is future:
                self.active.pop(active_key)
                self.active_tokens.pop(active_key, None)
            future.set_result(value)
        return value

    def _wait(self, future, origin=None):
        from concurrent.futures import TimeoutError
        from .monitoring import _read_token, MonitoringCancelled
        deadline = time.monotonic()+65
        while True:
            token = _read_token.get()
            if token is not None and token.canceled.is_set():
                raise MonitoringCancelled('Canceled snapshot subscription.')
            if origin is not None and origin.canceled.is_set():
                raise MonitoringCancelled('Owning read was canceled.')
            try:
                return future.result(timeout=.05)
            except TimeoutError:
                if future.done(): raise
                if time.monotonic() >= deadline: raise

    def existing(self, key, *, max_age=None):
        """Subscribe only to existing complete coverage, without starting a LIST."""
        from .monitoring import _read_token, MonitoringCancelled
        token = _read_token.get()
        if token is not None and token.canceled.is_set():
            raise MonitoringCancelled('Canceled snapshot subscription.')
        with self.lock:
            entry = self.entries.get(key)
            if (entry and not entry[2] and entry[0] > self.clock()
                    and (max_age is None or self.clock()-entry[3] < max_age)):
                self.entries.move_to_end(key)
                self.stats['hits'] += 1
                return entry[1]
            generation = (self.epoch, self.versions.get(key, 0))
            future = self.active.get((generation, key))
            origin = self.active_tokens.get((generation, key))
            if origin is not None and origin.canceled.is_set(): future = None
            if future is not None: self.stats['joined'] += 1
        if future is None: return MISSING
        # All production subscriptions are worker jobs, just like get().
        from PyQt5.QtCore import QCoreApplication, QThread
        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() is app.thread():
            raise RuntimeError('Snapshot subscriptions must wait in a worker.')
        try:
            return self._wait(future, origin)
        except Exception:
            from .monitoring import _read_token, MonitoringCancelled
            token = _read_token.get()
            if token is not None and token.canceled.is_set():
                raise MonitoringCancelled('Canceled snapshot subscription.')
            # Forbidden/unsupported/canceled parent coverage does not forbid an
            # independent namespace LIST with the subscriber's own read token.
            return MISSING

    def _trim(self):
        while self.entries and (len(self.entries) > self.capacity or self.total_weight > 32_000_000):
            key, _ = self.entries.popitem(last=False)
            self._forget_weight(key)

    def _forget_weight(self, key):
        self.total_weight -= self.weights.pop(key, 0)
        self.projections.pop(key, None)

    def weight_of(self, key):
        with self.lock:
            return self.weights.get(key, 0)

    def namespace_projection(self, key, resources, namespace):
        """Index an accepted aggregate snapshot once; never mutate its resources."""
        with self.lock:
            cached = self.projections.get(key)
            if cached is not None and cached[0] is resources:
                return list(cached[1].get(namespace, ()))
        from .collection_reader import namespace_index
        index = namespace_index(resources)
        with self.lock:
            entry = self.entries.get(key)
            if entry and not entry[2] and entry[1] is resources:
                self.projections[key] = (resources, index)
        return list(index.get(namespace, ()))

    def invalidate(self, predicate=None):
        with self.lock:
            if predicate is None:
                self.epoch += 1
                self.entries.clear()
                self.weights.clear()
                self.total_weight = 0
                self.projections.clear()
                self.versions.clear()
            else:
                keys = set(self.entries) | {key for _, key in self.active}
                for key in keys:
                    if predicate(key):
                        self.versions[key] = self.versions.get(key, 0)+1
                        self.entries.pop(key, None)
                        self._forget_weight(key)
                active = {key for _, key in self.active}
                self.versions = {key: version for key, version in self.versions.items() if key in self.entries or key in active}
