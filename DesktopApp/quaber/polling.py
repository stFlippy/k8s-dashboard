"""Shared polling admission and staggered network timer due dates."""
import random
from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QWidget, QMainWindow


def polling_allowed(owner):
    while owner is not None:
        controller = getattr(owner, 'monitoring', None)
        if controller is not None:
            return controller.policy.allows()
        if isinstance(owner, QWidget) and owner.isMinimized():
            return False
        owner = owner.parent() if isinstance(owner, QObject) else None
    return True


def mark_poll_timer(timer):
    timer.setProperty("clusterPoll", True)
    if not timer.property('pollJitterConnected'):
        timer.setProperty('pollJitterConnected', True)
        timer.timeout.connect(lambda: jitter_poll_timer(timer) if timer.isActive() else None)
    if timer.isActive(): jitter_poll_timer(timer)
    owner = timer.parent()
    while owner is not None:
        if isinstance(owner, QMainWindow):
            state = getattr(owner, "polling_state", None)
            if getattr(owner, 'preferences', {}).get('refresh_ms', 10000) == 0:
                timer.stop()
                if state is not None:
                    state.timers.pop(timer, None)
            elif state and state.paused:
                state.suspend(timer)
            break
        owner = owner.parent()


def restart_poll_timer(timer, interval):
    timer.setInterval(interval)
    timer.start()
    mark_poll_timer(timer)


def poll_interval(timer):
    if timer.interval() == timer.property('pollDueInterval'):
        return timer.property('pollBaseInterval')
    return timer.interval()


def jitter_poll_timer(timer, interval=None, rng=random.uniform):
    base = poll_interval(timer) if interval is None else interval
    if base <= 0: return
    # Delay-only jitter keeps the minimum requested cadence (not faster polling).
    due = max(base, round(base*rng(1., 1.2)))
    timer.setProperty('pollBaseInterval', base)
    timer.setProperty('pollDueInterval', due)
    timer.setInterval(due)




# Compatibility import, one source of state rather than two independent pauses.
from .monitoring import MonitoringController
PollingState = MonitoringController
