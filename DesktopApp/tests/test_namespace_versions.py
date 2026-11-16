import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtCore import Qt
from quaber.inventory import APP_LABEL, CSV_FIELDS, compare_versions, inventory_record, namespace_snapshots
from quaber.inventory_pages import LABEL_COLORS
from test_dumps_versions_admin import snapshot, deployment
from test_storage import CONFIG
from test_ux import ShellFixture, Deferred


def data(cid):
    result = snapshot(cid)
    result['namespaces'] = ['dev', 'prod', 'empty']
    result['namespace_count'] = 3
    result['records'] = [inventory_record(deployment('orders-blue', '1'), 'Deployment', 'dev'),
                         inventory_record(deployment('orders-green', '2'), 'Deployment', 'prod')]
    for record in result['records']:
        record['labels'].pop('GIT_BRANCH')
    return result


class ComparisonTests(unittest.TestCase):
    def test_same_cluster_namespaces_are_independent_and_empty_labels_match(self):
        scopes = namespace_snapshots([data('a')], {('a', 'dev'), ('a', 'prod')})
        rows, _ = compare_versions(scopes, ['app.kubernetes.io/version', 'GIT_BRANCH'])
        self.assertEqual(rows[0]['status'], 'Различается')
        self.assertEqual(rows[0]['label_statuses']['GIT_BRANCH'], 'Совпадает')
        self.assertIn(('a', 'dev'), rows[0]['cells'])
        self.assertIn(('a', 'prod'), rows[0]['cells'])
        self.assertEqual(compare_versions(scopes, ['GIT_BRANCH'])[0][0]['status'], 'Совпадает')
        scopes[0]['records'][0]['labels']['GIT_BRANCH'] = 'release'
        self.assertEqual(compare_versions(scopes, ['GIT_BRANCH'])[0][0]['status'], 'Различается')

    def test_equal_namespace_names_in_different_clusters_and_absent_service(self):
        sources = [data('a'), data('b')]
        scopes = namespace_snapshots(sources, {('a', 'dev'), ('b', 'dev')})
        self.assertEqual(len(scopes), 2)
        self.assertEqual(compare_versions(scopes, ['app.kubernetes.io/version'])[0][0]['status'], 'Совпадает')
        scopes = namespace_snapshots(sources, {('a', 'dev'), ('a', 'empty')})
        self.assertEqual(compare_versions(scopes, ['GIT_BRANCH'])[0][0]['status'], 'Отсутствует')

    def test_errors_in_unselected_namespaces_do_not_prevent_valid_comparison(self):
        source = data('a')
        source['errors'] = ['empty / Deployment: Forbidden']
        source['namespace_errors'] = {'empty': source['errors']}
        scopes = namespace_snapshots([source], {('a', 'dev'), ('a', 'prod')})
        self.assertEqual(compare_versions(scopes, ['GIT_BRANCH'])[0][0]['status'], 'Совпадает')
        source['global_errors'] = ['Resource limit reached']
        scopes = namespace_snapshots([source], {('a', 'dev'), ('a', 'prod')})
        self.assertEqual(compare_versions(scopes, ['GIT_BRANCH'])[0][0]['status'], 'Неполные данные')


class NamespacePageTests(ShellFixture, unittest.TestCase):
    def cache(self):
        cluster = self.window.store.put_cluster('Lab', 'https://example.test:6443', 'user', CONFIG)
        source = data(cluster['id'])
        source.update(cluster_name='Lab', server=cluster['server'])
        self.window.inventory.snapshots[cluster['id']] = (self.window.inventory.signature(cluster), time.monotonic(), source)
        return cluster, source

    def test_failed_refresh_keeps_known_namespaces_and_never_reports_a_match(self):
        cluster, source = self.cache()
        service = self.window.inventory
        deferred = Deferred()
        with patch.object(self.window, 'background', deferred):
            service.refresh({cluster['id']}, force=True)
            deferred.finish(failure='Forbidden')
        failed = service.get(cluster['id'])
        self.assertEqual(set(failed['namespaces']), {'dev', 'prod', 'empty'})
        scopes = namespace_snapshots([failed, data('other')], {(cluster['id'], 'dev'), ('other', 'dev')})
        rows, _ = compare_versions(scopes, ['GIT_BRANCH'])
        self.assertEqual(rows[0]['status'], 'Неполные данные')
        self.assertEqual(rows[0]['cells'][(cluster['id'], 'dev')][0], 'Нет данных')

    def test_selected_namespace_disappearing_is_reported_as_unavailable(self):
        cluster, source = self.cache()
        page = self.window.versions_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        page.namespaces.selected = {(cluster['id'], 'dev'), (cluster['id'], 'prod')}
        page.label_keys = ['GIT_BRANCH']
        source['namespaces'] = ['dev']
        source['records'] = [record for record in source['records'] if record['namespace'] == 'dev']
        page.render()
        self.assertEqual(page.table.columnCount(), 4)
        self.assertEqual(page.table.item(0, 1).text(), 'Неполные данные')
        self.assertEqual(page.table.item(0, 3).text(), 'Нет данных')

    def test_namespace_picker_and_per_label_colors_in_both_themes(self):
        cluster, source = self.cache()
        page = self.window.versions_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        page.namespaces.selected = {(cluster['id'], 'dev'), (cluster['id'], 'prod')}
        page.label_keys = ['app.kubernetes.io/version', 'GIT_BRANCH']
        for theme in ('light', 'dark'):
            self.window._select_theme(theme)
            page.render()
            self.assertEqual(page.table.columnCount(), 4)
            self.assertEqual(page.table.headerItem().text(2), 'Lab / dev')
            self.assertEqual(page.table.headerItem().text(3), 'Lab / prod')
            self.assertEqual(page.table.item(0, 1).text(), 'Различается')
            from PyQt6.QtGui import QColor
            colors = page.table.item(0, 2).data(LABEL_COLORS)
            self.assertGreater(QColor(colors[0]).red(), QColor(colors[0]).green())
            self.assertGreater(QColor(colors[1]).green(), QColor(colors[1]).red())
        page.namespaces.selected = {(cluster['id'], 'dev')}
        page.render()
        self.assertEqual(page.table.item(0, 1).text(), 'Один namespace')

    def test_export_captures_visible_table_rows_before_save_dialog(self):
        cluster, source = self.cache()
        page = self.window.dumps_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        page.table.item(0, 5).setText('visible-branch')
        page.table.topLevelItem(1).setHidden(True)
        expected = [page.table.item(0, column).text() for column in range(8)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'dump.csv'
            def choose(*args):
                source['records'][0]['branch'] = 'different-background-value'
                return str(path), 'CSV'
            with patch('quaber.inventory_pages.QFileDialog.getSaveFileName', side_effect=choose):
                page.export()
                self.wait()
            with path.open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.reader(stream)
                self.assertEqual(next(reader), list(CSV_FIELDS))
                self.assertEqual(list(reader), [expected])
            self.assertFalse(path.with_suffix('.log').exists())
            self.assertIn('строк: 1', page.status.text())
