"""Offline bounded display and atomic streaming export through the existing API."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from quaber.kubernetes import Kubernetes, LogTooLarge
from quaber.storage import UserStore
from quaber.request_policy import RequestPolicy
from test_storage import CONFIG


class GeneratedResponse:
    def __init__(self, size, fail=False):
        self.left = size
        self.fail = fail
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, count):
        self.reads.append(count)
        if self.fail and len(self.reads) == 3:
            raise OSError("fixture interrupted transfer")
        amount = min(count, self.left)
        self.left -= amount
        return b"x"*amount


class LogDownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = UserStore(self.root/"UserData")
        self.cluster = self.store.put_cluster("Fixture", "https://example.test:6443", "alice", CONFIG)
        proxy = Mock()
        proxy.url.return_value = "http://127.0.0.1:1234/prefix"
        self.api = Kubernetes(self.store, proxy, policy=RequestPolicy())
        self.api.opener = Mock()

    def test_download_streams_more_than_display_limit_in_small_chunks(self):
        response = GeneratedResponse(9*1024*1024)
        self.api.opener.open.return_value = response
        destination = self.root/"full.log"
        count = self.api.download_logs(self.cluster["id"], "ns", "pod", "app", True, destination)
        self.assertEqual(count, 9*1024*1024)
        self.assertEqual(destination.stat().st_size, count)
        self.assertLessEqual(max(response.reads), 65536)
        path = self.api.opener.open.call_args.args[0].full_url
        self.assertIn("previous=true", path)
        self.assertNotIn("limitBytes", path)
        self.assertNotIn("tailLines", path)

    def test_interrupted_download_keeps_original_and_removes_temporary_file(self):
        response = GeneratedResponse(9*1024*1024, fail=True)
        self.api.opener.open.return_value = response
        destination = self.root/"full.log"
        destination.write_bytes(b"previous export")
        with self.assertRaises(OSError):
            self.api.download_logs(self.cluster["id"], "ns", "pod", "app", False, destination)
        self.assertEqual(destination.read_bytes(), b"previous export")
        self.assertEqual(list(self.root.glob(".quaber-log-*")), [])

    def test_full_display_probe_detects_truncation_instead_of_silently_succeeding(self):
        self.api.opener.open.return_value = GeneratedResponse(4*1024*1024+1)
        with self.assertRaises(LogTooLarge):
            self.api.logs(self.cluster["id"], "ns", "pod", "app", tail=None)
        path = self.api.opener.open.call_args.args[0].full_url
        self.assertIn("limitBytes=4194305", path)
        self.assertNotIn("tailLines", path)

    def test_exact_display_boundary_is_not_rejected(self):
        self.api.opener.open.return_value = GeneratedResponse(4*1024*1024)
        text = self.api.logs(self.cluster["id"], "ns", "pod", "app", tail=None)
        self.assertEqual(len(text), 4*1024*1024)

    def test_unbounded_in_memory_response_is_not_permitted(self):
        with self.assertRaises(ValueError):
            self.api.request(self.cluster["id"], "/log", text=True, max_bytes=None)
        self.api.opener.open.assert_not_called()
