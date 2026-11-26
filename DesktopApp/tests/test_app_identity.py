import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5.QtGui import QImage
from quaber import app_identity


class AppIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_png_decodes_and_is_available_before_any_main_window(self):
        image = QImage(str(app_identity.PUBLIC/"icon.png"))
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (192, 192))
        app_identity.configure_application(self.app, app_identity.PUBLIC.parent)
        self.assertEqual(self.app.desktopFileName(), "quaber")
        self.assertFalse(self.app.windowIcon().pixmap(32, 32).isNull())
        dialog = QDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.windowIcon().isNull())

    def test_png_icon_loads_without_any_webp_resource(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            shutil.copyfile(app_identity.PUBLIC/"icon.png", folder/"icon.png")
            with patch.object(app_identity, "PUBLIC", folder):
                self.assertEqual(app_identity.icon_path(), folder/"icon.png")
                self.assertFalse(app_identity.application_icon().pixmap(64, 64).isNull())

    def test_linux_shell_start_registers_matching_desktop_entry_in_xdg_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/"project with spaces"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root/"xdg")}), patch.object(app_identity.sys, "platform", "linux"):
                app_identity.configure_application(self.app, project)
            entry = root/"xdg"/"applications"/"quaber.desktop"
            content = entry.read_text(encoding="utf-8")
            self.assertIn("Name=Quaber\n", content)
            self.assertIn("StartupWMClass=Quaber\n", content)
            self.assertIn("icon.png", content)
            self.assertIn('main.py"', content)
            stamp = entry.stat().st_mtime_ns
            app_identity.register_desktop_entry(project, data_home=root/"xdg")
            self.assertEqual(entry.stat().st_mtime_ns, stamp)

    def test_desktop_registration_failure_keeps_application_icon_and_startup_working(self):
        with patch.object(app_identity.sys, "platform", "linux"),\
             patch.object(app_identity, "register_desktop_entry", side_effect=PermissionError("read-only XDG folder")),\
             self.assertLogs("quaber", level="WARNING"):
            app_identity.configure_application(self.app, app_identity.PUBLIC.parent)
        self.assertFalse(self.app.windowIcon().isNull())

    def test_desktop_exec_quotes_spaces_and_escapes_field_codes(self):
        argument = app_identity._exec_argument('/opt/My App/100% "special"/$python')
        self.assertTrue(argument.startswith('"') and argument.endswith('"'))
        self.assertIn("100%%", argument)
        self.assertIn('\\\\"special\\\\"', argument)
        self.assertIn('\\\\$python', argument)
