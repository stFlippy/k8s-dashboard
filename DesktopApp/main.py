"""Quaber desktop entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import QLockFile

from quaber.storage import UserStore
from quaber.app_identity import configure_application


def main() -> int:
    app = QApplication(sys.argv)
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    configure_application(app, app_dir)
    app.quaber_console_logging = True
    app.setStyle("Fusion")
    data_dir = app_dir / "UserData"
    try:
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
    result = app.exec()
    cleanup = getattr(window, "_cleanup_thread", None)
    if cleanup is not None:
        cleanup.join()
    instance_lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
