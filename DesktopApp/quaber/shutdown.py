"""One GUI shutdown path; signal handlers only enqueue a request."""
import logging
import time
from PyQt5.QtCore import QObject, QTimer, QCoreApplication, QEvent

logger = logging.getLogger(__name__)


def release_window(window):
    """Destroy the widget tree on the GUI thread while QApplication is alive."""
    from PyQt5 import sip
    if window is not None and not sip.isdeleted(window):
        window.deleteLater()
        QCoreApplication.sendPostedEvents(window, QEvent.DeferredDelete)


class ShutdownCoordinator(QObject):
    def __init__(self, app, *, clock=time.monotonic):
        super().__init__(app)
        self.app, self.clock = app, clock
        self.window = None
        self.pending = None
        self.code = None
        self.accepted = False
        self.completed = False
        self.deadline = None
        self.stalled = False
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def attach(self, window):
        self.window = window
        self.app.setQuitOnLastWindowClosed(False)
        if self.accepted:
            code = self.code
            self.accepted, self.completed = False, False
            self.request('bootstrap_shutdown',code,code == 1)

    def signal_interrupt(self, signum=None, frame=None):
        # No widgets, logging, I/O or waits in Python's signal handler.
        if self.pending is None and not self.accepted:
            self.pending = ('user_interrupt',130,False)

    def request(self, reason, code, fatal=False):
        if self.accepted: return True
        if self.window is None:
            self.code = code
            self.accepted = True
            return True
        if fatal:
            self.window._fatal_shutdown = True
            self.window.write_guard.invalidate()
            self.window.preferences['read_only'] = True
            self.window.api.stopped = True
        if not self.window.close():
            return False
        self.accepted, self.code = True, code
        self.deadline = self.clock()+30
        logger.info('shutdown requested reason=%s code=%s',reason,code)
        return True

    def poll(self):
        if self.completed: return
        if self.pending is not None:
            request, self.pending = self.pending, None
            self.request(*request)
        if self.window is not None and getattr(self.window,'_closing',False) and not self.accepted:
            self.accepted, self.code = True, 0
            self.deadline = self.clock()+30
        if not self.accepted: return
        worker = getattr(self.window,'_cleanup_thread',None) if self.window is not None else None
        if worker is None or not worker.is_alive():
            self.completed = True
            self.timer.stop()
            logger.info('shutdown cleanup completed code=%s',self.code)
            self.app.exit(self.code)
        elif self.deadline is not None and self.clock() >= self.deadline and not self.stalled:
            self.stalled = True
            # Keep owning the instance lock until cleanup finishes. No unsafe
            # release/replay and no global process kill on a stalled transport.
            logger.error('shutdown cleanup deadline exceeded; waiting for owned operations')
