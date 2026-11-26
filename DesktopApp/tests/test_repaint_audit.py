"""Changed data must repaint; identical view state must preserve the document."""
import copy
import unittest
from unittest.mock import patch
from PyQt5.QtCore import QEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QTableWidgetItem
from quaber.log_source import LogSourceSelector
from quaber.resource_list import ResourceList
from quaber.ui import MetricsWidget
from quaber.widgets import PodDonut
from helpers_background import DeferredBackend
from test_loading_and_repaint import Paints
from test_ux import ShellFixture


class RepaintAuditTests(ShellFixture, unittest.TestCase):
    def test_manual_f5_reports_success_without_resetting_unchanged_overview(self):
        panel = self.open_panel('pod')
        with patch.object(panel.details, 'setHtml', wraps=panel.details.setHtml) as document, \
                patch.object(self.window.statusBar(), 'showMessage', wraps=self.window.statusBar().showMessage) as messages:
            self.window.shell_actions.refresh()
            self.wait()
            document.assert_not_called()
            self.assertTrue(any('карточка обновлена' in str(call.args[0]) for call in messages.call_args_list))

    def test_unchanged_deployment_overview_does_not_paint_and_status_change_does(self):
        panel = self.open_panel('deployment')
        panel.reload_deployment_pods()
        self.wait()
        QTest.qWait(180)
        widgets = (panel.details.viewport(), panel.pod_donut)
        paints = [Paints(widget) for widget in widgets]
        for unused in range(12):
            panel._adopt_live(copy.deepcopy(panel.resource))
        QTest.qWait(30)
        self.assertEqual([watcher.count for watcher in paints], [0, 0])
        panel._displayed_pods[0]['status']['phase'] = 'Failed'
        panel._adopt_live(copy.deepcopy(panel.resource))
        QTest.qWait(30)
        self.assertGreater(paints[1].count, 0)

    def test_donut_role_does_not_accumulate_and_new_target_repaints(self):
        donut = PodDonut(self.window)
        self.addCleanup(donut.deleteLater)
        pods = [copy.deepcopy(self.pod)]
        donut.set_pods(pods, 1, label='Новая версия')
        with patch.object(donut, 'update', wraps=donut.update) as updated:
            for unused in range(12):
                donut.set_pods(copy.deepcopy(pods), 1, label='Новая версия')
            updated.assert_not_called()
            self.assertEqual(donut.toolTip().count('Новая версия'), 1)
            donut.set_pods(pods, 2, requesting=True)
            updated.assert_called_once()
            self.assertNotIn('Новая версия', donut.toolTip())

    def test_unchanged_sidebar_does_not_repaint_and_theme_change_does(self):
        self.window.shell_actions.update_mode()
        QTest.qWait(180)
        paints = Paints(self.window.sidebar)
        with patch.object(self.window.brand_logo, 'setPixmap', wraps=self.window.brand_logo.setPixmap) as logo:
            for unused in range(12):
                self.window.shell_actions.update_mode()
            QTest.qWait(30)
            logo.assert_not_called()
            self.assertEqual(paints.count, 0)
            self.window._select_theme('light')
            QTest.qWait(30)
            logo.assert_called_once()
            self.assertGreater(paints.count, 0)

    def test_identical_view_color_does_not_paint_but_new_color_does(self):
        color = self.window.views.active.get('color', 'transparent')
        self.window._apply_view_color(color)
        QTest.qWait(180)
        paints = [Paints(widget) for widget in (self.window.matrix.viewport(), self.window.view_bar)]
        for unused in range(12):
            self.window._apply_view_color(color)
        QTest.qWait(30)
        self.assertEqual([watcher.count for watcher in paints], [0, 0])
        self.window._apply_view_color('#223344')
        QTest.qWait(30)
        self.assertGreater(paints[0].count, 0)

    def test_percent_graphs_apply_one_representation_and_keep_identical_chart(self):
        chart = MetricsWidget(self.window, self.api, DeferredBackend(), 'c', 'ns', 'pod')
        self.addCleanup(chart.deleteLater)
        chart.current = {'containers':[dict(name='app', cpu={'usageCores':.1,'limitCores':1},
                                           memory={'usageBytes':1024,'limitBytes':4096})]}
        chart.container.addItem('app')
        chart.mode.setCurrentIndex(0)
        chart.history_store.append(chart.history_identity, chart.current['containers'], now=1000)
        with patch('quaber.ui.time.time', return_value=1000):
            chart.render()
            with patch.object(chart.cpu, 'set_data', wraps=chart.cpu.set_data) as cpu, \
                    patch.object(chart.memory, 'set_data', wraps=chart.memory.set_data) as memory, \
                    patch.object(chart.cpu, 'update', wraps=chart.cpu.update) as updated:
                chart.render()
                cpu.assert_called_once()
                memory.assert_called_once()
                updated.assert_not_called()
            self.assertEqual(chart.cpu.values, [10])
            self.assertEqual(chart.memory.values, [25])
        chart.timer.stop()

    def test_log_source_does_not_repolish_identical_availability(self):
        selector = LogSourceSelector(self.window)
        self.addCleanup(selector.deleteLater)
        selector.set_availability('UNAVAILABLE')
        with patch.object(selector, 'setStyleSheet', wraps=selector.setStyleSheet) as styled:
            for unused in range(12):
                selector.set_availability('UNAVAILABLE')
            styled.assert_not_called()
            selector.set_availability('AVAILABLE')
            styled.assert_called_once()

    def test_clearing_hovered_resource_does_not_leave_deleted_item(self):
        table = ResourceList(self.window)
        self.addCleanup(table.deleteLater)
        table.show()
        self.app.processEvents()
        table.setRowCount(1)
        table.setItem(0, 0, QTableWidgetItem('one'))
        table._hovered_item = table.topLevelItem(0)
        table.clear()
        table.leaveEvent(QEvent(QEvent.Leave))
        self.assertIsNone(table._hovered_item)
        self.assertTrue(table.empty_label.isVisible())

    def test_repeated_event_refresh_does_not_change_model(self):
        panel = self.open_panel('pod')
        for label in ('События', 'Контейнеры'):
            table = next(panel.tabs.widget(i) for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == label)
            panel.tabs.setCurrentWidget(table)
            self.wait()
            changes = []
            table.model().dataChanged.connect(lambda *args: changes.append(args))
            with patch.object(table, 'setUpdatesEnabled', wraps=table.setUpdatesEnabled) as painting:
                table.refresh_data()
                self.wait()
                self.assertEqual(changes, [])
                painting.assert_not_called()


if __name__ == '__main__':
    unittest.main()
