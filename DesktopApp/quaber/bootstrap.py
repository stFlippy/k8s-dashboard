"""Bounded portable-store recovery, before any UI, proxy or file logger starts."""
import ctypes
import json
import os
import re
import secrets
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QLockFile, QSysInfo
from .storage import KeyUnavailableError, UserStore

JOURNAL = 'quaber-recovery.json'
NOTICE = ('Данные UserData недоступны на этом компьютере или под этой учётной записью Windows. '
          'Папка очищена, создано новое хранилище; добавьте подключения заново.')
STAGES = {'portable.key': '.recovery-key', 'clusters.json': '.recovery-clusters',
          'preferences.json': '.recovery-preferences'}


def _no_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('UserData recovery does not follow links or reparse points.')


def checked_root(app_dir, root):
    app_dir, root = Path(app_dir).absolute(), Path(root).absolute()
    if root != app_dir / 'UserData' or root == root.parent:
        raise OSError('Unexpected UserData location; recovery refused.')
    for path in reversed((root, *root.parents)):
        _no_link(path)
    if root.resolve() != (app_dir.resolve() / 'UserData'):
        raise OSError('UserData location changed; recovery refused.')
    return root


def is_local_root(root):
    value = str(root)
    # Extended local paths are not UNC shares (GetDriveType cannot use every
    # extended-path spelling either).
    if value.startswith('\\\\?\\') and re.match(r'^[A-Za-z]:\\', value[4:]):
        value = value[4:]
    if value.startswith(('\\\\', '//')):
        return False
    if os.name != 'nt':
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel.GetDriveTypeW.restype = ctypes.c_uint
    from pathlib import PureWindowsPath
    return kernel.GetDriveTypeW(PureWindowsPath(value).anchor) in (2, 3, 6)


def _foreign_lock(path):
    with path.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        return False
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError:
        return False
    if (len(lines) not in (3, 5) or not re.fullmatch(r'[1-9][0-9]{0,9}', lines[0])
            or not lines[1] or len(lines[1]) > 256
            or any(ord(c) < 32 for line in lines for c in line)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,254}', lines[2])):
        return False
    hosts = {str(h).casefold().rstrip('.') for h in
             (socket.gethostname(), os.environ.get('COMPUTERNAME', ''), QSysInfo.machineHostName())}
    return lines[2].casefold().rstrip('.') not in hosts


@dataclass
class StoreLock:
    app_dir: Path
    root: Path
    lock: QLockFile

    def unlock(self):
        self.lock.unlock()


