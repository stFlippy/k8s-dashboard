import unittest
from unittest.mock import Mock, patch
import yaml
from PyQt5.QtWidgets import QApplication, QWidget
from quaber.ui import ResourceDialog
from quaber.yaml_codec import dump_resource


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_safe_semantic_roundtrip(self):
        value = {'ключ': ['001', 'true', None, True, {}, [], '2026-11-18', '😀']}
        self.assertEqual(yaml.safe_load(dump_resource(value)), value)

    def test_thirty_close_cycles_release_timers_and_callbacks(self):
        from PyQt5.QtCore import QCoreApplication, QEvent, QTimer
        from PyQt5.QtTest import QTest
        from quaber.background import Background
        parent = QWidget()
        parent.preferences = {'read_only': True}
        bg = Background(parent)
        infrastructure_timers = set(parent.findChildren(QTimer))
        def cleanup():
            bg.stop()
            bg.pool.waitForDone(5000)
            bg._collect_retired()
            parent.close()
            parent.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.addCleanup(cleanup)
        api = Mock()
        api.cluster.return_value = {'name': 'C'}
        for _ in range(30):
            panel = ResourceDialog(parent, api, bg, 'c', 'configmap', 'ns', 'cm', {'metadata': {'uid': 'u'}})
            panel.tabs.setCurrentWidget(panel.yaml_editor)
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QTest.qWait(1)
        bg.pool.waitForDone(5000)
        deadline = __import__('time').monotonic()+5
        while (bg.running or bg._retired) and __import__('time').monotonic() < deadline:
            QTest.qWait(5)
        self.assertFalse(bg.running)
        self.assertFalse(bg._retired)
        self.assertEqual(parent.findChildren(ResourceDialog), [])
        self.assertEqual(set(parent.findChildren(QTimer)), infrastructure_timers)
        self.assertTrue(all(not timer.isActive() for timer in infrastructure_timers))

    def test_lazy_generation_and_save_guard(self):
        parent = QWidget()
        parent.preferences = {'read_only': True}
        api = Mock()
        api.cluster.return_value = {'name': 'C'}
        bg = Mock()
        panel = ResourceDialog(parent, api, bg, 'c', 'configmap', 'ns', 'cm',
                               {'metadata': {'name': 'cm', 'uid': 'u'}, 'data': {'key': 'value'}})
        try:
            self.assertFalse(panel._yaml_ready)
            self.assertEqual(panel.yaml_editor.toPlainText(), '')
            with patch.object(panel, '_ensure_write') as write:
                panel.save_yaml()
                write.assert_not_called()
            panel.tabs.setCurrentWidget(panel.yaml_editor)
            fn, done, failed = bg.submit.call_args.args
            panel._ensure_yaml_ready()
            self.assertTrue(panel._yaml_loading)
            done(fn())
            self.assertTrue(panel._yaml_ready)
            self.assertEqual(yaml.safe_load(panel.yaml_editor.toPlainText())['data']['key'], 'value')
            count = bg.submit.call_count
            panel._ensure_yaml_ready()
            self.assertEqual(bg.submit.call_count, count)
            panel._yaml_ready = False
            panel._ensure_yaml_ready()
            old_fn, old_done, _ = bg.submit.call_args.args
            panel._updated({'metadata': {'name': 'cm', 'uid': 'u', 'resourceVersion': '2'}, 'data': {'key': 'new'}}, notify=False)
            old_done(old_fn())
            self.assertEqual(yaml.safe_load(panel.yaml_editor.toPlainText())['data']['key'], 'new')
        finally:
            panel.background.active = False
            panel.deleteLater()
            parent.deleteLater()
