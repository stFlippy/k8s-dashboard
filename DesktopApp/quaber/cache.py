"""Bounded single-flight cache for expensive read-only API snapshots."""
import threading
import time
import json
from collections import OrderedDict
from concurrent.futures import Future


class SnapshotCache:
    def __init__(self, ttl=5, capacity=128):
        self.ttl, self.capacity = ttl, capacity
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.active = {}
        self.weights = {}

    def get(self, key, load):
        with self.lock:
            entry = self.entries.get(key)
            if entry and entry[0] > time.monotonic():
                if entry[2]:
                    raise entry[1]
                return entry[1]
            future = self.active.get(key)
            owner = future is None
            if owner:
                if len(self.active) >= self.capacity:
                    raise RuntimeError("Лимит одновременных чтений метрик исчерпан.")
                future = self.active[key] = Future()
        if not owner:
            return future.result(timeout=35)
        try:
            value = load()
        except Exception as exc:
            with self.lock:
                self.entries[key] = (time.monotonic()+1, exc, True)
                self._trim()
                self.active.pop(key)
                future.set_exception(exc)
            raise
        with self.lock:
            self.entries[key] = (time.monotonic()+self.ttl, value, False)
            self.weights[key] = len(value.encode("utf-8")) if isinstance(value, str) else len(json.dumps(value).encode("utf-8"))
            self.entries.move_to_end(key)
            self._trim()
            self.active.pop(key)
            future.set_result(value)
        return value

    def _trim(self):
        while self.entries and (len(self.entries) > self.capacity or sum(self.weights.values()) > 32_000_000):
            key, _ = self.entries.popitem(last=False)
            self.weights.pop(key, None)

    def invalidate(self):
        with self.lock:
            self.entries.clear()
            self.weights.clear()