def acquire_store_lock(app_dir):
    app_dir = Path(app_dir).absolute()
    root = checked_root(app_dir, app_dir / 'UserData')
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'quaber.lock'
    _no_link(path)
    lock = QLockFile(str(path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        error = lock.error()
        if error == QLockFile.PermissionError:
            raise PermissionError('Нет прав записи или блокировки UserData.')
        if error != QLockFile.LockFailedError:
            raise OSError('Не удалось заблокировать UserData; данные не изменены.')
        if (not is_local_root(root) or not _foreign_lock(path)
                or not lock.removeStaleLockFile() or not lock.tryLock(0)):
            raise RuntimeError('UserData уже используется или lock нельзя безопасно восстановить.')
    return StoreLock(app_dir, root, lock)


def _reset_targets(root):
    """Plan a complete cleanup without following links or releasing the lock."""
    reserved = {'quaber.lock', JOURNAL, *STAGES.values()}
    targets = []
    resolved_root = root.resolve()
    def visit(directory):
        for path in directory.iterdir():
            _no_link(path)
            if not path.resolve().is_relative_to(resolved_root):
                raise OSError('UserData cleanup target escaped the data directory.')
            if path.parent == root and path.name in reserved:
                continue
            if path.is_dir():
                visit(path)
            elif not path.is_file():
                raise OSError('Unexpected UserData entry; cleanup refused.')
            # Postorder: children are removed before their directories.
            targets.append(path)
    visit(root)
    return targets


def _recover(handle, reason='unprotect'):
    root = checked_root(handle.app_dir, handle.root)
    if not handle.lock.isLocked():
        raise PermissionError('Для восстановления UserData требуется эксклюзивная блокировка.')
    journal = root / JOURNAL
    _no_link(journal)
    if journal.exists():
        with journal.open('rb') as stream:
            raw = stream.read(4097)
        document = json.loads(raw)
        if (len(raw) > 4096 or not isinstance(document, dict) or set(document) != {'version','reason','stage'}
                or document.get('version') != 1 or document.get('stage') != 'reset'
                or document.get('reason') not in {'unprotect','unsupported_protection'}):
            raise ValueError('Invalid UserData recovery journal; no data was removed.')
        reason = document['reason']
    if reason not in {'unprotect','unsupported_protection'}:
        raise ValueError('Unsupported recovery reason.')
    targets = _reset_targets(root)
    # Open all old targets and prepare a protected replacement before destroying any.
    for path in targets:
        if path.is_file():
            with path.open('r+b'):
                pass
    for name in (*STAGES.values(), JOURNAL):
        _no_link(root / name)
    key = secrets.token_bytes(32)
    if os.name == 'nt':
        from .windows_secrets import PREFIX, transform
        key = PREFIX + transform(key)
    documents = {'portable.key': key, 'clusters.json': b'{"version":1,"clusters":[]}\n',
                 'preferences.json': b'{"read_only":true,"monitoring_idle_minutes":30}\n'}
    for name, data in documents.items():
        UserStore._atomic_bytes(root / STAGES[name], data)
    UserStore._atomic_bytes(journal, (json.dumps({'version':1,'reason':reason,'stage':'reset'})+'\n').encode('utf-8'))
    # After a failure the journal stays: no mixed old/new set can be opened normally.
    for path in targets:
        _no_link(path)
        if not path.resolve().is_relative_to(root.resolve()):
            raise OSError('UserData cleanup target escaped the data directory.')
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    for name, stage in STAGES.items():
        _no_link(root / stage)
        _no_link(root / name)
        os.replace(root / stage, root / name)
    store = UserStore(root)
    if store.clusters() or store.preferences().get('read_only') is not True:
        raise ValueError('UserData recovery validation failed.')
    journal.unlink()
    return store


def bootstrap_store(handle):
    if not isinstance(handle, StoreLock) or not handle.lock.isLocked():
        raise PermissionError('UserData must be locked before opening it.')
    root = checked_root(handle.app_dir, handle.root)
    for name in ('portable.key', 'clusters.json', 'preferences.json', 'audit.jsonl'):
        _no_link(root / name)
    journal = root / JOURNAL
    _no_link(journal)
    if journal.exists():
        return _recover(handle), True
    try:
        store = UserStore(root)
        # Malformed JSON and individual credentials are not a key-recovery trigger.
        clusters = store.clusters()
        store.preferences()
        damaged = False
        from cryptography.exceptions import InvalidTag
        for cluster in clusters:
            if not isinstance(cluster, dict):
                raise ValueError('Invalid cluster record; local data was not reset.')
            try:
                for field in ('encryptedKubeconfig', 'encryptedPassword'):
                    if cluster.get(field):
                        store.decrypt(cluster[field])
            except (InvalidTag, ValueError, TypeError, AttributeError, UnicodeError):
                cluster['enabled'] = False
                cluster['credential_error'] = 'Saved credentials are unavailable. Sign in again.'
                damaged = True
        if damaged:
            store.save_clusters(clusters)
        if not store.clusters_file.exists():
            store.save_clusters([])
        if not store.preferences_file.exists():
            store.save_preferences({'read_only': True, 'monitoring_idle_minutes': 30})
        return store, False
    except KeyUnavailableError as exc:
        return _recover(handle, exc.reason), True
