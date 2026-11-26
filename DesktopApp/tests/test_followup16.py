import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

import yaml
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QApplication, QMainWindow, QStyle, QDialog
from quaber.authorization import ClusterAuthorization, AuthorizationRequired
from quaber.dump_export import export_dump
from quaber.inventory import CSV_FIELDS
from quaber.kubeconfig_import import read_contexts
from quaber.polling import PollingState, mark_poll_timer, polling_allowed
from quaber.storage import UserStore
from quaber.themes import ApplicationStyle
from quaber.resource_list import ResourceList
from quaber.ui import LoginGroupsDialog
from test_storage import CONFIG
from test_ux import ShellFixture


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = UserStore(self.root/'data')

    def add(self, name, host, shared=True):
        config = CONFIG.replace('example.test', host)
        return self.store.put_cluster(name, f'https://{host}:6443', 'alice', config,
            password='old', remember_password=True, shared_password=shared)


    def test_enabling_peer_uses_group_password_and_invalid_password_blocks_whole_group(self):
        a, b = self.add('a', 'a.test'), self.add('b', 'b.test', False)
        self.assertEqual(a['accountId'], b['accountId'])
        auth = ClusterAuthorization(self.store, Mock())
        with patch('quaber.authorization.locate_kubectl', return_value=Path('kubectl')), patch('quaber.authorization.login', side_effect=RuntimeError('invalid username or password')) as login:
            for cluster in (a, b):
                with self.assertRaises(AuthorizationRequired):
                    auth.renew(cluster['id'], cluster['encryptedKubeconfig'])
            login.assert_called_once()

    def test_network_failure_does_not_block_other_cluster(self):
        a, b = self.add('a', 'a.test'), self.add('b', 'b.test')
        auth = ClusterAuthorization(self.store, Mock())
        with patch('quaber.authorization.locate_kubectl', return_value=Path('kubectl')), patch('quaber.authorization.login', side_effect=RuntimeError('connection refused')) as login:
            for cluster in (a, b):
                with self.assertRaises(AuthorizationRequired):
                    auth.renew(cluster['id'], cluster['encryptedKubeconfig'])
            self.assertEqual(login.call_count, 2)

    def test_import_flattens_files_and_preserves_all_contexts_without_executing_plugins(self):
        config = yaml.safe_load(CONFIG)
        config['users'][0]['user'] = {'client-certificate': 'cert', 'client-key': 'key'}
        (self.root/'cert').write_bytes(b'certificate')
        (self.root/'key').write_bytes(b'private-key')
        config['contexts'].append({'name': 'second', 'context': config['contexts'][0]['context'].copy()})
        path = self.root/'config'
        path.write_text(yaml.safe_dump(config))
        entries = read_contexts(path)
        records = self.store.import_contexts(entries)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertFalse(self.store.login_required(record))
            user = yaml.safe_load(self.store.decrypt(record['encryptedKubeconfig']))['users'][0]['user']
            self.assertEqual(user['client-key-data'], 'cHJpdmF0ZS1rZXk=')
        config['users'][0]['user'] = {'exec': {'command': 'anything'}}
        path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, 'exec/auth-provider'):
            read_contexts(path)

    def test_opaque_imported_token_waits_for_server_rejection_instead_of_password_prompt(self):
        records = self.store.import_contexts([{'name': 'token', 'username': 'alice', 'server': 'https://example.test:6443', 'kubeconfig': CONFIG}])
        self.assertFalse(self.store.login_required(records[0]))
        auth = ClusterAuthorization(self.store, Mock())
        with patch('quaber.authorization.login') as login:
            with self.assertRaisesRegex(AuthorizationRequired, 'kubeconfig'):
                auth.renew(records[0]['id'], records[0]['encryptedKubeconfig'])
            login.assert_not_called()


