"""Kubernetes quantity and cAdvisor metric helpers."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict

_CFS_PATTERN = re.compile(r'^container_cpu_cfs_(periods_total|throttled_periods_total)\{([^}]*)\}\s+([0-9.eE+-]+)', re.M)
_LABEL_PATTERN = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def parse_node_counters(text):
    counters = {}
    for match in _CFS_PATTERN.finditer(text):
        labels = {key: value.replace(r'\"', '"') for key, value in _LABEL_PATTERN.findall(match[2])}
        namespace, pod, container = (labels.get(k) for k in ('namespace', 'pod', 'container'))
        if namespace is None or pod is None or not container:
            continue
        try:
            value = float(match[3])
        except ValueError:
            continue
        counters.setdefault(namespace, {}).setdefault(pod, {}).setdefault(container, {})[match[1]] = value
    return counters


def cpu_cores(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))([num]?)", value.strip())
    if not match:
        return None
    return float(match[1]) * {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1}[match[2]]


def memory_bytes(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))([EPTGMK]i?|m)?", value.strip(), re.I)
    if not match:
        return None
    amount, suffix = float(match[1]), match[2] or ""
    if suffix == "m":
        return amount / 1000
    suffix = suffix.upper()
    binary = suffix.endswith("I")
    unit = suffix[:-1] if binary else suffix
    if not unit:
        power = 0
    elif unit in "KMGTPE" and len(unit) == 1:
        power = "KMGTPE".index(unit) + 1
    else:
        return None
    return amount * (1024 if binary else 1000) ** power


class ThrottleSamples:
    def __init__(self):
        self.samples: dict[tuple[str, str, str, str], tuple[float, float, float]] = {}
        self.lock = threading.Lock()
        self.identities = OrderedDict()

    def identity_floor(self, identity):
        with self.lock:
            floor = self.identities.setdefault(identity, time.monotonic_ns())
            self.identities.move_to_end(identity)
            while len(self.identities) > 5000: self.identities.popitem(last=False)
            return floor

    def parse(self, text: str, cluster_id: str, namespace: str, pod: str) -> dict[str, float | None]:
        counters = parse_node_counters(text).get(namespace, {}).get(pod, {})
        return self.from_counters(counters, cluster_id, '', namespace, pod)

    def from_counters(self, counters, cluster_id, node, namespace, pod, generation=None, *, not_before=None):
        result = {}
        now = time.monotonic()
        with self.lock:
            for container, values in counters.items():
                periods, throttled = values.get("periods_total"), values.get("throttled_periods_total")
                if periods is None or throttled is None:
                    continue
                if not_before is not None and generation is not None and generation < not_before:
                    result[container] = None
                    continue
                key = cluster_id, node, namespace, pod, container
                previous = self.samples.get(key)
                if previous and generation is not None and previous[4] is not None and generation <= previous[4]:
                    result[container] = previous[3]
                    continue
                if previous and periods == previous[0] and throttled == previous[1]:
                    result[container] = previous[3]
                    self.samples[key] = (periods, throttled, now, previous[3], generation)
                    continue
                percent = None
                if previous and periods >= previous[0] and throttled >= previous[1]:
                    delta = periods - previous[0]
                    percent = max(0.0, min(100.0, (throttled - previous[1]) / delta * 100)) if delta else 0.0
                self.samples[key] = (periods, throttled, now, percent, generation)
                result[container] = percent
            if len(self.samples) > 5000:
                self.samples = {key: value for key, value in self.samples.items() if now - value[2] <= 3600}
                if len(self.samples) > 5000:
                    self.samples = dict(sorted(self.samples.items(), key=lambda pair: pair[1][2])[-5000:])
        return result
