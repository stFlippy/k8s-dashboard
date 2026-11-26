"""Connection preferences and failed-login persistence with no real cluster IO."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QComboBox, QDialog
from quaber.ui import ClusterDialog
from quaber.resource_list import ResourceList
from quaber.schedule import ScheduleEngine, ScheduleInput, relative_interval
from test_storage import CONFIG
from test_ux import ShellFixture


class ConnectionPreferenceTests(ShellFixture, unittest.TestCase):
    def connection(self, name='Lab', **kwargs):
        return self.window.store.put_cluster(name, 'https://example.test:6443', 'alice', CONFIG, **kwargs)


    def test_settings_timeout_five_minute_steps_and_status_meaning(self):
        self.connection()
        self.api.access_summary.return_value = dict(level='Редактор', detail='Synthetic')
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            picker = dialog.findChild(QComboBox, 'monitoringIdleMinutes')
            self.assertEqual([picker.itemData(i) for i in range(picker.count())], list(range(10, 121, 5)))
            self.assertLessEqual(picker.width(), picker.sizeHint().width() + 5)
            self.assertGreaterEqual(picker.width(), picker.fontMetrics().horizontalAdvance('120 мин') + 32)
            table = next(t for t in dialog.findChildren(ResourceList) if t.property('clusterSettings'))
            self.assertEqual(table.columnCount(), 6)
            self.assertNotIn('Сохранённый вход', [table.headerItem().text(i) for i in range(table.columnCount())])
            self.assertIsNotNone(table.itemWidget(table.topLevelItem(0), 3))
            dialog.accept()
            return QDialog.Accepted
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()

    def make_dialog(self, connection=None):
        root = Path(self.temp.name)
        executable = root/'synthetic-kubectl.exe'
        executable.write_bytes(b'fixture')
        dialog = ClusterDialog(self.window, self.window.store, root, connection)
        if connection is None:
            dialog.name.setText('Unverified')
            account = self.window.store.put_account('Unverified account', 'alice', 'unverified-bad-password')
            dialog.reload_accounts(account['id'])
            dialog.server.setText('https://example.test:6443')
        dialog.kubectl_path.setText(str(executable))
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_bad_password_saves_disabled_and_cannot_replace_shared_password(self):
        self.connection(password='known-good', remember_password=True, shared_password=True)
        dialog = self.make_dialog()
        with patch('quaber.ui.login', side_effect=RuntimeError('Synthetic incorrect password')):
            dialog.connect_cluster(True)
            self.wait()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        records = {c['name']: c for c in self.window.store.clusters()}
        draft = records['Unverified']
        self.assertFalse(draft['enabled'])
        self.assertEqual(draft['encryptedKubeconfig'], '')
        self.assertNotIn('encryptedPassword', draft)
        self.assertEqual(self.window.store.saved_password(records['Lab']), 'known-good')
        with self.assertRaises(ValueError): self.window.store.set_cluster_enabled(draft['id'], True)
        edit = self.make_dialog(draft)
        account = next(a for a in self.window.store.accounts() if a['id'] == draft['accountId'])
        self.window.store.put_account(account['name'], account['username'], 'known-good', account_id=account['id'])
        edit.cluster = next(c for c in self.window.store.clusters() if c['id'] == draft['id'])
        with patch('quaber.ui.login', return_value=CONFIG):
            edit.connect_cluster(True)
            self.wait()
        saved = next(c for c in self.window.store.clusters() if c['id'] == draft['id'])
        self.assertNotIn('credential_error', saved)
        self.window.store.set_cluster_enabled(saved['id'], True)
        self.assertTrue(next(c for c in self.window.store.clusters() if c['id'] == draft['id'])['enabled'])

    def test_conflicting_edit_is_not_overwritten_by_fallback_save(self):
        record = self.connection(password='synthetic', remember_password=True)
        dialog = self.make_dialog(record)
        def changed(*args):
            self.window.store.set_cluster_enabled(record['id'], False)
            return CONFIG
        with patch.object(self.window.store, 'login_required', return_value=True), patch('quaber.ui.login', side_effect=changed):
            dialog.connect_cluster(True)
            self.wait()
        self.assertNotEqual(dialog.result(), QDialog.Accepted)
        saved = self.window.store.clusters()[0]
        self.assertEqual(saved['encryptedKubeconfig'], record['encryptedKubeconfig'])
        self.assertFalse(saved['enabled'])
        self.assertNotIn('credential_error', saved)


class ScheduleDisplayTests(unittest.TestCase):
    def test_dated_display_uses_calendar_zone(self):
        now = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)
        engine = ScheduleEngine()
        utc = engine.calculate(ScheduleInput('0 9 * * *', 'UTC'), now)
        samara = engine.calculate(ScheduleInput('0 9 * * *', 'UTC+04:00'), now)
        self.assertEqual(utc.next_due_utc-now, timedelta(hours=1))
        self.assertEqual(samara.next_due_utc-now, timedelta(hours=21))
        self.assertEqual(utc.tooltip(now), 'Next run: 2026-11-24 09:00:00\nUTC · через 1 час')
        self.assertEqual(samara.tooltip(now), 'Next run: 2026-11-25 09:00:00\nUTC+04:00 · через 21 час')
        self.assertEqual(relative_interval(25*60), 'через 25 минут')
        self.assertEqual(relative_interval(1), 'менее чем через минуту')


if __name__ == '__main__': unittest.main()
