"""Own-session WTS/power events. No cluster, credentials or proxy access."""
import ctypes
import logging
import os
import traceback
from PyQt5.QtCore import QAbstractNativeEventFilter, QEvent

logger = logging.getLogger(__name__)


class WindowsSession(QAbstractNativeEventFilter):
    def __init__(self, window, controller):
        QAbstractNativeEventFilter.__init__(self)
        self.window, self.controller = window, controller
        self.hwnd = None
        self.session_id = None
        self.warned = False
        self.connected = True
        self.closed = False
        self.failure = False
        self._foreground_ready = False
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        try:
            self.user = ctypes.WinDLL('user32', use_last_error=True)
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
        except OSError:
            self._warn()
            return
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetForegroundWindow.argtypes = []
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.GetWindowThreadProcessId.restype = w.DWORD
        self._foreground_ready = True
        self.wts.WTSRegisterSessionNotification.argtypes = [w.HWND, w.DWORD]
        self.wts.WTSRegisterSessionNotification.restype = w.BOOL
        self.wts.WTSUnRegisterSessionNotification.argtypes = [w.HWND]
        self.wts.WTSUnRegisterSessionNotification.restype = w.BOOL
        self.wts.WTSQuerySessionInformationW.argtypes = [w.HANDLE, w.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
        self.wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self.wts.WTSFreeMemory.restype = None
        self.wts.WTSQuerySessionInformationW.restype = w.BOOL
        self.kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = w.BOOL
        sid = w.DWORD()
        if self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            self.session_id = sid.value
        else:
            self._warn()
        from PyQt5.QtWidgets import QApplication
        QApplication.instance().installNativeEventFilter(self)
        window.destroyed.connect(self.close)
        self.register()
        self.query_initial()

    def _warn(self):
        if not self.warned:
            self.warned = True
            logger.warning('WTS unavailable; local visibility and inactivity protection remain active.')

    def register(self):
        if self.closed or self.failure or not hasattr(self, 'wts'):
            return
        hwnd = int(self.window.winId())
        if hwnd == self.hwnd:
            return
        if self.hwnd:
            self.wts.WTSUnRegisterSessionNotification(self.hwnd)
        self.hwnd = hwnd if self.wts.WTSRegisterSessionNotification(hwnd, 0) else None
        if self.hwnd is None: self._warn()

    def query_initial(self):
        from ctypes import wintypes as w
        if self.session_id is None:
            return
        ptr, size = ctypes.c_void_p(), w.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, self.session_id, 8, ctypes.byref(ptr), ctypes.byref(size)):
            self._warn()
            return
        try:
            if not ptr.value or size.value < ctypes.sizeof(ctypes.c_int):
                self._warn()
                return
            if ctypes.cast(ptr, ctypes.POINTER(ctypes.c_int))[0] != 0:
                self.connected = False
                self.controller.sleep('disconnect')
        finally:
            self.wts.WTSFreeMemory(ptr)
        # The leading fields of WTSINFOEX_LEVEL1 are session/state/flags.
        class LevelOne(ctypes.Structure):
            _fields_ = [('session', w.DWORD), ('state', ctypes.c_int), ('flags', w.LONG)]
        class Info(ctypes.Structure):
            _fields_ = [('level', w.DWORD), ('alignment', w.DWORD), ('data', LevelOne)]
        ptr, size = ctypes.c_void_p(), w.DWORD()
        if self.wts.WTSQuerySessionInformationW(None, self.session_id, 25, ctypes.byref(ptr), ctypes.byref(size)):
            try:
                if not ptr.value or size.value < ctypes.sizeof(Info):
                    self._warn()
                    return
                info = ctypes.cast(ptr, ctypes.POINTER(Info))[0]
                if size.value >= ctypes.sizeof(Info) and info.level == 1 and info.data.flags == 0:
                    self.connected = False
                    self.controller.sleep('lock')
            finally:
                self.wts.WTSFreeMemory(ptr)
        else:
            self._warn()

    def own_foreground(self):
        if os.name != 'nt' or not self._foreground_ready:
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            return app.activeWindow() is not None if app is not None else None
        from ctypes import wintypes as w
        hwnd = self.user.GetForegroundWindow()
        if not hwnd:
            return None
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def session_event(self, event, session_id):
        if not self.closed and session_id == self.session_id:
            if event in (2, 4, 6):
                self.connected = False
                self.controller.sleep('disconnect')
            elif event == 7:
                self.connected = False
                self.controller.sleep('lock')
            elif event in (1, 3, 5, 8):
                self.connected = True
                self.controller.changed.emit()
            # connect/unlock never remove latched sleep.

    def nativeEventFilter(self, kind, message):
        if self.closed or self.failure or os.name != 'nt':
            return False, 0
        try:
            if bytes(kind) not in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
                return False, 0
            address = int(message) if message is not None else 0
            if not address:
                return False, 0
            from ctypes import wintypes as w
            msg = w.MSG.from_address(address)
            if self.hwnd and msg.hWnd == self.hwnd:
                if msg.message == 0x02B1: self.session_event(msg.wParam, msg.lParam)
                elif msg.message == 0x0218 and msg.wParam == 4: self.controller.sleep('suspend')
        except Exception:
            self._callback_failure()
        return False, 0

    def _callback_failure(self):
        # Disable this boundary before logging or notifying other callbacks.
        # No exception arguments/locals are sent to an unredacted fallback.
        self.failure = True
        self.connected = False
        try:
            from .app_logging import redact
            logger.error('WTS callback failed; adapter disabled.\n%s',
                         redact(traceback.format_exc()))
        except Exception:
            pass
        try:
            self.controller.sleep('disconnect')
        except Exception:
            pass

    def eventFilter(self, watched, event):
        try:
            if not self.closed and not self.failure and watched is self.window and event.type() == QEvent.WinIdChange and os.name == 'nt':
                self.register()
        except Exception:
            self._callback_failure()
        return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if os.name == 'nt':
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
            if self.hwnd:
                self.wts.WTSUnRegisterSessionNotification(self.hwnd)
                self.hwnd = None
