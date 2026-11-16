"""Recent Pod activity and the temporary matrix deployment mode."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

RECENT_SECONDS = 30 * 60
DEPLOY_MODE_SECONDS = 30 * 60


def recent_activity_deadline(pod, now=None):
    """Return when this Pod's creation or latest container restart ages out.

    Creation also covers a replacement Pod whose restartCount starts at zero.
    An older Pod may instead have a recently restarted regular or init container.
    """
    now = now or datetime.now(timezone.utc)
    values = [pod.get("metadata", {}).get("creationTimestamp")]
    status = pod.get("status", {})
    for key in ("containerStatuses", "initContainerStatuses", "ephemeralContainerStatuses"):
        for container in status.get(key, []):
            if not container.get("restartCount"):
                continue
            values.extend((
                container.get("state", {}).get("running", {}).get("startedAt"),
                container.get("lastState", {}).get("terminated", {}).get("finishedAt"),
                container.get("lastState", {}).get("terminated", {}).get("startedAt"),
            ))
    deadlines = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.tzinfo is not None and 0 <= (now - timestamp).total_seconds() < RECENT_SECONDS:
                deadlines.append(timestamp + timedelta(seconds=RECENT_SECONDS))
        except (ValueError, OverflowError):
            continue
    return max(deadlines, default=None)


class DeploymentMode(QObject):
    """A bounded pause that survives restarts without restarting the countdown."""

    changed = pyqtSignal(bool)
    countdown = pyqtSignal(int)

    def __init__(self, parent=None, expires_at=None, *, wall_clock=None, steady_clock=None):
        super().__init__(parent)
        self._wall_clock = wall_clock or time.time
        self._steady_clock = steady_clock or time.monotonic
        self.expires_at = None
        self._steady_until = 0
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        if type(expires_at) in (int, float) and 0 < expires_at < 1e12 and math.isfinite(expires_at):
            remaining = expires_at - self._wall_clock()
            if 0 < remaining <= DEPLOY_MODE_SECONDS:
                self.expires_at = expires_at
                self._steady_until = self._steady_clock() + remaining
                self.timer.start()

    @property
    def remaining_seconds(self):
        if self.expires_at is None:
            return 0
        return max(0, math.ceil(min(self.expires_at - self._wall_clock(),
                                    self._steady_until - self._steady_clock())))

    @property
    def active(self):
        return self.remaining_seconds > 0

    def set_active(self, active):
        if active and self.active:
            return
        if active:
            self.expires_at = self._wall_clock() + DEPLOY_MODE_SECONDS
            self._steady_until = self._steady_clock() + DEPLOY_MODE_SECONDS
            self.timer.start()
        elif self.expires_at is not None:
            self.expires_at = None
            self.timer.stop()
        else:
            return
        self.changed.emit(self.active)
        self.countdown.emit(self.remaining_seconds)

    def tick(self):
        if self.expires_at is not None and not self.active:
            self.set_active(False)
        else:
            self.countdown.emit(self.remaining_seconds)
