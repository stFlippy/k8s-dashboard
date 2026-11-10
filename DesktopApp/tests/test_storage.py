import json
import tempfile
import unittest
from pathlib import Path

from quaber.storage import UserStore, sanitize_kubeconfig
from quaber.kubernetes import Kubernetes


CONFIG = """apiVersion: v1
kind: Config
current-context: c
clusters:
- name: cluster
  cluster:
    server: https://example.test:6443
    certificate-authority-data: Y2E=
users:
- name: user
  user:
    token: a.b.c
contexts:
- name: c
  context:
    cluster: cluster
    user: user
"""


class StoreTests(unittest.TestCase):
    def test_portable_credentials_and_no_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = UserStore(root)
            cluster = first.put_cluster("Production", "https://example.test:6443", "alice", CONFIG)
            stored = (root / "clusters.json").read_text()
            self.assertNotIn("a.b.c", stored)
            self.assertNotIn("password", stored)
            second = UserStore(root)
            self.assertEqual(sanitize_kubeconfig(second.decrypt(cluster["encryptedKubeconfig"]), cluster["server"]),
                             second.decrypt(cluster["encryptedKubeconfig"]))

    def test_reject_unsafe_kubeconfig_and_duplicate_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            store.put_cluster("One", "https://example.test:6443", "alice", CONFIG)
            with self.assertRaisesRegex(ValueError, "уже существует"):
                store.put_cluster("one", "https://example.test:6443", "alice", CONFIG)
            with self.assertRaisesRegex(ValueError, "неподдерживаемые"):
                sanitize_kubeconfig(CONFIG.replace("token: a.b.c", "exec: {command: sh}\n    token: a.b.c"), "https://example.test:6443")

    def test_missing_portable_key_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = UserStore(root)
            store.put_cluster("One", "https://example.test:6443", "alice", CONFIG)
            (root / "portable.key").unlink()
            with self.assertRaisesRegex(ValueError, "отсутствует portable.key"):
                UserStore(root)

    def test_group_login_updates_all_or_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            first = store.put_cluster("One", "https://example.test:6443", "alice", CONFIG)
            second = store.put_cluster("Two", "https://example.test:6443", "alice", CONFIG)
            before = (store.clusters_file).read_bytes()
            with self.assertRaisesRegex(ValueError, "bearer token"):
                store.update_logins({first["id"]: CONFIG, second["id"]: CONFIG.replace("token: a.b.c", "token: ''")})
            self.assertEqual(store.clusters_file.read_bytes(), before)
            store.update_logins({first["id"]: CONFIG, second["id"]: CONFIG})
            self.assertNotEqual(store.clusters_file.read_bytes(), before)

    def test_delete_pod_checks_uid_and_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            cluster = store.put_cluster("One", "https://example.test:6443", "alice", CONFIG)
            class DummyProxy:
                pass
            api = Kubernetes(store, DummyProxy())
            pod = {"metadata": {"uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "ownerReferences": [{"controller": True}]}}
            api.resource = lambda *args: pod
            api.request = lambda *args: {}
            with self.assertRaisesRegex(RuntimeError, "изменился"):
                api.delete_pod(cluster["id"], "default", "pod", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "restart")
            with self.assertRaisesRegex(ValueError, "Режим"):
                api.delete_pod(cluster["id"], "default", "pod", pod["metadata"]["uid"], "delete")


if __name__ == "__main__":
    unittest.main()
