from unittest.mock import Mock
from PyQt5.QtWidgets import QApplication, QWidget
from quaber.pod_logs import PodLogs
from helpers_background import DeferredBackend as Deferred

class LogFixture:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def setUp(self):
        self.owner = QWidget()
        self.owner.controller = QWidget(self.owner)
        self.owner.background = Deferred()
        self.owner.api = Mock()
        self.owner.cluster_id, self.owner.namespace, self.owner.name = "c", "ns", "pod"
        self.view = PodLogs(self.owner, ["app", "sidecar"])
        self.app.processEvents()  # the single initial request
        self.view.timer.stop()
        self.addCleanup(self.owner.deleteLater)
        self.bg = self.owner.background
