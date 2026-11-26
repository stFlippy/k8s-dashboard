"""Direct HTTPS using the same sanitized kubectl-issued kubeconfig as proxy."""
import base64
import os
import ssl
import tempfile
import threading
import urllib.request
from collections import OrderedDict
from pathlib import Path
import yaml
from .storage import sanitize_kubeconfig
from .app_logging import register_secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a cluster credential to an OAuth page or another server.
        return None


class HTTPSConnection:
    def __init__(self, store, cluster):
        safe = sanitize_kubeconfig(store.decrypt(cluster['encryptedKubeconfig']), cluster['server'])
        config = yaml.safe_load(safe)
        trust = config['clusters'][0]['cluster']
        user = config['users'][0]['user']
        self.server = trust['server']
        self.headers = {}
        context = ssl.create_default_context()
        if trust.get('certificate-authority-data'):
            ca = base64.b64decode(trust['certificate-authority-data'], validate=True)
            context.load_verify_locations(cadata=ca.decode('ascii') if b'-----BEGIN CERTIFICATE-----' in ca else ca)
        if trust.get('insecure-skip-tls-verify') is True:
            # Honor only the existing explicit kubeconfig policy, never a retry.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if user.get('token'):
            register_secret(user['token'])
            self.headers['Authorization'] = 'Bearer ' + user['token']
        else:
            # OpenSSL loads the chain/key into memory; files are removed before I/O.
            with tempfile.TemporaryDirectory(prefix='quaber-https-') as directory:
                cert, key = Path(directory)/'client.pem', Path(directory)/'client.key'
                cert.write_bytes(base64.b64decode(user['client-certificate-data'], validate=True))
                key.write_bytes(base64.b64decode(user['client-key-data'], validate=True))
                if os.name != 'nt':
                    cert.chmod(0o600)
                    key.chmod(0o600)
                context.load_cert_chain(str(cert), str(key))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                 urllib.request.HTTPSHandler(context=context))


class HTTPSTransport:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.connections = OrderedDict()

    def connection(self, cluster):
        identity = (cluster['id'], cluster['server'], cluster['encryptedKubeconfig'])
        with self.lock:
            existing = self.connections.get(identity)
            if existing is not None:
                self.connections.move_to_end(identity)
                return existing
            connection = HTTPSConnection(self.store, cluster)
            # Drop old secrets for a renewed connection; in-flight users retain theirs.
            for key in list(self.connections):
                if key[0] == cluster['id']: del self.connections[key]
            self.connections[identity] = connection
            while len(self.connections) > 64: self.connections.popitem(last=False)
            return connection

    def close(self):
        with self.lock: self.connections.clear()
