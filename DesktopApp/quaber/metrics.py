"""Kubernetes quantity and cAdvisor metric helpers."""

from __future__ import annotations

import re
import threading
import time


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

    def parse(self, text: str, cluster_id: str, namespace: str, pod: str) -> dict[str, float | None]:
        counters: dict[str, dict[str, float]] = {}
        pattern = re.compile(r'^container_cpu_cfs_(periods_total|throttled_periods_total)\{([^}]*)\}\s+([0-9.eE+-]+)', re.M)
        for match in pattern.finditer(text):
            labels = dict((key, value.replace(r'\"', '"')) for key, value in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"', match[2]))
            if labels.get("namespace") != namespace or labels.get("pod") != pod or not labels.get("container"):
                continue
            try:
                value = float(match[3])
            except ValueError:
                continue
            counters.setdefault(labels["container"], {})[match[1]] = value
        result = {}
        now = time.monotonic()
        with self.lock:
            for container, values in counters.items():
                periods, throttled = values.get("periods_total"), values.get("throttled_periods_total")
                if periods is None or throttled is None:
                    continue
                key = cluster_id, namespace, pod, container
                previous = self.samples.get(key)
                if previous and periods == previous[0] and throttled == previous[1]:
                    result[container] = previous[3]
                    continue
                percent = None
                if previous and periods >= previous[0] and throttled >= previous[1]:
                    delta = periods - previous[0]
                    percent = max(0.0, min(100.0, (throttled - previous[1]) / delta * 100)) if delta else 0.0
                self.samples[key] = (periods, throttled, now, percent)
                result[container] = percent
            if len(self.samples) > 5000:
                self.samples = {key: value for key, value in self.samples.items() if now - value[2] <= 3600}
        return result
