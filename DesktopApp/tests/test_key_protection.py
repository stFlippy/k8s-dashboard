import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from quaber.storage import UserStore, UnsupportedKeyProtection
from quaber.windows_secrets import PREFIX
from quaber.bootstrap import acquire_store_lock, bootstrap_store


class KeyProtectionTests(unittest.TestCase):
    def test_simulated_posix_recognizes_dpapi_without_treating_it_as_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'UserData'
            root.mkdir()
            protected = PREFIX+b'synthetic-dpapi-blob'
            (root/'portable.key').write_bytes(protected)
            posix = Mock(wraps=os)
            posix.name = 'posix'
            with patch('quaber.storage.os',posix):
                with self.assertRaises(UnsupportedKeyProtection) as result:
                    UserStore(root)
            self.assertEqual(result.exception.reason,'unsupported_protection')
            self.assertEqual((root/'portable.key').read_bytes(),protected)

    def test_simulated_posix_recovery_clears_userdata_and_defaults_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder)
            handle = acquire_store_lock(app)
            try:
                (handle.root/'portable.key').write_bytes(PREFIX+b'synthetic-dpapi-blob')
                sentinel = handle.root/'user-export.csv'
                sentinel.write_text('old computer data')
                posix = Mock(wraps=os)
                posix.name = 'posix'
                with patch('quaber.storage.os',posix), patch('quaber.bootstrap.os',posix):
                    store, recovered = bootstrap_store(handle)
                self.assertTrue(recovered)
                self.assertIs(store.preferences()['read_only'],True)
                self.assertEqual(store.preferences()['monitoring_idle_minutes'],30)
                self.assertEqual(store.clusters(),[])
                self.assertFalse(sentinel.exists())
            finally:
                handle.unlock()
