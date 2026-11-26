import json
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
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
    def test_preferences_are_consistent_during_background_replacements(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            store.save_preferences({'sequence': 0, 'payload': '0'*4096})
            barrier = threading.Barrier(2)
            def writer():
                barrier.wait(timeout=3)
                for sequence in range(1, 81):
                    store.save_preferences({'sequence':sequence, 'payload':str(sequence)*4096})
            def reader():
                barrier.wait(timeout=3)
                for _ in range(160):
                    snapshot = store.preferences()
                    self.assertEqual(snapshot['payload'], str(snapshot['sequence'])*4096)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer), pool.submit(reader)]
                for future in futures:
                    future.result(timeout=20)

    def test_password_is_opt_in_encrypted_portable_and_removable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = UserStore(root)
            cluster = store.put_cluster("One", "https://example.test:6443", "alice", CONFIG,
                                        password="private-fixture-password", remember_password=True,
                                        kubectl_path="C:/clients/kubectl.exe")
            self.assertNotIn("private-fixture-password", store.clusters_file.read_text())
            self.assertEqual(UserStore(root).saved_password(cluster), "private-fixture-password")
            edited = store.put_cluster("Renamed", cluster["server"], "alice", None, cluster_id=cluster["id"])
            self.assertEqual(store.saved_password(edited), "private-fixture-password")
            self.assertEqual(edited["kubectlPath"], "C:/clients/kubectl.exe")
            store.update_logins({cluster["id"]: CONFIG}, password="new-fixture-password")
            self.assertEqual(store.saved_password(store.clusters()[0]), "new-fixture-password")
            removed = store.put_cluster("Renamed", cluster["server"], "alice", None, cluster_id=cluster["id"],
                                        remember_password=False, kubectl_path="")
            self.assertNotIn("encryptedPassword", removed)
            self.assertNotIn("kubectlPath", removed)

    def test_password_is_not_reused_for_another_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            cluster = store.put_cluster("One", "https://example.test:6443", "alice", CONFIG,
                                        password="private-fixture-password", remember_password=True)
            edited = store.put_cluster("One", cluster["server"], "bob", CONFIG, cluster_id=cluster["id"])
            self.assertNotIn("encryptedPassword", edited)
            other = store.put_cluster("Two", cluster["server"], "alice", CONFIG)
            store.update_logins({other["id"]: CONFIG}, password="not-opted-in")
            self.assertNotIn("encryptedPassword", store.clusters()[1])

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
        from helpers_permits import bound_permit
        with tempfile.TemporaryDirectory() as temporary:
            store = UserStore(Path(temporary))
            cluster = store.put_cluster("One", "https://example.test:6443", "alice", CONFIG)
            class DummyProxy:
                pass
            api = Kubernetes(store, DummyProxy())
            pod = {"metadata": {"uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 'resourceVersion':'1', "ownerReferences": [{"controller": True}]}}
            api.resource = lambda *args: pod
            api.request = lambda *args: {}
            with self.assertRaisesRegex(RuntimeError, "изменился"):
                api.delete_pod(cluster["id"], "default", "pod", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "restart", expected_version='1',permit=bound_permit(
                    api,(cluster['id'],'pod','default','pod','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'),'delete',{'mode':'restart','resourceVersion':'1'}))
            with self.assertRaisesRegex(ValueError, "Режим"):
                api.delete_pod(cluster["id"], "default", "pod", pod["metadata"]["uid"], "delete", expected_version='1',permit=bound_permit(
                    api,(cluster['id'],'pod','default','pod',pod['metadata']['uid']),'delete',{'mode':'delete','resourceVersion':'1'}))


if __name__ == "__main__":
    unittest.main()
