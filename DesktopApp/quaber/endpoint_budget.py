"""Per-user, cross-process endpoint budget. No credentials or resource data are stored."""
from contextlib import closing
import ctypes
import getpass
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid


def endpoint_key(server):
    parsed = urlsplit(server)
    host = (parsed.hostname or '').lower()
    if not host or parsed.scheme not in ('http', 'https'):
        return ''
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return hashlib.sha256(f'{parsed.scheme}://{host}:{port}'.encode()).hexdigest()


def process_alive(pid):
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Inaccessible is not proof of exit.
        try:
            code = ctypes.c_uint32()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class EndpointBudget:
    rate = 2.
    burst = 2.
    byte_rate = 4_000_000.
    byte_burst = 8_000_000.
    max_inflight = 2

    def __init__(self, path=None, clock=time.monotonic):
        if path is None:
            user = hashlib.sha256(getpass.getuser().encode()).hexdigest()[:16]
            path = Path(tempfile.gettempdir()) / ('quaber-budget-v1-' + user) / 'requests.sqlite3'
        self.path, self.clock = Path(path), clock
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if self.path.parent.is_symlink() or self.path.is_symlink():
                        raise OSError('The request-budget path must not be a symbolic link.')
                    with closing(sqlite3.connect(self.path, timeout=.1)) as db:
                        db.execute('PRAGMA journal_mode=WAL')
                        db.executescript("""
                            CREATE TABLE IF NOT EXISTS buckets
                                (endpoint TEXT PRIMARY KEY, tokens REAL, at REAL, bytes REAL);
                            CREATE TABLE IF NOT EXISTS leases
                                (token TEXT PRIMARY KEY, endpoint TEXT, pid INTEGER, expires REAL, heavy INTEGER);
                        """)
                        db.commit()
                    if os.name != 'nt':
                        self.path.chmod(0o600)
                    self._ready = True
        db = sqlite3.connect(self.path, timeout=.1)
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    def acquire(self, endpoint, *, monitor=True, heavy=False):
        now = self.clock()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM leases WHERE expires <= ? OR expires > ?', (now, now + 361))
            for token, pid in db.execute('SELECT token,pid FROM leases WHERE endpoint=?', (endpoint,)).fetchall():
                if not process_alive(pid):
                    db.execute('DELETE FROM leases WHERE token=?', (token,))
            row = db.execute('SELECT tokens,at,bytes FROM buckets WHERE endpoint=?', (endpoint,)).fetchone()
            tokens, at, byte_tokens = row if row is not None else (self.burst, now, self.byte_burst)
            if at > now:  # OS reboot resets the monotonic epoch.
                tokens, at, byte_tokens = self.burst, now, self.byte_burst
            tokens = min(self.burst, tokens + self.rate * (now - at))
            byte_tokens = min(self.byte_burst, byte_tokens + self.byte_rate * (now - at))
            active, heavies = db.execute('SELECT count(*),coalesce(sum(heavy),0) FROM leases WHERE endpoint=?', (endpoint,)).fetchone()
            allowed = tokens >= 1 and active < self.max_inflight and (not heavy or not heavies) and (not monitor or byte_tokens >= 0)
            lease = uuid.uuid4().hex if allowed else None
            db.execute('INSERT OR REPLACE INTO buckets VALUES(?,?,?,?)',
                       (endpoint, tokens - int(allowed), now, byte_tokens))
            if lease:
                db.execute('INSERT INTO leases VALUES(?,?,?,?,?)', (lease, endpoint, os.getpid(), now + 360, int(heavy)))
            db.execute('DELETE FROM buckets WHERE at < ? AND endpoint NOT IN (SELECT endpoint FROM leases)', (now - 86400,))
            db.commit()
            return lease

    def release(self, endpoint, lease, received=0):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM leases WHERE token=?', (lease,))
            db.execute('UPDATE buckets SET bytes=bytes-? WHERE endpoint=?', (received, endpoint))
            db.commit()
