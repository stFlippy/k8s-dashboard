import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
from quaber.ui import MainWindow


class MemoryStore:
    def __init__(self, root):
        self.root = root
        self.prefs = {'refresh_ms': 0, 'read_only': True, 'kubectl_path': ''}
    def preferences(self): return copy.deepcopy(self.prefs)
    def save_preferences(self, value): self.prefs = copy.deepcopy(value)
    def clusters(self): return []
    def login_required(self, cluster): return False


class MatrixFixture:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')), \
             patch.object(MainWindow, '_prompt_expired_login'), \
             patch.object(MainWindow, '_load_initial_matrix'):
            self.window = MainWindow(MemoryStore(Path(self.temp.name)), Path(self.temp.name))
        self.pod = {'metadata': {'name': 'pod', 'uid': 'p1', 'managedFields': [{'fieldsV1': {'x': 'y'*100000}}],
                    'ownerReferences': [{'controller': True, 'kind': 'Deployment', 'uid': 'd'}]},
                    'status': {'phase': 'Running'}}
        self.window.snapshot = {'c': {'name': 'Cluster', 'namespaces': {'ns': {
            'deployments': [{'metadata': {'name': 'app', 'uid': 'd'}}],
            'pods': [self.pod], 'replica_sets': []}}}}
        self.window.store.clusters = lambda: [dict(id=cid, name=data['name'], enabled=True, server='https://synthetic.invalid', username='fixture') for cid, data in self.window.snapshot.items()]
        self.window.render_matrix()


    def tearDown(self):
        self.window.close()
        self.window._cleanup_thread.join(5)
        from quaber.shutdown import release_window
        release_window(self.window)
        self.app.processEvents()
        self.temp.cleanup()
