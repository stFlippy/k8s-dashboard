"""Quaber desktop entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox

from quaber.storage import UserStore


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Quaber")
    app.setStyle("Fusion")
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        store = UserStore(app_dir / "UserData")
        from quaber.ui import MainWindow
        window = MainWindow(store, app_dir)
    except Exception as exc:
        QMessageBox.critical(None, "Quaber — ошибка запуска", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
