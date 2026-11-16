import base64
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'Addons' / 'kubectl'
spec = importlib.util.spec_from_file_location('windows_login', SCRIPT.with_name('kubectl_windows.py'))
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
BASH = Path('C:/Program Files/Git/bin/bash.exe')

class LoginFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('QUABER_')}
        self.env.update(HOME=str(self.root), KUBECONFIG=str(self.root / 'config'))
        # Run the unchanged source with LF line endings under Git Bash.
        self.reference = self.root / 'linux-kubectl'
        self.reference.write_text(SCRIPT.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')

    def invoke(self, argv, env=None, stdin=''):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env or self.env, clear=True), patch('sys.stdin', io.StringIO(stdin)), redirect_stdout(output), redirect_stderr(errors):
            code = client.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def reference_run(self, argv, env=None, stdin=''):
        command = [str(BASH), '--noprofile', '--norc', self.reference.as_posix(), *argv]
        # Git Bash initializes HOME at startup; test the script's own empty-HOME
        # behavior by clearing it after startup.
        if (env or self.env).get('HOME') == '':
            command = [str(BASH), '--noprofile', '--norc', '-c', 'HOME=; source "$@"', 'kubectl', self.reference.as_posix(), *argv]
        result = subprocess.run(command,
                                env=env or self.env, input=stdin, capture_output=True, encoding='utf-8', timeout=20)
        return result.returncode, result.stdout, result.stderr

