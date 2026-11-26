"""Bounded GUI/file application logging with credential redaction."""
import logging
from logging.handlers import RotatingFileHandler
import re
import threading
import time
from collections import OrderedDict
from PyQt5.QtCore import QObject, pyqtSignal

_lock = threading.Lock()
_secrets = OrderedDict()
MAX_SECRETS = 128
SECRET_TTL = 300

_problems = OrderedDict()
_problem_lock = threading.Lock()


def log_problem(scope, message):
    """Keep complete visible failures, without flooding a polling journal."""
    safe = redact(str(message))
    key = (scope, safe)
    with _problem_lock:
        stamp = time.monotonic()
        previous = _problems.get(key, float('-inf'))
        if stamp-previous < 60:
            return
        _problems[key] = stamp
        _problems.move_to_end(key)
        while len(_problems) > 256:
            _problems.popitem(last=False)
    logging.getLogger('quaber.errors').error('%s: %s', scope, safe)


def register_secret(value):
    if value:
        with _lock:
            _secrets[value] = time.monotonic() + SECRET_TTL
            _secrets.move_to_end(value)
            while len(_secrets) > MAX_SECRETS:
                _secrets.popitem(last=False)


def redact(text):
    with _lock:
        for value, expiry in list(_secrets.items()):
            if expiry <= time.monotonic():
                del _secrets[value]
        secrets = sorted(_secrets, key=len, reverse=True)
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(--password(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\b(?:authorization)\s*[:=]\s*)(?:Bearer|Basic)\s+\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([\"']?\b(?:password|passwd|client_secret|id_token|refresh_token|token)\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}]+)", r"\1[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", text)
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class SafeRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # The default logging diagnostic prints raw record.args to stderr.
        # Never expose credentials if a disk write fails.
        if not getattr(self, "failure_reported", False):
            self.failure_reported = True
            try:
                self.error_callback("ERROR  Не удалось записать журнал на диск. Проверьте доступ и свободное место.")
            except RuntimeError:
                pass


class LogBridge(QObject):
    message = pyqtSignal(str)


class GuiHandler(logging.Handler):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record):
        try:
            self.bridge.message.emit(self.format(record))
        except RuntimeError:
            pass  # The owning window has already closed.


class LogSession:
    def __init__(self, root, parent=None, console=False):
        self.bridge = LogBridge(parent)
        self.logger = logging.getLogger("quaber")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handlers = []
        formatter = SafeFormatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        gui = GuiHandler(self.bridge)
        gui.setFormatter(formatter)
        self.handlers.append(gui)
        self.file_error = None
        try:
            directory = root / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            handler = SafeRotatingHandler(directory / "quaber.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler.error_callback = self.bridge.message.emit
            handler.setFormatter(formatter)
            self.handlers.append(handler)
        except OSError as exc:
            self.file_error = str(exc)
        if console:
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
            self.handlers.append(stream)
        for handler in self.handlers:
            self.logger.addHandler(handler)

    def close(self):
        for handler in self.handlers:
            self.logger.removeHandler(handler)
            handler.close()
        self.handlers.clear()
