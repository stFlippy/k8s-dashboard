"""Empty tabs, visible selected namespaces and readable application-log search."""
import unittest
from unittest.mock import patch
from PyQt5.QtGui import QColor, QPalette
from quaber.themes import theme
from test_storage import CONFIG
from test_ux import ShellFixture


def contrast(first, second):
    def luminance(color):
        channels = [color.redF(),color.greenF(),color.blueF()]
        channels = [value/12.92 if value <= .04045 else ((value+.055)/1.055)**2.4 for value in channels]
        return sum(value*weight for value,weight in zip(channels,(.2126,.7152,.0722)))
    high,low = sorted((luminance(first),luminance(second)),reverse=True)
    return (high+.05)/(low+.05)


class TabsAndLoginTests(ShellFixture,unittest.TestCase):
    def populate(self):
        cluster = self.window.store.put_cluster('Lab','https://example.test:6443','user',CONFIG)
        self.window.snapshot = {cluster['id']:dict(name='Lab',namespaces={
            'busy':dict(deployments=[self.deployment],pods=[],replica_sets=[]),
            'empty':dict(deployments=[],pods=[],replica_sets=[])})}
        self.window.render_matrix()
        return cluster

    def test_empty_namespaces_always_show_in_all_tabs_and_after_filtering(self):
        self.populate()
        window = self.window
        first = window.views.active
        first['selectedNamespaces'] = None
        window.render_matrix()
        self.assertFalse(hasattr(window,'hide_empty'))
        self.assertEqual(sum(not window.matrix.isColumnHidden(c) for c in range(1,window.matrix.columnCount())),2)
        self.assertEqual(window.scope_buttons['selectedNamespaces'][1].text(),'Namespace: Все ▾')
        self.assertNotIn('hideFilteredNamespaces',first)
        window.search.setText('no-such-workload')
        window.render_matrix()
        self.assertEqual(window.matrix.rowCount(),0)
        self.assertEqual(sum(not window.matrix.isColumnHidden(c) for c in range(1,window.matrix.columnCount())),2)
        window.search.clear()
        other = window.views.add('Other')
        other.update(selectedClusterIds=None,
                     selectedDeployments=['app','obsolete-resource'])
        with patch.object(window,'_matrix_scope_changed'):
            window._load_view_controls()
        self.assertIsNone(other['selectedNamespaces'])
        self.assertEqual(sum(not window.matrix.isColumnHidden(c) for c in range(1,window.matrix.columnCount())),2)
        self.assertNotIn('непустые',window.scope_buttons['selectedNamespaces'][1].text())
        self.assertIn('Deployment: Все',window.scope_buttons['selectedDeployments'][1].text())
        self.assertNotIn('hideFilteredNamespaces',other)
        window.matrix.setColumnHidden(2,True)
        window.render_matrix()
        self.assertFalse(window.matrix.isColumnHidden(2))

    def test_new_tab_is_empty_and_sends_no_background_cluster_reads(self):
        self.populate()
        window = self.window
        old_snapshot = window.snapshot
        with patch('quaber.ui.QInputDialog.getText',return_value=('Empty',True)):
            window._add_view()
        self.assertEqual(window.views.active['selectedClusterIds'],[])
        self.assertEqual(window.matrix.rowCount(),0)
        self.assertEqual(window.matrix.columnCount(),1)
        with patch.object(window.background,'submit',wraps=window.background.submit) as submit, \
                patch('quaber.ui.polling_allowed',return_value=True):
            window.refresh_matrix()
            submit.assert_not_called()
        self.assertIs(window.snapshot,old_snapshot)
        self.assertIn('Выберите кластеры',window.statusBar().currentMessage())

    def test_log_search_placeholder_remains_readable_after_theme_and_focus_change(self):
        field = self.window.log_panel.search_field
        self.window.navigate_workspace(self.window.tabs.indexOf(self.window.log_panel))
        for name in ('light','dark','light','dark'):
            self.window._select_theme(name)
            field.setFocus()
            self.app.processEvents()
            colors = theme(name=='light')
            for group in (QPalette.Active,QPalette.Inactive,QPalette.Disabled):
                color = field.palette().color(group,QPalette.PlaceholderText)
                self.assertEqual(color,QColor(colors['muted']))
                self.assertEqual(color.alpha(),255)
                self.assertGreaterEqual(contrast(color,QColor(colors['input'])),4.5)


if __name__ == '__main__':
    unittest.main()
