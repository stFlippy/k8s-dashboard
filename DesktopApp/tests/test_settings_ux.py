"""Settings UX acceptance: local synthetic connections only, no cluster transport."""
import csv
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit
from quaber.connection_presentation import password_groups, login_color, TimeZonePicker
from quaber.dump_export import export_dump
from quaber.inventory import CSV_FIELDS
from quaber.resource_list import ResourceList
from quaber.schedule import load_zone, ScheduleEngine, ScheduleInput
from quaber.themes import apply_palette
from quaber.ui import ClusterDialog, LoginGroupsDialog
from test_storage import CONFIG
from test_ux import ShellFixture
import test_perf_matrix as matrix_fixture


class ConnectionPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_passwords_are_grouped_by_account_reference(self):
        records = [dict(id=str(i), name=str(i), username=user, server='https://same', accountId='shared-'+user if shared else 'account-'+str(i))
                   for i, (user, shared) in enumerate([('admin', True), ('edit', True), ('admin', True), ('admin', False), ('admin', False)])]
        groups = password_groups(records)
        self.assertEqual(sorted(len(g['clusters']) for g in groups), [1, 1, 1, 2])
        self.assertTrue(all(len({c['username'] for c in g['clusters']}) == 1 for g in groups))
        self.assertEqual(login_color('admin'), login_color('admin'))
        self.assertNotEqual(login_color('admin'), login_color('edit'))

    def test_only_fixed_offsets_are_supported(self):
        for value, offset in [('UTC+04:00', 240), ('UTC-03:30', -210), ('UTC', 0)]:
            picker = TimeZonePicker(value)
            self.assertEqual(picker.text(), value)
            self.assertEqual(datetime(2026, 11, 24, tzinfo=load_zone(picker.text())).utcoffset(), timedelta(minutes=offset))
            picker.deleteLater()
        picker = TimeZonePicker('Europe/Samara')
        self.assertEqual(picker.text(), '')
        self.assertEqual(picker.findData('Europe/Samara'), -1)
        picker.deleteLater()
        for bad in ['UTC+14:15', 'UTC-12:15', 'UTC+04:99', 'Europe/Samara']:
            with self.assertRaises(ValueError): load_zone(bad)
        engine = ScheduleEngine()
        result = engine.calculate(ScheduleInput('0 9 * * *', 'UTC+04:00'), datetime(2026, 11, 24, 4, tzinfo=timezone.utc))
        self.assertEqual(result.next_due_utc, datetime(2026, 11, 24, 5, tzinfo=timezone.utc))


    def test_placeholder_contrast_in_both_themes(self):
        field = QLineEdit()
        for light in (False, True):
            apply_palette(field, light)
            palette = field.palette()
            self.assertNotEqual(palette.color(QPalette.Active, QPalette.PlaceholderText), palette.color(QPalette.Active, QPalette.Base))
            self.assertEqual(palette.color(QPalette.Active, QPalette.PlaceholderText), palette.color(QPalette.Inactive, QPalette.PlaceholderText))
        field.deleteLater()

    def test_delayed_disconnect_does_not_close_reenabled_connection(self):
        from quaber.kubectl import ProxyManager
        store = Mock()
        manager = ProxyManager(None, store)
        with patch.object(manager, 'close') as close:
            store.clusters.return_value = [dict(id='c', enabled=True)]
            manager.close_disabled('c')
            close.assert_not_called()
            store.clusters.return_value = [dict(id='c', enabled=False)]
            manager.close_disabled('c')
            close.assert_called_once_with('c')

    def test_dynamic_csv_xlsx_headers_and_columns_after_z(self):
        fields = (*CSV_FIELDS, *(f'label:key-{i}' for i in range(30)))
        row = tuple(f'value-{i}' for i in range(len(fields)))
        with tempfile.TemporaryDirectory() as directory:
            csv_path, xlsx_path = Path(directory)/'report.csv', Path(directory)/'report.xlsx'
            export_dump(csv_path, [row], fields=fields)
            export_dump(xlsx_path, [row], fields=fields)
            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(list(csv.reader(stream)), [list(fields), list(row)])
            with ZipFile(xlsx_path) as archive:
                xml = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            cells = xml.findall('s:sheetData/s:row', ns)[1].findall('s:c', ns)
            self.assertEqual(cells[26].get('r'), 'AA2')
            self.assertEqual(cells[-1].get('r'), 'AM2')
            self.assertEqual(xml.find('s:autoFilter', ns).get('ref'), 'A1:AM2')


