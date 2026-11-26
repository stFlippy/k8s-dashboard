"""Bound one visible rollout burst without extending it on background ticks."""
import time


class RolloutCadence:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.until = None
        self.exhausted = False

    def request_fast(self):
        self.until = self.clock()+120
        self.exhausted = False

    def interval(self, moving):
        now = self.clock()
        if self.until is not None and now >= self.until:
            self.exhausted = True
        if not moving:
            if not self.exhausted: self.until = None
            return 5000
        if self.until is None and not self.exhausted:
            self.until = now+120
        return 1000 if not self.exhausted and self.until is not None and now < self.until else 5000
