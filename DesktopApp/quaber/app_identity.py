"""Application icon and Linux desktop identity for direct Python/shell launches."""
import logging
import os
from pathlib import Path
import sys
import tempfile
from PyQt5.QtGui import QIcon

APP_ID = "quaber"
PUBLIC = Path(__file__).resolve().parent.parent / "public"


def icon_path():
    return next((path for path in (PUBLIC/"icon.png", PUBLIC/"icon.webp", PUBLIC/"logo.png") if path.is_file()), PUBLIC/"icon.png")


def application_icon():
    for name in ("icon.png", "icon.webp", "logo.png"):
        icon = QIcon(str(PUBLIC/name))
        if not icon.isNull() and not icon.pixmap(32, 32).isNull():
            return icon
    return QIcon()


def _desktop_string(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _exec_argument(value):
    value = str(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\"+character)
    return _desktop_string('"'+value+'"')


def register_desktop_entry(app_dir, *, data_home=None):
    """Write an icon lookup entry in the current user's XDG directory; no root required."""
    app_dir = Path(app_dir).resolve()
    configured = data_home or os.environ.get("XDG_DATA_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else Path.home()/".local"/"share"
    entry = root/"applications"/(APP_ID+".desktop")
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(app_dir/"main.py")]
    content = ("[Desktop Entry]\nType=Application\nName=Quaber\n"
               "Exec="+" ".join(_exec_argument(argument) for argument in command)+"\n"
               "Icon="+_desktop_string(icon_path())+"\n"
               "Terminal=false\nNoDisplay=true\nStartupWMClass=Quaber\n")
    if entry.is_file() and entry.read_text(encoding="utf-8") == content:
        return entry
    entry.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=entry.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, entry)
    finally:
        temporary.unlink(missing_ok=True)
    return entry


def configure_application(app, app_dir):
    app.setApplicationName("Quaber")
    app.setApplicationDisplayName("Quaber")
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(application_icon())
    if sys.platform.startswith("linux"):
        try:
            register_desktop_entry(app_dir)
        except OSError as exc:
            logging.getLogger("quaber").warning("Не удалось зарегистрировать значок рабочего стола: %s", exc)
