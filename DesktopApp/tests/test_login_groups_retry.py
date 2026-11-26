"""Partial group authentication, optimistic updates and native caption flags."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QDialog
from quaber.connection_presentation import password_groups
from quaber.ui import LoginGroupsDialog, MainWindow
from test_storage import CONFIG
from test_ux import ShellFixture


class LoginRetryTests(ShellFixture, unittest.TestCase):
    def test_saving_password_requires_explicit_choice_even_in_shared_group(self):
        record = self.connections()[0]
        self.window.store.put_cluster(record['name'], record['server'], record['username'], None,
                                     cluster_id=record['id'], password='old-secret', remember_password=True)
        record = self.window.store.clusters()[0]
        dialog = self.dialog([record])
        self.assertFalse(dialog.remember_passwords[0].isChecked())
        dialog.passwords[0].setText('session-secret')
        with patch('quaber.ui.login', return_value=CONFIG), patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            dialog.submit()
            self.wait_dialog(dialog)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        record = self.window.store.clusters()[0]
        self.assertNotIn('encryptedPassword', record)
        self.assertEqual(self.window.store.saved_password(record), 'session-secret')
        from quaber.storage import UserStore
        self.assertEqual(UserStore(self.window.store.root).saved_password(record), '')

    def connections(self):
        return [self.window.store.put_cluster(name, 'https://example.test:6443', username, CONFIG,
                                              shared_password=True)
                for name, username in [('Admin lab', 'admin'), ('Edit lab', 'edit')]]

    def dialog(self, records):
        dialog = LoginGroupsDialog(self.window, password_groups(records))
        self.addCleanup(dialog.deleteLater)
        return dialog

    def wait_dialog(self, dialog):
        deadline = time.monotonic()+5
        while dialog.busy and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertFalse(dialog.busy)

    def test_failed_first_group_does_not_stop_second_and_retry_skips_success(self):
        records = self.connections()
        dialog = self.dialog(records)
        dialog.passwords[0].setText('bad')
        dialog.passwords[1].setText('good')
        def login(executable, server, username, password):
            if password == 'bad': raise RuntimeError('incorrect password')
            return CONFIG
        with patch('quaber.ui.login', side_effect=login) as command, patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            dialog.submit()
            self.wait_dialog(dialog)
            self.assertEqual(command.call_count, 2)
            self.assertEqual(dialog.completed_groups, {1})
            self.assertEqual(dialog.succeeded_cluster_ids, {records[1]['id']})
            self.assertFalse(dialog.passwords[1].isEnabled())
            self.assertTrue(dialog.passwords[1].isHidden())
            self.assertFalse(dialog.passwords[0].isHidden())
            self.assertTrue(dialog.remember_passwords[1].isHidden())
            self.assertFalse(dialog.remember_passwords[0].isHidden())
            self.assertEqual(dialog.passwords[1].text(), '')
            saved = self.window.store.clusters()[1]['encryptedKubeconfig']
            dialog.passwords[0].setText('corrected')
            dialog.submit()
            self.wait_dialog(dialog)
            self.assertEqual(command.call_count, 3)
            self.assertEqual(command.call_args.args[2], 'admin')
            self.assertEqual(self.window.store.clusters()[1]['encryptedKubeconfig'], saved)
            self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_successful_first_group_is_not_replayed_with_stale_expected_record(self):
        records = self.connections()
        dialog = self.dialog(records)
        dialog.passwords[0].setText('admin-good')
        dialog.passwords[1].setText('edit-bad')
        def login(executable, server, username, password):
            if password == 'edit-bad': raise RuntimeError('incorrect password')
            return CONFIG
        with patch('quaber.ui.login', side_effect=login) as command, patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            dialog.submit()
            self.wait_dialog(dialog)
            self.assertEqual(dialog.completed_groups, {0})
            dialog.passwords[1].setText('edit-good')
            dialog.submit()
            self.wait_dialog(dialog)
        self.assertEqual([call.args[2] for call in command.call_args_list], ['admin', 'edit', 'edit'])
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_new_click_uses_fresh_record_but_midflight_change_is_rejected(self):
        record = self.connections()[0]
        dialog = self.dialog([record])
        # Harmless settings change before clicking must not poison the snapshot.
        self.window.store.put_cluster(record['name'], record['server'], record['username'], None,
                                      cluster_id=record['id'], controller_timezone='UTC')
        dialog.passwords[0].setText('good')
        with patch('quaber.ui.login', return_value=CONFIG), patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            dialog.submit()
            self.wait_dialog(dialog)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        current = self.window.store.clusters()[0]
        second = self.dialog([current])
        second.passwords[0].setText('good')
        def changed(*args):
            self.window.store.set_cluster_enabled(record['id'], False)
            return CONFIG
        with patch('quaber.ui.login', side_effect=changed), patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            second.submit()
            self.wait_dialog(second)
        self.assertFalse(second.succeeded_cluster_ids)
        self.assertIn('изменилась', second.group_statuses[0].text())
        saved = self.window.store.clusters()[0]
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['encryptedKubeconfig'], current['encryptedKubeconfig'])
        second.reject()

    def test_changed_identity_before_submit_never_receives_old_password(self):
        record = self.connections()[0]
        dialog = self.dialog([record])
        rows = self.window.store.clusters()
        rows[0]['server'] = 'https://replacement.example.test:6443'
        self.window.store.save_clusters(rows)
        dialog.passwords[0].setText('private')
        with patch('quaber.ui.login') as command:
            dialog.submit()
            self.wait_dialog(dialog)
            command.assert_not_called()
        self.assertIn('изменено', dialog.group_statuses[0].text())
        dialog.reject()

    def test_double_submit_does_not_create_duplicate_login_and_error_is_redacted(self):
        record = self.connections()[0]
        dialog = self.dialog([record])
        secret = 'synthetic-private-secret'
        dialog.passwords[0].setText(secret)
        with patch('quaber.ui.login', side_effect=RuntimeError('diagnostic: '+secret)) as command, patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            dialog.submit()
            dialog.submit()
            self.wait_dialog(dialog)
        command.assert_called_once()
        self.assertNotIn(secret, dialog.group_statuses[0].text())
        dialog.reject()

    def test_cancel_preserves_success_in_mainwindow_authorization_tracking(self):
        records = self.connections()
        self.window._unauthorized_clusters.update(c['id'] for c in records)
        self.api.authorization.renew.side_effect = RuntimeError('manual needed')
        def inspect(dialog):
            dialog.passwords[0].setText('good')
            dialog.passwords[1].setText('bad')
            dialog.submit()
            self.wait_dialog(dialog)
            dialog.reject()
            return QDialog.Rejected
        def login(executable, server, username, password):
            if password == 'bad': raise RuntimeError('incorrect password')
            return CONFIG
        with patch('quaber.ui.run_dialog', side_effect=inspect), patch('quaber.ui.polling_allowed', return_value=True), patch('quaber.ui.login', side_effect=login), patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')):
            MainWindow._prompt_expired_login(self.window)
            self.wait()
        self.assertNotIn(records[0]['id'], self.window._unauthorized_clusters)
        self.assertNotIn(records[0]['id'], self.window._auth_dismissed)
        self.assertIn(records[1]['id'], self.window._unauthorized_clusters)
        self.assertIn(records[1]['id'], self.window._auth_dismissed)

    def test_context_help_button_removed_from_settings_and_secret_dialog(self):
        dialog = self.dialog(self.connections())
        self.assertFalse(dialog.windowFlags() & Qt.WindowContextHelpButtonHint)
        dialog.reject()
        def inspect(settings):
            self.assertFalse(settings.windowFlags() & Qt.WindowContextHelpButtonHint)
            return QDialog.Rejected
        self.api.access_summary.return_value = dict(level='Чтение', detail='Synthetic')
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()


if __name__ == '__main__': unittest.main()
