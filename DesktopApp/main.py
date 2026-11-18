"""Quaber desktop entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import PYQT_VERSION_STR, QLockFile, Qt, qVersion
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QApplication, QMessageBox

from quaber.storage import UserStore
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
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    configure_application(app, app_dir)
    app.quaber_console_logging = True
    app.setStyle(ApplicationStyle())
    data_dir = app_dir / "UserData"
    try:
        validate_qt_runtime()
        data_dir.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(data_dir.resolve() / "quaber.lock"))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(0):
            raise RuntimeError("Эта папка UserData уже используется другим экземпляром Quaber.")
        store = UserStore(data_dir)
        from quaber.ui import MainWindow
        window = MainWindow(store, app_dir)
    except Exception as exc:
        QMessageBox.critical(None, "Quaber — ошибка запуска", str(exc))
        return 1
    window.show()
    result = app.exec_()
    cleanup = getattr(window, "_cleanup_thread", None)
    if cleanup is not None:
        cleanup.join()
    instance_lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
