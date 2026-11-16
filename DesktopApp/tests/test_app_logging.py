import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
import tempfile
import threading
import unittest
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtTest import QTest
from quaber.app_logging import LogSession, SafeFormatter, register_secret, redact
from quaber.log_panel import LogPanel


class AppLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_redaction_before_gui_and_file_from_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = QWidget()
            panel = LogPanel(owner)
            session = LogSession(Path(temporary), owner)
            session.bridge.message.connect(panel.append, Qt.ConnectionType.QueuedConnection)
            register_secret("credential with spaces")
            try:
                worker = threading.Thread(target=lambda: session.logger.warning(
                    'Failure %s token="opaque-access-token" Authorization: Bearer example-access-token',
                    "credential with spaces"))
                worker.start()
                worker.join(timeout=2)
                QTest.qWait(40)
                for text in (panel.output.toPlainText(), (Path(temporary)/"logs/quaber.log").read_text(encoding="utf-8")):
                    self.assertIn("Failure", text)
                    self.assertIn("REDACTED", text)
                    for secret in ("credential with spaces", "opaque-access-token", "example-access-token"):
                        self.assertNotIn(secret, text)
                before = (Path(temporary)/"logs/quaber.log").read_text(encoding="utf-8")
                panel.clear()
                self.assertEqual(panel.output.toPlainText(), "")
                self.assertEqual((Path(temporary)/"logs/quaber.log").read_text(encoding="utf-8"), before)
            finally:
                session.close()
                owner.deleteLater()

    def test_command_password_and_exception_are_redacted(self):
        text = redact('kubectl login --password "test password" client_secret=opaque-secret')
        self.assertNotIn("test password", text)
        self.assertNotIn("opaque-secret", text)
        register_secret("exception-private-credential")
        try:
            raise RuntimeError("exception-private-credential")
        except RuntimeError:
            import sys
            record = logging.LogRecord("quaber.test", logging.ERROR, __file__, 1, "Failure", (), sys.exc_info())
        self.assertNotIn("exception-private-credential", SafeFormatter().format(record))

    def test_visible_log_is_bounded(self):
        owner = QWidget()
        panel = LogPanel(owner)
        for line in range(5010):
            panel.append(str(line))
        self.assertEqual(panel.output.blockCount(), 1000)
        self.assertEqual(panel.output.toPlainText().splitlines()[0], "4010")
        owner.deleteLater()