class SettingsWindowTests(ShellFixture, unittest.TestCase):
    def prepare_connections(self, count=3):
        for i in range(count):
            self.window.store.put_cluster(f'Lab {i}', f'https://lab{i}.test:6443', 'edit' if i % 2 else 'admin', CONFIG.replace('https://example.test:6443', f'https://lab{i}.test:6443'))
        self.api.access_summary.return_value = dict(level='Редактор', detail='Synthetic')

    def test_settings_group_order_dropdowns_and_row_double_click(self):
        self.prepare_connections()
        edited = []
        def inspect(dialog):
            if isinstance(dialog, ClusterDialog):
                edited.append(dialog.cluster['id'])
                dialog.show()
                self.app.processEvents()
                self.assertGreater(dialog.controller_timezone.y(), dialog.account.y())
                dialog.reject()
                return QDialog.Rejected
            dialog.show()
            self.app.processEvents()
            table = next(table for table in dialog.findChildren(ResourceList) if table.property('clusterSettings'))
            self.assertEqual([table.item(r, 2).text() for r in range(3)], ['admin', 'admin', 'edit'])
            self.assertFalse(any(isinstance(w, QLineEdit) and 'IANA' in w.placeholderText() for w in dialog.findChildren(QLineEdit)))
            timeout = dialog.findChild(QComboBox, 'monitoringIdleMinutes')
            self.assertEqual(timeout.currentData(), 30)
            timeout.setCurrentIndex(timeout.findData(120))
            from PyQt5.QtTest import QTest
            QTest.qWait(250)
            self.assertEqual(self.window.store.preferences()['monitoring_idle_minutes'], 120)
            theme = dialog.findChild(QComboBox, 'settingsTheme')
            self.assertGreater(theme.mapTo(dialog, theme.rect().topLeft()).y(), timeout.mapTo(dialog, timeout.rect().topLeft()).y())
            entry = table.topLevelItem(1)
            cid = entry.data(0, Qt.UserRole)
            table.setCurrentItem(entry)
            table.cellDoubleClicked.emit(1, 1)
            self.assertEqual(edited, [cid])
            self.assertIsNotNone(table.itemWidget(entry, 3))
            dialog.accept()
            return QDialog.Accepted
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()

    def test_report_label_choice_persists_without_polling(self):
        page = self.window.dumps_page
        self.assertEqual(page.report_labels, [])
        snapshot = dict(server='https://lab.test', cluster_name='Lab', namespace_count=1, captured_at='2026-11-24T00:00:00+00:00', errors=[], records=[dict(namespace='ns', kind='Deployment', name='app', images=[], branch='', deployed_by='', version='', replicas=1, labels={'team': 'ops'})])
        page.snapshots = lambda: [snapshot]
        page.selected_clusters = lambda: []
        page.set_report_labels(['team', 'absent'])
        from PyQt5.QtTest import QTest
        QTest.qWait(250)
        self.assertEqual(self.window.store.preferences()['report_label_keys'], ['absent', 'team'])
        self.assertEqual(page.table.columnCount(), 11)
        self.assertEqual(page.table.item(0, 9).text(), '')
        self.assertEqual(page.table.item(0, 10).text(), 'ops')
        self.assertEqual(page.report_fields[-2:], ('label:absent', 'label:team'))
        page.exporting = True
        page.set_report_labels([])
        self.assertEqual(page.report_labels, ['absent', 'team'])


class VisibleClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matrix_fixture.MatrixTests.setUpClass()
        cls.app = matrix_fixture.MatrixTests.app
    setUp = matrix_fixture.MatrixTests.setUp
    tearDown = matrix_fixture.MatrixTests.tearDown

    def test_counter_colors_change_immediately_with_theme(self):
        self.window._select_theme('light')
        self.assertIn('#005FB8', self.window.summary_label.text())
        self.assertIn('#005FB8', self.window.workload_counters.text())
        self.assertNotIn('<a ', self.window.workload_counters.text())
        self.window._select_theme('dark')
        self.assertIn('#478FE8', self.window.summary_label.text())
        self.assertIn('#478FE8', self.window.workload_counters.text())

    def test_disabled_stale_connection_is_neither_rendered_nor_counted(self):
        window = self.window
        window.snapshot['off'] = copy.deepcopy(window.snapshot['c'])
        window.snapshot['deleted'] = copy.deepcopy(window.snapshot['c'])
        window.store.clusters = lambda: [dict(id='c', enabled=True), dict(id='off', enabled=False)]
        window.views.active['selectedClusterIds'] = ['c', 'off', 'deleted']
        window.render_matrix()
        self.assertEqual({c[0] for c in window._columns}, {'c'})
        self.assertEqual(window.scope_buttons['selectedClusterIds'][1].text(), 'Кластер: 1 ▾')
        window.views.active['selectedNamespaces'] = []
        window.render_matrix()
        self.assertEqual(window.scope_buttons['selectedClusterIds'][1].text(), 'Кластер: 0 ▾')

if __name__ == '__main__': unittest.main()