class WindowsLoginTests(LoginFixture):
    @unittest.skipUnless(BASH.exists(), 'Git Bash is required for differential checks')
    def test_cli_matches_original_stdout_stderr_and_exit_code(self):
        cases = [[], ['--help'], ['-h'], ['--version'], ['login', '--help'], ['-login', '--version'],
                 ['login', '--username'], ['login', '--password='], ['login', '--server'],
                 ['login', '--onlytoken=bad'], ['login', '--onlytoken', 'false', 'extra'],
                 ['login', 'https://a', 'https://b'], ['login', '--api-insecure-skip-tls-verify'],
                 ['login', '--oidc-issuer-url', 'https://dex'], ['login', '--api-ca-file', 'ca'],
                 ['login', 'http://api.test'], ['login', 'https://api.test/path'],
                 ['login', 'https://192.168.56.11:6443'], ['login', 'https://api.test'], ['login']]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv), self.reference_run(argv))

    @unittest.skipUnless(BASH.exists(), 'Git Bash is required for differential checks')
    def test_environment_validation_matches_original(self):
        cases = [('QUABER_API_INSECURE_SKIP_TLS_VERIFY', 'invalid', ['--help']),
                 ('QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY', 'invalid', ['--version']),
                 ('QUABER_LOGIN_TIMEOUT', '0', ['login', 'https://api.test']),
                 ('QUABER_LOGIN_TIMEOUT', '601', ['login', 'https://api.test']),
                 ('QUABER_OIDC_ISSUER_URL', 'http://dex.test', ['login', 'https://api.test'])]
        for name, value, argv in cases:
            env = dict(self.env, **{name: value})
            if name == 'HOME': env.pop('KUBECONFIG')
            with self.subTest(name=name, value=value):
                self.assertEqual(self.invoke(argv, env), self.reference_run(argv, env))

    def test_json_config_is_not_used(self):
        (self.root / 'kubectl-login.json').write_text('{"defaults":{"oidc-issuer-url":"https://dex.test"}}')
        code, _, errors = self.invoke(['login', 'https://api.test'])
        self.assertEqual(code, 1)
        self.assertEqual(errors, 'Ошибка: для неизвестного кластера укажите --oidc-issuer-url\n')

    def test_ca_readability_is_checked_when_tls_is_enabled(self):
        env = dict(self.env, QUABER_API_INSECURE_SKIP_TLS_VERIFY='false')
        code, _, errors = self.invoke(['login', 'https://192.168.56.11:6443'], env)
        self.assertEqual(code, 1)
        self.assertIn('/home/flippy/k8s-lab/kubeconfigs/k8s-lab-1-ca.crt', errors)

    def test_default_insecure_login_does_not_require_ca_files(self):
        code, _, errors = self.invoke(['login', 'https://192.168.56.11:6443'])
        self.assertEqual(code, 1)
        self.assertEqual(errors, 'Server: https://192.168.56.11:6443\n\nОшибка: укажите --username при неинтерактивном запуске\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows profile fallback')
    def test_login_without_home_or_kubeconfig_reaches_credentials(self):
        env = dict(self.env, USERPROFILE=str(self.root))
        env.pop('HOME')
        env.pop('KUBECONFIG')
        code, _, errors = self.invoke(['login', 'https://192.168.56.12:6443'], env)
        self.assertEqual(code, 1)
        self.assertIn('--username', errors)
        self.assertNotIn('HOME', errors)

    @unittest.skipUnless(os.name == 'nt', 'Windows profile fallback')
    def test_kubeconfig_location_priority(self):
        profile = self.root / 'profile'
        custom_home = self.root / 'custom-home'
        profile_config = profile / '.kube' / 'config'
        home_config = custom_home / '.kube' / 'config'
        explicit_config = self.root / 'explicit-config'
        for config in (profile_config, home_config, explicit_config):
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text('fixture')
        cases = [({'USERPROFILE': str(profile)}, profile_config),
                 ({'USERPROFILE': str(profile), 'HOME': ''}, profile_config),
                 ({'USERPROFILE': str(profile), 'HOME': str(custom_home)}, home_config),
                 ({'USERPROFILE': str(profile), 'HOME': str(custom_home), 'KUBECONFIG': str(explicit_config)}, explicit_config)]
        for overrides, expected in cases:
            env = {k: v for k, v in self.env.items() if k not in ('HOME', 'KUBECONFIG')}
            env.update(overrides)
            with self.subTest(overrides=overrides), patch.object(client, 'find_real_kubectl', return_value=Path('native.exe')), patch.object(client, 'native_value', return_value='https://api.test') as read:
                self.invoke(['login'], env)
                self.assertEqual(read.call_args.args[1], expected)

    def test_oidc_ca_readability_is_checked_when_tls_is_enabled(self):
        env = dict(self.env, QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY='false')
        code, _, errors = self.invoke(['login', 'https://192.168.56.11:6443'], env)
        self.assertEqual(code, 1)
        self.assertIn('не удаётся прочитать CA Dex:', errors)

    def test_native_discovery_probes_path_candidates_and_skips_self(self):
        wrapper_dir = self.root / 'wrapper'
        wrapper_dir.mkdir()
        (wrapper_dir / 'kubectl.exe').write_bytes(b'wrapper')
        failed, native = self.root / 'failed', self.root / 'native'
        failed.mkdir()
        native.mkdir()
        for folder in (failed, native): (folder / 'kubectl.exe').write_bytes(b'fixture')
        env = dict(self.env, PATH=os.pathsep.join(map(str, (wrapper_dir, failed, native))))
        with patch.dict(os.environ, env, clear=True), patch.object(client.sys, 'frozen', True, create=True), patch.object(client.sys, 'executable', str(wrapper_dir / 'kubectl.exe')), patch.object(client.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as run:
            self.assertEqual(client.find_real_kubectl(), (native / 'kubectl.exe').resolve())
            self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(os.name == 'nt', 'Persisted Windows PATH')
    def test_native_discovery_uses_saved_path_if_process_path_is_stale(self):
        native_dir = self.root / 'installed client'
        native_dir.mkdir()
        native = native_dir / 'kubectl.exe'
        native.write_bytes(b'fixture')
        env = dict(self.env, PATH=str(self.root / 'stale'), CLIENT_LOCATION=str(native_dir))
        with patch.dict(os.environ, env, clear=True), patch.object(client, 'windows_saved_path_entries', return_value=['%CLIENT_LOCATION%']), patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(client.find_real_kubectl(), native.resolve())
            run.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Persisted Windows PATH')
    def test_process_path_has_priority_over_saved_path(self):
        native = self.root / 'kubectl.exe'
        native.write_bytes(b'fixture')
        env = dict(self.env, PATH=str(self.root))
        with patch.dict(os.environ, env, clear=True), patch.object(client, 'windows_saved_path_entries', return_value=iter(())), patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(client.find_real_kubectl(), native.resolve())

    @unittest.skipUnless(os.name == 'nt', 'Persisted Windows PATH')
    def test_saved_path_still_skips_self_and_invalid_clients(self):
        wrapper = self.root / 'wrapper'
        failed = self.root / 'failed'
        native = self.root / 'native'
        for directory in (wrapper, failed, native):
            directory.mkdir()
            (directory / 'kubectl.exe').write_bytes(b'fixture')
        with patch.dict(os.environ, dict(self.env, PATH=str(self.root / 'absent')), clear=True), patch.object(client.sys, 'frozen', True, create=True), patch.object(client.sys, 'executable', str(wrapper / 'kubectl.exe')), patch.object(client, 'windows_saved_path_entries', return_value=map(str, (wrapper, failed, native))), patch.object(client.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as run:
            self.assertEqual(client.find_real_kubectl(), (native / 'kubectl.exe').resolve())
            self.assertEqual(run.call_count, 2)

    def test_curl_tls_flags_and_secrets_are_not_command_arguments(self):
        with patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'200')) as run:
            self.assertEqual(client.curl_request('curl.exe', 'https://dex.test/token', self.root / 'out', '20', 'ca.crt', True,
                             config=self.root / 'curl.conf', data=self.root / 'form'), '200')
            args = run.call_args.args[0]
            self.assertIn('--insecure', args)
            self.assertNotIn('--cacert', args)
            self.assertIn('--max-time', args)
            self.assertIn('@' + str(self.root / 'form'), args)
        with patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'200')) as run:
            client.curl_request('curl.exe', 'https://api.test', self.root / 'out', '20', 'ca.crt', False)
            self.assertIn('--cacert', run.call_args.args[0])
            self.assertNotIn('--insecure', run.call_args.args[0])

    def test_namespace_preservation_and_failed_merge_leave_old_config(self):
        config = self.root / 'config'
        config.write_text('previous')
        temporary = self.root / 'temporary'
        temporary.mkdir()
        with patch.object(client, 'native_value', side_effect=['api.test', 'team']), patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(client.LoginError, 'не удалось объединить kubeconfig'):
                client.save_config(config, temporary, Path('native.exe'), 'https://api.test', 'user', 'a.b.c',
                                   ['default', 'team'], '', '', '', '', False)
        self.assertEqual(config.read_text(), 'previous')
        self.assertIn("namespace: 'team'", (temporary / 'new-login.yaml').read_text())
        self.assertFalse(list(self.root.glob('.config.kubectl-login.*.tmp')))

    def test_401_reports_clock_skew(self):
        headers = self.root / 'headers'
        headers.write_text('Date: Wed, 01 Jan 2020 00:00:00 GMT\n')
        with self.assertRaisesRegex(client.LoginError, 'рассинхронизация времени'):
            client.check_api_status('401', headers)

    def test_active_server_is_read_from_kubeconfig(self):
        (self.root / 'config').write_text('fixture')
        with patch.object(client, 'find_real_kubectl', return_value=Path('native.exe')), patch.object(client, 'native_value', return_value='https://api.test'):
            code, _, errors = self.invoke(['login'])
        self.assertEqual(code, 1)
        self.assertIn('для неизвестного кластера', errors)


