"""Synthetic own-session routing; this does not prove native Windows delivery."""
import unittest
from types import SimpleNamespace
from PyQt5.QtCore import QAbstractNativeEventFilter
from quaber.monitoring import MonitoringState
from quaber.windows_session import WindowsSession


class SessionRoutingTests(unittest.TestCase):
    def test_foreign_ignored_own_disconnect_and_lock_latch_until_explicit_reconnect(self):
        for event, reason in ((2, 'disconnect'), (4, 'disconnect'), (6, 'disconnect'), (7, 'lock')):
            state = MonitoringState()
            changes = []
            session = WindowsSession.__new__(WindowsSession)
            QAbstractNativeEventFilter.__init__(session)
            session.session_id, session.connected, session.closed = 41, True, False
            session.controller = SimpleNamespace(sleep=state.sleep, changed=SimpleNamespace(emit=lambda: changes.append(True)))
            session.session_event(event, 99)
            self.assertEqual(state.state, 'ACTIVE')
            session.session_event(event, 41)
            self.assertEqual((state.state, state.hard_reason), ('SLEEPING', reason))
            self.assertFalse(session.connected)
            for resume in (1, 3, 5, 8):
                session.session_event(resume, 41)
                self.assertTrue(session.connected)
                self.assertEqual(state.state, 'SLEEPING')
            self.assertEqual(len(changes), 4)
            self.assertTrue(state.reconnect())
            state.reconnected(True)
            session.closed = True
            session.session_event(event, 41)
            self.assertEqual(state.state, 'ACTIVE')
