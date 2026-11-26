"""User-visible regressions: empty selections, stable tables and bounded hints."""
import copy
import time
import unittest
from unittest.mock import patch

from PyQt5.QtCore import Qt, QEvent, QPoint
from PyQt5.QtGui import QFontMetrics, QHelpEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QToolTip, QLabel

from quaber.inventory_pages import VersionsPage
from quaber.quota_card import QuotaCard
from quaber.quota_controller import QuotaController
from quaber.tooltips import MAX_WIDTH, tooltip_text, wrap_tooltip
from test_dumps_versions_admin import snapshot
from test_loading_and_repaint import Paints, wait
from test_storage import CONFIG
from test_ux import ShellFixture


class PolishTests(ShellFixture, unittest.TestCase):
    def cache(self):
        clusters = []
        sources = {}
        for name in ('a', 'b'):
            server = f'https://{name}.test:6443'
            cluster = self.window.store.put_cluster(name, server, 'user', CONFIG.replace('https://example.test:6443', server))
            source = snapshot(cluster['id'])
            source.update(cluster_name=name, server=server)
            self.window.inventory.snapshots[cluster['id']] = (self.window.inventory.signature(cluster), time.monotonic(), source)
            sources[cluster['id']] = source
            clusters.append(cluster)
        collector = patch('quaber.inventory.collect_inventory',
                          side_effect=lambda api, cluster, progress=None: copy.deepcopy(sources[cluster['id']]))
        collector.start()
        self.addCleanup(collector.stop)
        return clusters

    def versions(self):
        self.cache()
        page = self.window.versions_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        self.wait()
        page.namespaces.selected = None  # explicit user choice, never a default
        page.set_label_keys(['app.kubernetes.io/version'])
        self.app.processEvents()
        return page

    def test_labels_persist_but_new_page_starts_with_no_namespaces(self):
        self.cache()
        page = self.window.versions_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        self.assertEqual(page.namespaces.ids(), set())
        self.assertEqual(page.table.rowCount(), 0)
        self.assertIn('Выберите неймспейсы и метки', page.table.empty_label.text())
        self.assertIn('GIT_BRANCH', page.available_labels)
        page.set_label_keys(['GIT_BRANCH', 'app.kubernetes.io/version'])
        wait(lambda: self.window.store.preferences().get('comparison_label_keys') == page.label_keys)
        self.assertEqual(self.window.store.preferences()['comparison_label_keys'], page.label_keys)
        page.namespaces.selected = None
        fresh = VersionsPage(self.window, self.window.inventory)
        try:
            self.assertEqual(fresh.label_keys, page.label_keys)
            self.assertEqual(fresh.namespaces.ids(), set())
        finally:
            fresh.deleteLater()

    def assert_stable(self, page):
        table = page.table
        table.selectRow(0)
        # Let first-show layout/selection updates finish before measuring refresh.
        QTest.qWait(150)
        selected = table.currentItem()
        paints = Paints(table.viewport())
        changes = []
        table.model().dataChanged.connect(lambda *args: changes.append(args))
        with patch.object(table, 'setUpdatesEnabled', wraps=table.setUpdatesEnabled) as updates:
            for unused in range(12):
                page.render()
            QTest.qWait(30)
        self.assertEqual(changes, [])
        self.assertIs(table.currentItem(), selected)
        self.assertEqual(paints.count, 0)
        updates.assert_not_called()

    def test_identical_dumps_does_not_repaint_or_reset_selection(self):
        self.cache()
        page = self.window.dumps_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        self.app.processEvents()
        self.assert_stable(page)
        self.assertEqual(page.labels_button.text(), 'Дополнительные labels…')

    def test_identical_comparison_does_not_repaint_but_changed_labels_and_theme_do(self):
        page = self.versions()
        self.assert_stable(page)
        source = page.snapshots()[1]
        source['records'][0]['labels']['app.kubernetes.io/version'] = '2'
        page.render()
        self.assertEqual(page.table.item(0, 0).data(Qt.UserRole), 'Различается')
        previous = page.table.item(0, 0).data(Qt.ForegroundRole)
        self.window._select_theme('light')
        self.assertNotEqual(page.table.item(0, 0).data(Qt.ForegroundRole), previous)

    def test_unchanged_configmap_keeps_selection_and_identity(self):
        table = self.window.configmap_controls[2]
        values = [dict(metadata=dict(name='config', uid='uid', resourceVersion='1'))]
        self.window._render_resources('configmap', values, ('c', 'apps', 'Lab'))
        table.selectRow(0)
        selected = table.currentItem()
        changes = []
        table.model().dataChanged.connect(lambda *args: changes.append(args))
        values[0]['metadata']['resourceVersion'] = '2'
        self.window._render_resources('configmap', values, ('c', 'apps', 'Lab'))
        self.assertEqual(changes, [])
        self.assertIs(table.currentItem(), selected)

    def test_unchanged_quotas_keep_cards_even_when_resource_version_changes(self):
        self.cache()
        self.window.tabs.setCurrentWidget(self.window.quotas_page)
        controller = QuotaController(self.window)
        quota = dict(metadata=dict(name='budget', resourceVersion='1'), status=dict(used={'pods':'2'}, hard={'pods':'10'}))
        def deliver():
            with patch('quaber.quota_controller.polling_allowed', return_value=True), \
                    patch.object(self.window.background, 'submit', return_value=True) as submit:
                controller.load()
                submit.call_args.args[1]([('Lab', 'apps', [quota], '')])
                submit.call_args.kwargs['finalized']('SUCCESS')
        deliver()
        cards = self.window.quotas_page.findChildren(QuotaCard)
        self.assertEqual(len(cards), 1)
        quota['metadata']['resourceVersion'] = '2'
        deliver()
        self.assertEqual(self.window.quotas_page.findChildren(QuotaCard), cards)
        quota['status']['used']['pods'] = '3'
        deliver()
        self.assertIsNot(self.window.quota_layout.itemAt(0).widget(), cards[0])

    def test_tooltips_wrap_long_russian_sentences_and_unbroken_names(self):
        metrics = QFontMetrics(QToolTip.font())
        original = 'Next run unavailable: часовой пояс контроллера неизвестен. Откройте Настройки → выберите кластер → Часовой пояс.\n' + 'deployment-' * 70
        wrapped = tooltip_text(wrap_tooltip(original))
        lines = wrapped.splitlines()
        self.assertGreater(len(lines), 3)
        self.assertTrue(all(metrics.horizontalAdvance(line) <= MAX_WIDTH for line in lines))
        self.assertEqual(''.join(wrapped.split()), ''.join(original.split()))

    def test_tooltip_on_empty_table_is_suppressed_but_real_cell_is_shown(self):
        page = self.versions()
        table = page.table
        table.setToolTip('Only over a real row')
        empty = QPoint(table.viewport().width()-5, table.viewport().height()-5)
        with patch('quaber.tooltips.QToolTip.showText') as show:
            self.app.sendEvent(table.viewport(), QHelpEvent(QEvent.ToolTip, empty, table.viewport().mapToGlobal(empty)))
            show.assert_not_called()
            point = table.visualRect(table.model().index(0, 0)).center()
            self.app.sendEvent(table.viewport(), QHelpEvent(QEvent.ToolTip, point, table.viewport().mapToGlobal(point)))
            self.assertEqual(show.call_count, 1)
            self.assertIn('<qt>', show.call_args.args[1])

    def test_no_maintenance_navigation_and_matrix_menu_has_no_delete(self):
        self.assertFalse(hasattr(self.window, 'maintenance_page'))
        self.assertFalse(hasattr(self.window, 'manage_workloads'))
        side = self.window.sidebar.layout()
        self.assertEqual(side.indexOf(self.window.log_nav_button)+1, side.indexOf(self.window.settings_button))
        self.assertNotIn('Maintenance mode', [button.text() for button in self.window.nav_buttons])
        identity = ('c', 'pod', 'apps', 'pod', 'uid')
        menu = self.window.resource_actions.menu(identity, allow_delete=False)
        self.addCleanup(menu.deleteLater)
        self.assertFalse(any('Удалить' in action.text() for action in menu.actions()))
        menu = self.window.resource_actions.menu(('c','cronjob','apps','nightly','uid'))
        self.addCleanup(menu.deleteLater)
        self.assertIn('Открыть', [action.text() for action in menu.actions()])
        self.assertNotIn('Открыть обзор', [action.text() for action in menu.actions()])

    def test_main_spinner_is_immediately_left_of_refresh(self):
        button = self.window.matrix_refresh_button
        layout = button.parentWidget().layout()
        self.assertEqual(layout.indexOf(self.window.loading_status)+1, layout.indexOf(button))
        self.assertFalse(self.window.loading_status.timer.isActive())

    def test_identical_matrix_does_not_paint_but_new_status_does(self):
        clusters = self.cache()
        self.window.snapshot = {cluster['id']: dict(name=cluster['name'], namespaces={
            'ns': dict(deployments=[self.deployment], pods=[self.pod],
                       replica_sets=self.api.replica_sets.return_value)}) for cluster in clusters}
        self.window.render_matrix()
        QTest.qWait(150)
        matrix = self.window.matrix
        watchers = [Paints(widget) for widget in (matrix.viewport(), matrix.frozen.viewport(), matrix.group_headers)]
        for unused in range(12):
            self.window.render_matrix()
        QTest.qWait(30)
        self.assertEqual([watcher.count for watcher in watchers], [0, 0, 0])
        point = matrix.content_rect(matrix.visualRect(matrix.model().index(0, 1)), 0).center()
        with patch.object(self.window.resource_actions, 'show') as show:
            self.window._pod_menu(point)
            self.assertEqual(show.call_args.kwargs, {'allow_delete': False})
        self.pod['status']['phase'] = 'Failed'
        self.window.render_matrix()
        QTest.qWait(30)
        self.assertGreater(watchers[0].count, 0)


if __name__ == '__main__':
    unittest.main()