class ExportTests(unittest.TestCase):
    def test_csv_and_xlsx_contain_same_complete_visible_values(self):
        rows = [('https://host', 'namespace', 'Deployment', 'service', 'registry/'+'path'*80+':tag', 'release<&', 'Иван', '=1+2', '2')]
        with tempfile.TemporaryDirectory() as folder:
            csv_path, xlsx_path = Path(folder)/'dump.csv', Path(folder)/'dump.xlsx'
            export_dump(csv_path, rows)
            export_dump(xlsx_path, rows)
            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                csv_rows = list(csv.reader(stream))
            with ZipFile(xlsx_path) as archive:
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))
                root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            values = [[c.find('s:is/s:t', ns).text for c in row] for row in root.findall('s:sheetData/s:row', ns)]
            self.assertEqual(values, [list(CSV_FIELDS), list(rows[0])])
            self.assertEqual(len(csv_rows), 2)
            self.assertEqual(csv_rows[1], list(rows[0]))
            self.assertFalse(list(Path(folder).glob('*.log')))


class PollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_minimize_preserves_poll_timers_epoch_and_does_not_restart_refresh(self):
        from quaber.ui import MainWindow
        from test_perf_matrix import MemoryStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = MemoryStore(Path(folder.name))
        store.prefs['refresh_ms'] = 5000
        with patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')), \
             patch.object(MainWindow, '_load_initial_matrix'), patch.object(MainWindow, '_prompt_expired_login'):
            window = MainWindow(store, Path(folder.name))
        resume = Mock()
        # V3's one controller owns workspace, epochs and dispatch as well as timers.
        window.monitoring.resume = resume
        window.show()
        self.app.processEvents()
        resume.reset_mock()
        epoch = window.monitoring.policy.epoch
        polling, expiry = QTimer(window), QTimer(window)
        polling.start(5000)
        expiry.start(60000)
        mark_poll_timer(polling)
        window.showMinimized()
        self.app.processEvents()
        self.assertTrue(polling_allowed(window))
        self.assertTrue(polling.isActive())
        self.assertTrue(expiry.isActive())
        window.showNormal()
        self.app.processEvents()
        self.assertTrue(polling.isActive())
        from quaber.polling import poll_interval
        self.assertEqual(poll_interval(polling), 5000)
        self.assertTrue(5000 <= polling.interval() <= 6000)
        resume.assert_not_called()
        self.assertEqual(window.monitoring.policy.epoch, epoch)
        window.close()
        window._cleanup_thread.join(5)
        window.deleteLater()
        self.app.processEvents()

    def test_standard_buttons_disable_system_icons(self):
        style = ApplicationStyle()
        self.assertEqual(style.styleHint(QStyle.StyleHint.SH_DialogButtonBox_ButtonsHaveIcons), 0)


class PageTests(ShellFixture, unittest.TestCase):
    def test_one_password_field_logs_into_two_distinct_servers(self):
        store = self.window.store
        group = [store.put_cluster(host, f'https://{host}:6443', 'alice', CONFIG.replace('example.test', host), shared_password=True)
                 for host in ('a.test', 'b.test')]
        dialog = LoginGroupsDialog(self.window, [{'username': 'alice', 'clusters': group}])
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(len(dialog.passwords), 1)
        self.assertFalse(dialog.remember_passwords[0].isChecked())
        dialog.remember_passwords[0].setChecked(True)
        dialog.passwords[0].setText('new-password')
        with patch('quaber.ui.locate_kubectl', return_value=Path('kubectl')), patch('quaber.ui.login', side_effect=lambda client, server, user, password: CONFIG.replace('https://example.test:6443', server)) as login:
            dialog.submit()
            self.wait()
            self.assertEqual(login.call_count, 2)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual([store.saved_password(c) for c in store.clusters()], ['new-password', 'new-password'])


    def test_navigation_labels_export_menu_and_refresh_on_right(self):
        self.assertEqual(len(self.window.nav_buttons), 7)
        page = self.window.versions_page
        self.assertTrue(page.labels_button.text().startswith('Labels'))
        self.assertIs(page.controls.itemAt(page.controls.count()-1).widget(), page.refresh_button)
        dumps = self.window.dumps_page
        self.assertEqual([action.text() for action in dumps.export_button.menu().actions()], ['CSV', 'XLSX'])
        panel = self.open_panel('pod')
        self.assertIn('Terminal', [panel.tabs.tabText(i) for i in range(panel.tabs.count())])
