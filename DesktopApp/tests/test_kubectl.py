import os
import sys
import tempfile
import subprocess
import unittest
import time
from unittest.mock import patch
from pathlib import Path

from quaber.kubectl import ProxyManager, login, run_limited, test_connection, locate_kubectl
from quaber.processes import start_process, stop_process
from quaber.kubernetes import Kubernetes
from quaber.storage import UserStore
from test_storage import CONFIG


class KubectlTests(unittest.TestCase):
    def test_client_selection_is_explicit_then_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "kubectl.exe" if os.name == "nt" else "kubectl"
            client = root / name
            client.write_bytes(b"fixture")
            client.chmod(0o700)
            bundled = root / "addons" / name
            bundled.parent.mkdir()
            bundled.write_bytes(b"fixture")
            with patch.dict(os.environ, {"PATH": ""}):
                with self.assertRaisesRegex(FileNotFoundError, "PATH"):
                    locate_kubectl(root)
                self.assertEqual(locate_kubectl(root, str(client)), client.resolve())
            with patch.dict(os.environ, {"PATH": str(root)}):
                self.assertEqual(locate_kubectl(root), client.resolve())
                with self.assertRaises(FileNotFoundError):
                    locate_kubectl(root, str(root / "missing"))

    def test_timeout_stops_child_holding_inherited_pipes(self):
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('spawned',flush=True); time.sleep(60)"
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            run_limited([sys.executable, "-c", code], timeout=1, limit=1024)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.name == "nt", "Windows process timeout redaction")
    def test_shutdown_timeout_does_not_expose_command_credentials(self):
        from unittest.mock import Mock
        process = Mock()
        process._quaber_job = None
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired(["kubectl", "--password", "private-fixture-password"], 5)
        with self.assertRaises(TimeoutError) as failure:
            stop_process(process)
        self.assertNotIn("private-fixture-password", str(failure.exception))

    @unittest.skipUnless(os.name == "nt" and Path("Addons/kubectl.exe").exists() and Path("C:/Projects/kubectl.exe").exists(),
                         "Requires the Windows wrapper and native kubectl")
    def test_wrapper_proxy_close_does_not_wait_on_inherited_pipes(self):
        config = CONFIG.replace("certificate-authority-data: Y2E=", "insecure-skip-tls-verify: true")
        code = f"""
import tempfile
from pathlib import Path
from quaber.storage import UserStore
from quaber.kubectl import ProxyManager
with tempfile.TemporaryDirectory() as root:
    store = UserStore(Path(root))
    cluster = store.put_cluster('Fixture', 'https://example.test:6443', 'fixture', {config!r})
    proxy = ProxyManager(Path('Addons/kubectl.exe').resolve(), store)
    proxy.url(cluster)
    proxy.close()
    print('CLOSED')
"""
        process = start_process([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=12)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertIn(b"CLOSED", stdout)
        finally:
            stop_process(process)
            process.communicate(timeout=5)

    def test_process_output_limit(self):
        with self.assertRaisesRegex(RuntimeError, "превысил"):
            run_limited([sys.executable, "-c", "print('x' * 100000)"], timeout=5, limit=1024)

    @unittest.skipIf(os.name == "nt", "The mock kubectl is a POSIX executable script")
    def test_login_and_proxy_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mock = root / "kubectl"
            mock.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
config = {CONFIG!r}
if sys.argv[1] == 'login':
    assert sys.argv[2] == 'https://example.test:6443'
    assert sys.argv[3:5] == ['--username', 'alice']
    assert sys.argv[5:7] == ['--password', 'secret']
    assert os.environ['HOME'].startswith('/tmp/quaber-login-')
    path = Path(os.environ['KUBECONFIG'])
    path.write_text(config)
elif sys.argv[1] == 'proxy':
    prefix = next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--api-prefix='))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != prefix + '/api/v1/namespaces':
                self.send_response(404); self.end_headers(); return
            body = json.dumps({{'items':[{{'metadata':{{'name':'default'}}}}]}}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    print(f'Starting to serve on 127.0.0.1:{{server.server_port}}', flush=True)
    server.serve_forever()
else:
    sys.exit(2)
""")
            mock.chmod(0o700)
            syntax = subprocess.run([sys.executable, "-m", "py_compile", str(mock)], capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            store = UserStore(root / "UserData")
            config = login(mock, "https://example.test:6443", "alice", "secret")
            self.assertNotIn("secret", config)
            self.assertEqual(test_connection(mock, store, "https://example.test:6443", config), 1)
            cluster = store.put_cluster("One", "https://example.test:6443", "alice", config)
            proxy = ProxyManager(mock, store)
            try:
                api = Kubernetes(store, proxy)
                self.assertEqual(api.namespaces(cluster["id"]), ["default"])
                self.assertEqual(api.namespaces(cluster["id"]), ["default"])
            finally:
                proxy.close()


if __name__ == "__main__":
    unittest.main()
