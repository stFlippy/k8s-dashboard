"""Bounded in-memory samples, independent of the displayed graph period."""
from collections import OrderedDict, deque
import time


class MetricHistory:
    retention_seconds = 30 * 60

    def __init__(self):
        self.series = OrderedDict()

    def append(self, identity, containers, now=None):
        now = time.time() if now is None else now
        for container in containers:
            key = (*identity, container["name"])
            rows = self.series.setdefault(key, deque(maxlen=720))
            rows.append((now, container.get("cpu", {}).get("usageCores"),
                         container.get("memory", {}).get("usageBytes"), container.get("cpuThrottlingPercent")))
            while rows and rows[0][0] < now-self.retention_seconds:
                rows.popleft()
            self.series.move_to_end(key)
        while len(self.series) > 1024:
            self.series.popitem(last=False)

    def window(self, identity, container, seconds, now=None):
        now = time.time() if now is None else now
        return [row for row in self.series.get((*identity, container), ()) if now-seconds <= row[0] <= now]

    def merge(self, identity, series, now=None):
        now = time.time() if now is None else now
        for name, incoming in series.items():
            key = (*identity, name)
            points = {row[0]: row for row in incoming if now-self.retention_seconds <= row[0] <= now}
            # A concurrent live sample remains authoritative at the same instant.
            points.update({row[0]: row for row in self.series.get(key, ())
                           if now-self.retention_seconds <= row[0] <= now})
            self.series[key] = deque(sorted(points.values())[-720:], maxlen=720)
            self.series.move_to_end(key)
        while len(self.series) > 1024:
            self.series.popitem(last=False)