@unittest.skipUnless(BASH.exists(), 'Git Bash is required for differential HTTPS checks')
class HTTPSParityTests(LoginFixture):
    def test_https_onlytoken_and_errors_match_reference(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256())
        cert_file, key_file = self.root / 'ca.pem', self.root / 'key.pem'
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        payload = base64.urlsafe_b64encode(json.dumps({'preferred_username': 'display-user'}).encode()).decode().rstrip('=')
        token = 'header.' + payload + '.signature'
        state = {'mode': 'success', 'forms': []}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def send_json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.path == '/api/v1/namespaces':
                    if state['mode'] == 'forbidden': self.send_json(403, {}); return
                    self.send_json(200, {'items': [{'metadata': {'name': name}} for name in ['team', 'default', 'alpha']]})
                    return
                if state['mode'] == 'discovery-error': self.send_json(503, {'error': 'unavailable'}); return
                if state['mode'] == 'missing-issuer': self.send_json(200, {'token_endpoint': issuer + '/token'}); return
                if state['mode'] == 'issuer-mismatch': self.send_json(200, {'issuer': issuer + '/other', 'token_endpoint': issuer + '/token'}); return
                if state['mode'] == 'unsafe-endpoint': self.send_json(200, {'issuer': issuer, 'token_endpoint': 'http://localhost/token'}); return
                self.send_json(200, {'issuer': issuer, 'token_endpoint': issuer + '/token'})
            def do_POST(self):
                state['forms'].append(parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()))
                if state['mode'] == 'denied': self.send_json(400, {'error': 'access_denied', 'error_description': 'fixture denied'}); return
                if state['mode'] == 'token-error': self.send_json(401, {'error': 'invalid_grant', 'error_description': 'fixture invalid'}); return
                if state['mode'] == 'no-token': self.send_json(200, {}); return
                if state['mode'] == 'malformed-token': self.send_json(200, {'id_token': 'invalid'}); return
                self.send_json(200, {'id_token': token})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        issuer = f'https://localhost:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # The reference uses Python only as its JSON parser; this is a test shim.
        parser_shim = self.root / 'json-parser.py'
        parser_shim.write_text('import sys\nsys.stdout.reconfigure(newline="\\n")\ncode = sys.argv[2]\nsys.argv = ["-c", *sys.argv[3:]]\nexec(compile(code, "<string>", "exec"))\n', encoding='utf-8')
        shim = self.root / 'python3'
        shim.write_text('#!/usr/bin/env bash\nexec "' + Path(sys.executable).as_posix() + '" "' + parser_shim.as_posix() + '" "$@"\n', encoding='utf-8', newline='\n')
        env = dict(self.env, QUABER_OIDC_ISSUER_URL=issuer, QUABER_API_CA_FILE=str(self.root / 'missing-api.crt'), QUABER_OIDC_CA_FILE=str(self.root / 'missing-dex.crt'), PATH=str(self.root) + os.pathsep + self.env['PATH'])
        argv = ['login', 'https://api.test', '--username', 'fixture user', '--password', 's&cret + тест', '--onlytoken']
        for mode in ('success', 'denied', 'discovery-error', 'missing-issuer', 'issuer-mismatch', 'unsafe-endpoint', 'token-error', 'no-token', 'malformed-token'):
            state['mode'] = mode
            with self.subTest(mode=mode):
                self.assertEqual(self.invoke(argv, env), self.reference_run(argv, env))
        self.assertEqual(state['forms'][0], state['forms'][1])
        self.assertEqual(state['forms'][0]['password'], ['s&cret + тест'])
        self.assertFalse((self.root / 'config').exists())
        state['mode'] = 'success'
        verified = dict(env, QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY='false', QUABER_OIDC_CA_FILE=str(cert_file))
        self.assertEqual(self.invoke(argv, verified), self.reference_run(argv, verified))
        # Git Bash's colon-separated KUBECONFIG must be converted for the
        # Windows native client. The original script itself stays untouched.
        native = Path('C:/Projects/kubectl.exe')
        if not native.exists():
            return
        (self.root / 'kubectl').write_text(
            '#!/usr/bin/env bash\nexport KUBECONFIG="$(cygpath -w -p "$KUBECONFIG")"\nexec "'
            + native.as_posix() + '" "$@"\n', encoding='utf-8', newline='\n')
        env.update(PATH=str(self.root) + os.pathsep + str(native.parent) + os.pathsep + self.env['PATH'])
        full_args = ['login', issuer, '--username', 'fixture user', '--password', 'fixture-password']
        import yaml
        destination = self.root / 'config'
        for mode in ('success', 'forbidden'):
            state['mode'] = mode
            destination.unlink(missing_ok=True)
            actual = self.invoke(full_args, env)
            self.assertEqual(actual[0], 0, actual)
            windows_config = yaml.safe_load(destination.read_text(encoding='utf-8'))
            destination.unlink()
            reference = self.reference_run(full_args, env)
            self.assertEqual(actual, reference)
            self.assertEqual(windows_config, yaml.safe_load(destination.read_text(encoding='utf-8')))
        # Re-login retains an accessible selected namespace and unrelated context.
        state['mode'] = 'success'
        saved = yaml.safe_load(destination.read_text(encoding='utf-8'))
        selected = next(item for item in saved['contexts'] if item['name'] == saved['current-context'])
        selected['context']['namespace'] = 'team'
        saved['contexts'].append({'name': 'unrelated', 'context': dict(selected['context'])})
        original = yaml.safe_dump(saved)
        destination.write_text(original, encoding='utf-8')
        actual = self.invoke(full_args, env)
        windows_config = yaml.safe_load(destination.read_text(encoding='utf-8'))
        destination.write_text(original, encoding='utf-8')
        reference = self.reference_run(full_args, env)
        self.assertEqual(actual, reference)
        self.assertIn('Using namespace "team".', actual[1])
        self.assertEqual(windows_config, yaml.safe_load(destination.read_text(encoding='utf-8')))

if __name__ == '__main__': unittest.main()
