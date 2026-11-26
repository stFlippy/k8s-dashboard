"""Actual ctypes MSG boundary regressions; no UserData or cluster access."""
import ctypes
import os
import unittest
from ctypes import wintypes as w
from types import SimpleNamespace
from unittest.mock import Mock
from PyQt5.QtCore import QAbstractNativeEventFilter, QByteArray
from quaber.windows_session import WindowsSession


@unittest.skipUnless(os.name == 'nt', 'Windows MSG ABI required')
class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = WindowsSession.__new__(WindowsSession)
        QAbstractNativeEventFilter.__init__(self.session)
        self.session.closed = self.session.failure = False
        self.session.connected = True
        self.session.hwnd, self.session.session_id = 123, 41
        self.session.controller = SimpleNamespace(sleep=Mock(), changed=SimpleNamespace(emit=Mock()))
        self.msg = w.MSG()
        self.msg.hWnd = 123
        self.msg.message, self.msg.wParam, self.msg.lParam = 0x02B1, 4, 41

    def deliver(self, kind=b'windows_generic_MSG'):
        return self.session.nativeEventFilter(kind, ctypes.addressof(self.msg))

    def test_actual_msg_disconnect_and_unlock(self):
        self.assertEqual(self.deliver(QByteArray(b'windows_generic_MSG')), (False, 0))
        self.session.controller.sleep.assert_called_once_with('disconnect')
        self.assertFalse(self.session.connected)
        self.msg.wParam = 8
        self.deliver(b'windows_dispatcher_MSG')
        self.assertTrue(self.session.connected)
        self.session.controller.sleep.assert_called_once()

    def test_foreign_window_session_and_suspend(self):
        self.msg.hWnd = 124
        self.deliver()
        self.msg.hWnd, self.msg.lParam = 123, 42
        self.deliver()
        self.session.controller.sleep.assert_not_called()
        self.msg.message, self.msg.wParam = 0x0218, 4
        self.deliver()
        self.session.controller.sleep.assert_called_once_with('suspend')

    def test_rejected_events_never_dereference_pointer(self):
        self.assertEqual(self.session.nativeEventFilter(b'other', 1), (False, 0))
        self.assertEqual(self.session.nativeEventFilter(b'windows_generic_MSG', 0), (False, 0))
        self.session.closed = True
        self.assertEqual(self.session.nativeEventFilter(b'windows_generic_MSG', 1), (False, 0))

    def test_python_failure_disables_boundary_logs_once_and_sleeps(self):
        self.session.session_event = Mock(side_effect=ValueError('token=synthetic-secret'))
        with self.assertLogs('quaber.windows_session', level='ERROR') as logs:
            self.assertEqual(self.deliver(), (False, 0))
            self.deliver()
        self.assertTrue(self.session.failure)
        self.assertFalse(self.session.connected)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Traceback', logs.output[0])
        self.assertNotIn('synthetic-secret', logs.output[0])
        self.session.controller.sleep.assert_called_once_with('disconnect')

    def test_register_recreate_and_close_are_idempotent(self):
        self.session.wts = SimpleNamespace(WTSRegisterSessionNotification=Mock(return_value=True),
                                           WTSUnRegisterSessionNotification=Mock())
        self.session.window = SimpleNamespace(winId=Mock(return_value=456))
        self.session.register()
        self.session.wts.WTSUnRegisterSessionNotification.assert_called_once_with(123)
        self.session.wts.WTSRegisterSessionNotification.assert_called_once_with(456, 0)
        self.session.register()
        self.session.wts.WTSRegisterSessionNotification.assert_called_once()
        self.session.close()
        self.session.close()
        self.assertIsNone(self.session.hwnd)
        self.assertEqual(self.session.wts.WTSUnRegisterSessionNotification.call_count, 2)
