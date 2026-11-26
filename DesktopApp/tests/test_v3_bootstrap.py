"""Synthetic temporary roots only; mocked DPAPI is not native DPAPI acceptance."""
import json
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from quaber.bootstrap import acquire_store_lock, bootstrap_store, checked_root, JOURNAL
from quaber.storage import KeyUnavailableError, UserStore
from quaber.windows_secrets import PREFIX


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quaber-v3-recovery-')
        self.base = Path(self.temp.name)
        self.app = self.base / 'copy'
        self.app.mkdir()
        self.root = self.app / 'UserData'
        self.root.mkdir()
        self.sentinel = self.base / 'outside-sentinel.txt'
        self.sentinel.write_bytes(b'outside never changes')
        self.handles = []
        def transform(blob, decrypt=False):
            if not decrypt:
                return b'synthetic-own:' + blob
            if blob.startswith(b'synthetic-own:'):
                return blob[len(b'synthetic-own:'):]
            raise OSError(13, 'synthetic foreign key')
        self.crypto = patch('quaber.windows_secrets.transform', side_effect=transform)
        self.crypto.start()

    def tearDown(self):
        for handle in self.handles: handle.unlock()
        self.crypto.stop()
        self.assertEqual(self.sentinel.read_bytes(), b'outside never changes')
        self.temp.cleanup()

    def acquire(self):
        handle = acquire_store_lock(self.app)
        self.handles.append(handle)
        return handle

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def foreign_data(self):
        (self.root / 'portable.key').write_bytes(PREFIX+b'synthetic-foreign')
        (self.root / 'clusters.json').write_text('{"version":1,"clusters":[{"id":"foreign"}]}')
        (self.root / 'preferences.json').write_text('{"read_only":false}')
        (self.root / 'audit.jsonl').write_text('synthetic audit\n')
        (self.root / 'export.txt').write_text('user export')
        (self.root / 'audit.jsonl.4').write_text('not a current app rotation; keep')
        for directory in ('logs', 'cache/nested', 'recovery-old/logs'):
            target = self.root / directory
            target.mkdir(parents=True, exist_ok=True)
            (target / 'old-data.txt').write_text('copied from another computer')

    def test_fresh_accessible_and_second_start_preserve_key(self):
        handle = self.acquire()
        store, reset = bootstrap_store(handle)
        self.assertFalse(reset)
        self.assertEqual(store.clusters(), [])
        self.assertTrue(store.preferences()['read_only'])
        self.assertTrue((self.root / 'portable.key').read_bytes().startswith(PREFIX))
        store.save_clusters([{'id': 'kept', 'encryptedKubeconfig': 'synthetic-invalid-field'}])
        (self.root / 'export.txt').write_text('local data stays')
        key = (self.root / 'portable.key').read_bytes()
        handle.unlock()
        other = self.acquire()
        store, reset = bootstrap_store(other)
        self.assertFalse(reset)
        self.assertEqual(store.clusters()[0]['id'], 'kept')
        self.assertFalse(store.clusters()[0]['enabled'])
        self.assertIn('Sign in again', store.clusters()[0]['credential_error'])
        self.assertEqual((self.root / 'portable.key').read_bytes(), key)
        self.assertEqual((self.root / 'export.txt').read_text(), 'local data stays')

    def test_foreign_key_clears_entire_userdata_without_backups_and_is_idempotent(self):
        self.foreign_data()
        handle = self.acquire()
        with self.assertRaises(KeyUnavailableError): UserStore(self.root)
        store, reset = bootstrap_store(handle)
        self.assertTrue(reset)
        self.assertEqual(store.clusters(), [])
        self.assertTrue(store.preferences()['read_only'])
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {'portable.key', 'clusters.json', 'preferences.json', 'quaber.lock'})
        self.assertTrue((self.root / 'quaber.lock').exists())
        self.assertFalse((self.root / 'audit.jsonl').exists())
        self.assertFalse((self.root / JOURNAL).exists())
        before = self.files()
        self.assertFalse(bootstrap_store(handle)[1])
        self.assertEqual(self.files(), before)

    def test_foreign_copied_lock_can_be_replaced_only_locally(self):
        self.foreign_data()
        (self.root / 'quaber.lock').write_text('424242424\nQuaber\nsynthetic-foreign-host\nforeign-machine\nforeign-boot\n')
        handle = self.acquire()
        self.assertTrue(handle.lock.isLocked())
        self.assertIn(socket.gethostname().casefold(), (self.root / 'quaber.lock').read_text().casefold())
        store, cleared = bootstrap_store(handle)
        self.assertTrue(cleared)
        self.assertEqual(store.clusters(), [])
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {'portable.key', 'clusters.json', 'preferences.json', 'quaber.lock'})

    def test_live_lock_and_unknown_foreign_metadata_do_not_reset(self):
        self.foreign_data()
        handle = self.acquire()
        before = self.files()
        with self.assertRaises(RuntimeError): acquire_store_lock(self.app)
        self.assertEqual(self.files(), before)
        handle.unlock()
        (self.root / 'quaber.lock').write_text('not valid lock metadata')
        before = self.files()
        with self.assertRaises(RuntimeError): acquire_store_lock(self.app)
        self.assertEqual(self.files(), before)

    def test_protect_or_read_failure_never_clear_old_data(self):
        self.foreign_data()
        handle = self.acquire()
        before = self.files()
        with patch('quaber.windows_secrets.transform', side_effect=OSError('synthetic protect failure')):
            with self.assertRaises(OSError): bootstrap_store(handle)
        self.assertEqual(self.files(), before)
        with patch('quaber.bootstrap.UserStore', side_effect=OSError('synthetic read denied')):
            with self.assertRaises(OSError): bootstrap_store(handle)
        self.assertEqual(self.files(), before)

    def test_owned_remote_lock_can_clear_foreign_userdata_without_backup(self):
        self.foreign_data()
        handle = self.acquire()
        with patch('quaber.bootstrap.is_local_root', return_value=False):
            store, recovered = bootstrap_store(handle)
        self.assertTrue(recovered)
        self.assertEqual(store.clusters(), [])
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {'portable.key', 'clusters.json', 'preferences.json', 'quaber.lock'})

    def test_crash_during_removal_keeps_journal_and_next_start_completes(self):
        self.foreign_data()
        handle = self.acquire()
        original = Path.unlink
        removed = []
        def unlink(path, *a, **k):
            if path.parent == self.root and path.name == 'clusters.json':
                raise OSError('synthetic crash after journal')
            removed.append(path.name)
            return original(path, *a, **k)
        with patch.object(Path, 'unlink', unlink):
            with self.assertRaises(OSError): bootstrap_store(handle)
        self.assertTrue((self.root / JOURNAL).exists())
        self.assertIn('version', json.loads((self.root / JOURNAL).read_text()))
        store, reset = bootstrap_store(handle)
        self.assertTrue(reset)
        self.assertEqual(store.clusters(), [])
        self.assertFalse((self.root / JOURNAL).exists())

    def test_wrong_root_and_malformed_json_are_not_foreign_key_reset(self):
        before = self.files()
        for root in (self.app, self.base, self.app / 'UserData-other'):
            with self.assertRaises(OSError): checked_root(self.app, root)
        self.assertEqual(self.files(), before)
        handle = self.acquire()
        bootstrap_store(handle)
        (self.root / 'preferences.json').write_text('broken json')
        before = self.files()
        with self.assertRaises(ValueError): bootstrap_store(handle)
        self.assertEqual(self.files(), before)

    def test_link_target_is_rejected_without_touching_sentinel(self):
        self.foreign_data()
        try:
            (self.root / 'unknown-link').symlink_to(self.sentinel)
        except OSError as exc:
            self.skipTest('native symlink creation unavailable: '+str(exc))
        handle = self.acquire()
        before = self.files()
        with self.assertRaises(OSError): bootstrap_store(handle)
        self.assertEqual(self.files(), before)

    def test_remote_foreign_lock_and_denied_target_are_unchanged(self):
        self.foreign_data()
        path = self.root / 'quaber.lock'
        path.write_text('424242424\nQuaber\nsynthetic-foreign-host\nforeign-machine\nforeign-boot\n')
        before = self.files()
        with patch('quaber.bootstrap.is_local_root', return_value=False):
            with self.assertRaises(RuntimeError): acquire_store_lock(self.app)
        self.assertEqual(self.files(), before)
        handle = self.acquire()
        before = self.files()
        original = Path.open
        def denied(path, mode='r', *args, **kwargs):
            if path.name == 'clusters.json' and mode == 'r+b':
                raise PermissionError('synthetic write denied')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            with self.assertRaises(PermissionError): bootstrap_store(handle)
        self.assertEqual(self.files(), before)

    def test_failure_at_each_staged_replace_can_resume_safely(self):
        for fail_at in range(1, 8):
            self.foreign_data()
            handle = self.acquire()
            old = self.files()
            original = os.replace
            count = [0]
            def replace(source, destination):
                count[0] += 1
                if count[0] == fail_at:
                    raise OSError('synthetic replace crash')
                return original(source, destination)
            with patch('quaber.bootstrap.os.replace', side_effect=replace):
                with self.assertRaises(OSError): bootstrap_store(handle)
            remaining = self.files()
            if fail_at <= 4:
                self.assertEqual(remaining['export.txt'], old['export.txt'])
                for name in ('portable.key', 'clusters.json', 'preferences.json', 'audit.jsonl'):
                    self.assertEqual(remaining[name], old[name])
            else:
                self.assertIn(JOURNAL, remaining)
            store, reset = bootstrap_store(handle)
            self.assertTrue(reset)
            self.assertEqual(store.clusters(), [])
            self.assertTrue(store.preferences()['read_only'])
            self.assertFalse((self.root / JOURNAL).exists())
            handle.unlock()

    def test_racing_starts_have_at_most_one_lock_owner(self):
        self.foreign_data()
        before = self.files()
        barrier = threading.Barrier(2)
        finished, release = threading.Event(), threading.Event()
        attempts = []
        mutex = threading.Lock()
        def start():
            barrier.wait(timeout=2)
            try:
                handle = acquire_store_lock(self.app)
            except RuntimeError:
                handle = None
            with mutex:
                attempts.append(handle is not None)
                if len(attempts) == 2: finished.set()
            if handle:
                release.wait(3)
                handle.unlock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start) for _ in range(2)]
            try:
                self.assertTrue(finished.wait(2))
                self.assertEqual(sorted(attempts), [False, True])
                current = self.files()
                for name, contents in before.items(): self.assertEqual(current[name], contents)
            finally:
                release.set()
            for future in futures: future.result(timeout=2)
