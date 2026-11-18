"""Use the native Windows caption for the same access-state color as the sidebar."""
import ctypes
import sys

from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication


def apply_caption_color(window, color=None):
    app = QApplication.instance()
    if sys.platform != "win32" or not app or app.platformName() != "windows":
        return False
    from ctypes import wintypes
    try:
        setter = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
        setter.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        setter.restype = ctypes.c_long
        caption = QColor(color) if color else None
        caption_value = (caption.red() | caption.green() << 8 | caption.blue() << 16) if caption else 0xFFFFFFFF
        text_value = 0xFFFFFF if caption else 0xFFFFFFFF
        results = []
        for attribute, value in ((35, caption_value), (36, text_value)):
            native = wintypes.DWORD(value)
            results.append(setter(int(window.winId()), attribute, ctypes.byref(native), ctypes.sizeof(native)) == 0)
        return all(results)
    except (OSError, AttributeError):
        return False
