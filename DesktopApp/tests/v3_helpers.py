"""Physical fake HTTP boundary and deterministic clock; no secrets/network."""
import io
import json
import threading
import urllib.error
from urllib.parse import urlsplit


class FakeClock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class FakeHTTP:
    def __init__(self, route, clock):
        self.route, self.clock = route, clock
        self.calls, self.inflight, self.peak = [], 0, 0
        self.lock = threading.Lock()
    def open(self, request, **kwargs):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            record = dict(path=request.selector, cluster=urlsplit(request.full_url).hostname,
                          method=request.get_method(), start=self.clock())
            self.calls.append(record)
        try:
            status, headers, payload = self.route(request)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            record.update(status=status, bytes=len(data))
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, 'synthetic', headers, io.BytesIO(data))
            return io.BytesIO(data)
        finally:
            with self.lock:
                record['end'] = self.clock()
                self.inflight -= 1
