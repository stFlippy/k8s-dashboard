import unittest
from unittest.mock import patch
from PyQt5.QtWidgets import QDialog
from PyQt5.QtCore import QCoreApplication, QEvent
from quaber.resource_list import ResourceList
from test_storage import CONFIG
from test_ux import ShellFixture

class SettingsLifetimeTests(ShellFixture, unittest.TestCase):
    def test_monitoring_controls_are_in_settings_with_identical_widths(self):
        for i in range(2):
            self.window.store.put_cluster('Cluster '+str(i), 'https://example.test:6443', 'alice', CONFIG)
        self.api.access_summary.return_value = {'level':'Чтение', 'detail':''}
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            table = next(t for t in dialog.findChildren(ResourceList) if t.property('clusterSettings'))
            widgets = [table.itemWidget(table.topLevelItem(i),3) for i in range(2)]
            self.assertEqual(widgets[0].width(), widgets[1].width())
            self.assertLess(widgets[0].width(), 200)
            widgets[0].setCurrentIndex(1)
            self.assertFalse(self.window.store.clusters()[0]['enabled'])
            dialog.accept()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()  # queued refresh must not touch the destroyed table
            return QDialog.Rejected
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()
