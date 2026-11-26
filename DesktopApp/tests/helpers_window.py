"""Register ownership cleanup before returning a window to a test body."""
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent
from quaber.ui import MainWindow


def create_window(test, store, root):
    window = MainWindow(store,root)
    def cleanup():
        if not sip.isdeleted(window):
            window._pod_operations.clear()
            for panel in getattr(window,'_resource_stack',[]):
                panel.yaml_editor.editor.document().setModified(False)
                panel.pending_operation = False
                panel._cancel_scale_draft(lock=False)
            window.close()
        worker = getattr(window,'_cleanup_thread',None)
        if worker is not None:
            worker.join(5)
            if worker.is_alive(): raise RuntimeError('Owned window cleanup did not finish.')
        if not sip.isdeleted(window): window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    test.addCleanup(cleanup)
    return window
