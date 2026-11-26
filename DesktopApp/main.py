"""Quaber desktop entry point."""

from __future__ import annotations

import sys
import signal
from pathlib import Path

from PyQt5.QtCore import PYQT_VERSION_STR, Qt, qVersion, QTimer
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QApplication, QMessageBox

from quaber.bootstrap import acquire_store_lock, bootstrap_store, NOTICE
from quaber.app_identity import configure_application
from quaber.themes import ApplicationStyle


TARGET_PYQT = "5.15.9"
TARGET_QT = "5.15.2"


def validate_qt_runtime() -> None:
    if PYQT_VERSION_STR != TARGET_PYQT or qVersion() != TARGET_QT:
        raise RuntimeError(
            f"Quaber requires PyQt {TARGET_PYQT} / Qt {TARGET_QT}; "
            f"loaded PyQt {PYQT_VERSION_STR} / Qt {qVersion()}."
        )


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    from quaber.callback_diagnostics import failure_hook
    from quaber.shutdown import ShutdownCoordinator, release_window
    shutdown = ShutdownCoordinator(app)
    previous_hook = sys.excepthook
    previous_sigint = signal.getsignal(signal.SIGINT)
    sys.excepthook = failure_hook(app,shutdown)
    signal.signal(signal.SIGINT,shutdown.signal_interrupt)
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    configure_application(app, app_dir)
    app.quaber_console_logging = True
    app.setStyle(ApplicationStyle())
    instance_lock = None
    window = None
    try:
        validate_qt_runtime()
        shutdown.poll()
        if shutdown.accepted: return shutdown.code
        instance_lock = acquire_store_lock(app_dir)
        shutdown.poll()
        if shutdown.accepted: return shutdown.code
        store, recovered = bootstrap_store(instance_lock)
        shutdown.poll()
        if shutdown.accepted: return shutdown.code
        from quaber.ui import MainWindow
        window = MainWindow(store, app_dir)
        shutdown.attach(window)
        if not shutdown.accepted: window.show()
        if recovered:
            QTimer.singleShot(0, lambda: window.statusBar().showMessage(NOTICE, 30000))
        return app.exec_()
    except KeyboardInterrupt:
        shutdown.request('user_interrupt',130)
        return 130
    except Exception as exc:
        shutdown.poll()
        if shutdown.accepted: return shutdown.code
        from quaber.app_logging import redact
        QMessageBox.critical(None, "Quaber — ошибка запуска", redact(str(exc)))
        return shutdown.code if shutdown.accepted else 1
    finally:
        shutdown.timer.stop()
        cleanup = getattr(window, '_cleanup_thread', None) if window is not None else None
        if cleanup is not None: cleanup.join()
        release_window(window)
        if instance_lock is not None: instance_lock.unlock()
        signal.signal(signal.SIGINT,previous_sigint)
        sys.excepthook = previous_hook


if __name__ == "__main__":
    raise SystemExit(main())
