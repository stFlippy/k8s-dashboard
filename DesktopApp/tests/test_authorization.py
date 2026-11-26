import io
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from quaber.authorization import ClusterAuthorization, AuthorizationRequired
from quaber.kubernetes import Kubernetes
from quaber.storage import UserStore
from quaber.request_policy import RequestPolicy
from test_storage import CONFIG


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = UserStore(Path(folder.name))
        self.cluster = self.store.put_cluster("Test", "https://example.test:6443", "alice", CONFIG,
            password="private-password", remember_password=True)
        self.proxy = Mock()
        self.proxy.url.return_value = "http://127.0.0.1:1234"
        self.auth = ClusterAuthorization(self.store, self.proxy)

    def test_concurrent_requests_share_one_login(self):
        identifier, config = self.cluster["id"], self.cluster["encryptedKubeconfig"]
        with patch("quaber.authorization.locate_kubectl", return_value=Path("kubectl.exe")), patch("quaber.authorization.login", return_value=CONFIG) as login:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.auth.renew(identifier, config), range(6)))
        login.assert_called_once()
        self.assertTrue(all(c["id"] == identifier for c in results))
        self.proxy.close.assert_called_once_with(identifier)

    def test_failed_saved_password_blocks_retries_until_credentials_change(self):
        with patch("quaber.authorization.locate_kubectl", return_value=Path("kubectl.exe")), patch("quaber.authorization.login", side_effect=RuntimeError("rejected private-password")) as login:
            for _ in range(2):
                with self.assertRaises(AuthorizationRequired) as failure:
                    self.auth.renew(self.cluster["id"], self.cluster["encryptedKubeconfig"])
                self.assertNotIn("private-password", str(failure.exception))
            login.assert_called_once()
        self.store.update_logins({self.cluster["id"]: CONFIG})
        account = self.store.accounts()[0]
        self.store.put_account(account['name'], account['username'], 'corrected-password',
                               account_id=account['id'], remember_password=True)
        current = self.store.clusters()[0]
        with patch("quaber.authorization.locate_kubectl", return_value=Path("kubectl.exe")), patch("quaber.authorization.login", return_value=CONFIG) as login:
            self.auth.renew(current["id"], current["encryptedKubeconfig"])
            login.assert_called_once()

    def test_401_renews_then_retries_original_request_once(self):
        api = Kubernetes(self.store, self.proxy, policy=RequestPolicy())
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = io.BytesIO(b'{"items":[]}').read
        denied = urllib.error.HTTPError("http://localhost", 401, "Unauthorized", {}, io.BytesIO())
        api.opener = Mock()
        api.opener.open.side_effect = [denied, response]
        with patch("quaber.authorization.locate_kubectl", return_value=Path("kubectl.exe")), patch("quaber.authorization.login", return_value=CONFIG) as login:
            self.assertEqual(api.namespaces(self.cluster["id"]), [])
            login.assert_called_once()
        self.assertEqual(api.opener.open.call_count, 2)

    def test_no_password_notifies_ui_and_does_not_attempt_login(self):
        self.store.put_cluster("Test", self.cluster["server"], "alice", None, cluster_id=self.cluster["id"], remember_password=False)
        self.store = UserStore(self.store.root)  # The session-only secret ends at restart.
        api = Kubernetes(self.store, self.proxy, policy=RequestPolicy())
        api.authorization_required = Mock()
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError("http://localhost", 401, "Unauthorized", {}, io.BytesIO())
        with patch("quaber.authorization.login") as login:
            with self.assertRaises(AuthorizationRequired):
                api.namespaces(self.cluster["id"])
            login.assert_not_called()
        api.authorization_required.assert_called_once_with(self.cluster["id"])

    def test_rbac_403_does_not_trigger_login(self):
        api = Kubernetes(self.store, self.proxy, policy=RequestPolicy())
        api.authorization_required = Mock()
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError("http://localhost", 403, "Forbidden", {}, io.BytesIO())
        with patch("quaber.authorization.login") as login:
            with self.assertRaises(PermissionError):
                api.namespaces(self.cluster["id"])
            login.assert_not_called()
        api.authorization_required.assert_not_called()
