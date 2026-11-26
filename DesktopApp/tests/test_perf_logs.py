from unittest.mock import patch
import unittest
from helpers_log_ui import LogFixture


class CachedLogTests(LogFixture, unittest.TestCase):
    def test_unchanged_append_crlf_rotation(self):
        self.bg.finish('😀 начало\r\nстрока\r\n')
        self.assertEqual(self.view.output.toPlainText(), '😀 начало\nстрока\n')
        with patch.object(self.view.output, 'setPlainText', wraps=self.view.output.setPlainText) as reset, \
             patch.object(self.view, 'update_search', wraps=self.view.update_search) as search:
            self.view.show_text('😀 начало\r\nстрока\r\n')
            reset.assert_not_called()
            search.assert_not_called()
            self.view.show_text('😀 начало\nстрока\nновая\n')
            reset.assert_not_called()
            self.assertEqual(self.view.output.toPlainText(), '😀 начало\nстрока\nновая\n')
            self.view.show_text('rotation\n')
            self.assertEqual(reset.call_count, 1)
            self.assertEqual(self.view.output.toPlainText(), 'rotation\n')
