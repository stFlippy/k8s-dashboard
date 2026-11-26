import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from quaber.callback_diagnostics import failure_hook


class CallbackDiagnosticsTests(unittest.TestCase):
    def test_redacted_trace_and_failure_exit_once(self):
        app = SimpleNamespace(exit=Mock())
        hook = failure_hook(app)
        try:
            raise ValueError('token=synthetic-secret')
        except ValueError as exc:
            with self.assertLogs('quaber', level='CRITICAL') as logs:
                hook(type(exc), exc, exc.__traceback__)
                hook(type(exc), exc, exc.__traceback__)
        app.exit.assert_called_once_with(1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Traceback', logs.output[0])
        self.assertNotIn('synthetic-secret', logs.output[0])

    def test_logging_failure_still_exits(self):
        app = SimpleNamespace(exit=Mock())
        with patch('quaber.callback_diagnostics.logging.getLogger', side_effect=OSError):
            failure_hook(app)(ValueError, ValueError('token=synthetic-secret'), None)
        app.exit.assert_called_once_with(1)
