"""Local TLS Kubernetes-shaped endpoint; no real cluster credentials or writes."""
import base64
import copy
import ipaddress
import json
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
import yaml


def matrix_data():
    lists = {key: [] for key in ('namespaces','deployments','pods','replicasets','statefulsets',
                                'daemonsets','replicationcontrollers','jobs','cronjobs')}
    for namespace_number in range(1, 11):
        ns = f'ns-{namespace_number:02d}'
        lists['namespaces'].append(dict(apiVersion='v1', kind='Namespace', metadata=dict(name=ns, uid=ns)))
        for deployment_number in range(1, 6):
            name = f'app-{deployment_number:02d}'
            uid, rs = f'{ns}-{name}', f'{ns}-{name}-rs'
            meta = dict(name=name, namespace=ns, uid=uid, resourceVersion='1', creationTimestamp='2026-01-01T00:00:00Z')
            container = dict(name='main', image='bench/app:1.0', resources=dict(requests=dict(cpu='100m',memory='64Mi')))
            lists['deployments'].append(dict(apiVersion='apps/v1', kind='Deployment', metadata=meta,
                spec=dict(replicas=4, selector=dict(matchLabels=dict(app=name)), template=dict(spec=dict(containers=[container]))),
                status=dict(replicas=4, readyReplicas=4, availableReplicas=4, updatedReplicas=4,
                            conditions=[dict(type='Available', status='True', reason='MinimumReplicasAvailable')])) )
            lists['replicasets'].append(dict(apiVersion='apps/v1', kind='ReplicaSet',
                metadata=dict(name=name+'-rs', namespace=ns, uid=rs, resourceVersion='1',
                              ownerReferences=[dict(kind='Deployment', name=name, uid=uid, controller=True)]),
                spec=dict(replicas=4), status=dict(replicas=4, readyReplicas=4)))
            for pod_number in range(4):
                pod_name = f'{name}-rs-{pod_number}'
                lists['pods'].append(dict(apiVersion='v1', kind='Pod',
                    metadata=dict(name=pod_name, namespace=ns, uid=f'{rs}-pod-{pod_number}', resourceVersion='1',
                                  labels=dict(app=name), creationTimestamp='2026-01-01T00:00:00Z',
                                  ownerReferences=[dict(kind='ReplicaSet', name=name+'-rs', uid=rs, controller=True)]),
                    spec=dict(nodeName='bench-node', containers=[copy.deepcopy(container)]),
                    status=dict(phase='Running', containerStatuses=[dict(name='main', ready=True, restartCount=0,
                        containerID=f'containerd://{rs}-{pod_number}', state=dict(running=dict(startedAt='2026-01-01T00:00:00Z')))])))
    return lists


class TLSEndpoint:
    token = 'synthetic-benchmark-token'
    def __init__(self, directory, delay=0., *, require_client_cert=False):
        directory = Path(directory)
        now = datetime.now(timezone.utc)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Quaber local benchmark CA')])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
              .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None), True)
              .sign(ca_key, hashes.SHA256()))
        def issue(name, purpose, san=None):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)]))
                    .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
                    .add_extension(x509.ExtendedKeyUsage([purpose]), False))
            if san: cert = cert.add_extension(x509.SubjectAlternativeName(san), False)
            cert = cert.sign(ca_key, hashes.SHA256())
            return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        server_cert, server_key = issue('localhost', ExtendedKeyUsageOID.SERVER_AUTH,
            [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))])
        self.client_cert, self.client_key = issue('benchmark client', ExtendedKeyUsageOID.CLIENT_AUTH)
        self.ca = ca.public_bytes(serialization.Encoding.PEM)
        cert, key = directory/'server.pem', directory/'server.key'
        cert.write_bytes(server_cert)
        key.write_bytes(server_key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        if require_client_cert:
            context.load_verify_locations(cadata=self.ca.decode())
            context.verify_mode = ssl.CERT_REQUIRED
        self.data = matrix_data()
        self.calls, self.lock = [], threading.Lock()
        self.active = self.peak = 0
        self.delay, self.status, self.redirect = delay, 200, None
        endpoint = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def do_GET(self):
                with endpoint.lock:
                    endpoint.active += 1
                    endpoint.peak = max(endpoint.peak, endpoint.active)
                    endpoint.calls.append(dict(path=urlsplit(self.path).path, method='GET', time=time.perf_counter()))
                try:
                    time.sleep(endpoint.delay)
                    status = endpoint.status
                    if not require_client_cert and self.headers.get('Authorization') != 'Bearer '+endpoint.token:
                        status = 401
                    if endpoint.redirect:
                        self.send_response(302)
                        self.send_header('Location', endpoint.redirect)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    if status == 200:
                        path = urlsplit(self.path).path
                        collection = path.rstrip('/').split('/')[-1]
                        resources = endpoint.data.get(collection, [])
                        if '/namespaces/' in path:
                            ns = path.split('/namespaces/', 1)[1].split('/', 1)[0]
                            resources = [r for r in resources if r.get('metadata', {}).get('namespace') == ns]
                        query = parse_qs(urlsplit(self.path).query)
                        start, limit = int(query.get('continue', ['0'])[0]), int(query.get('limit', ['200'])[0])
                        body = dict(apiVersion='v1', kind='List', metadata=dict(resourceVersion='1',
                            **({'continue':str(start+limit)} if start+limit < len(resources) else {})), items=resources[start:start+limit])
                    else:
                        body = dict(kind='Status', status='Failure', reason='Forbidden', message='synthetic rejection')
                    raw = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                finally:
                    with endpoint.lock: endpoint.active -= 1
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server_url = f'https://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def config(self, *, client_cert=False, trust=True):
        b64 = lambda value: base64.b64encode(value).decode()
        cluster = dict(server=self.server_url)
        if trust: cluster['certificate-authority-data'] = b64(self.ca)
        user = dict(token=self.token) if not client_cert else {
            'client-certificate-data': b64(self.client_cert), 'client-key-data': b64(self.client_key)}
        return yaml.safe_dump(dict(apiVersion='v1', kind='Config', **{'current-context':'fixture'},
            clusters=[dict(name='fixture',cluster=cluster)],users=[dict(name='fixture',user=user)],
            contexts=[dict(name='fixture',context=dict(cluster='fixture',user='fixture'))]))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
