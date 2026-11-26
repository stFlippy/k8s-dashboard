"""Bounded Prometheus/Thanos history using the existing cluster identity/budget."""
import base64
from datetime import datetime
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml
from .authorization import AuthorizationRequired
from .metrics_reader import SourceUnavailable
from .monitoring import MonitoringCancelled
from .request_policy import credentials_revision
from .safety import check_identity


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a cluster bearer token to an OAuth login or another host.
        return None


def history_query(namespace, pod):
    selector = ('{namespace=' + json.dumps(namespace) + ',pod=' + json.dumps(pod)
                + ',container!="",container!="POD"}')
    expressions = {
        'cpu': 'max by(container)(rate(container_cpu_usage_seconds_total' + selector + '[5m]))',
        'memory': 'max by(container)(container_memory_working_set_bytes' + selector + ')',
        'throttle': ('100 * max by(container)(rate(container_cpu_cfs_throttled_periods_total' + selector
                     + '[5m])) / max by(container)(rate(container_cpu_cfs_periods_total' + selector + '[5m]))'),
    }
    return ' or '.join('label_replace((' + query + '),"quaber_metric","' + metric
                       + '","container",".*")' for metric, query in expressions.items())


def parse_history(document, start, end):
    if document.get('status') != 'success' or document.get('data', {}).get('resultType') != 'matrix':
        raise ValueError('Сервер мониторинга не вернул временные ряды.')
    result = {}
    for series in document['data'].get('result', []):
        labels = series.get('metric', {})
        name, metric = labels.get('container'), labels.get('quaber_metric')
        if not name or metric not in ('cpu', 'memory', 'throttle'):
            continue
        if len(result) >= 128 and name not in result:
            raise ValueError('Слишком много контейнеров в истории метрик.')
        points = result.setdefault(name, {})
        for stamp, raw in series.get('values', [])[:721]:
            stamp, value = float(stamp), float(raw)
            if not math.isfinite(stamp) or not start <= stamp <= end:
                continue
            row = points.setdefault(stamp, [stamp, None, None, None])
            row[{'cpu': 1, 'memory': 2, 'throttle': 3}[metric]] = (
                min(100., value) if metric == 'throttle' else value) if math.isfinite(value) and value >= 0 else None
    return {name: sorted((tuple(row) for row in rows.values()), key=lambda row: row[0])[-720:]
            for name, rows in result.items()}


class HistoryReader:
    def __init__(self, api):
        self.api = api

    def guarded(self, load):
        try:
            return load()
        except (MonitoringCancelled, AuthorizationRequired):
            raise
        except Exception as exc:
            failure = SourceUnavailable(exc)
            failure.retry_after = 60
            raise failure from exc

    def discover(self, cid):
        api = self.api
        try:
            route = api.request(cid, '/apis/route.openshift.io/v1/namespaces/openshift-monitoring/routes/thanos-querier')
            host = route.get('spec', {}).get('host', '')
            if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host):
                raise ValueError('Некорректный адрес Thanos route.')
            return ('route', 'https://' + host, 'Thanos / OpenShift')
        except (MonitoringCancelled, AuthorizationRequired):
            raise
        except Exception:
            pass
        # Only a bounded set of well-known services; never scan pods/namespaces.
        for namespace, service, port, scheme in (
            ('openshift-monitoring', 'thanos-querier', 9091, 'https'),
            ('monitoring', 'prometheus-kube-prometheus-prometheus', 9090, 'http'),
            ('monitoring', 'prometheus-operated', 9090, 'http'),
        ):
            try:
                api.request(cid, f'/api/v1/namespaces/{namespace}/services/{service}')
                return ('proxy', f'/api/v1/namespaces/{namespace}/services/{scheme}:{service}:{port}/proxy', 'Prometheus / Thanos')
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception:
                pass
        raise RuntimeError('Prometheus/Thanos не найден или недоступен. metrics-server не хранит историю.')

    def external(self, cid, address):
        from .cluster_workers import cluster_io
        return cluster_io(self.api, cid, lambda: self._external(cid, address))

    def _external(self, cid, address):
        api = self.api
        cluster = api.cluster(cid)
        api._check_credentials(cid, cluster)
        config = yaml.safe_load(api.store.decrypt(cluster.get('encryptedKubeconfig', '')))
        selected = next(c['context'] for c in config['contexts'] if c['name'] == config['current-context'])
        user = next(u['user'] for u in config['users'] if u['name'] == selected['user'])
        token = user.get('token')
        if not token:
            raise RuntimeError('Thanos route требует bearer token; используется вход с клиентским сертификатом.')
        trust = next(c['cluster'] for c in config['clusters'] if c['name'] == selected['cluster'])
        context = ssl.create_default_context()
        if trust.get('certificate-authority-data'):
            context.load_verify_locations(cadata=base64.b64decode(trust['certificate-authority-data']).decode('ascii'))
        # Honor only an explicitly selected cluster TLS policy, never silently
        # retry an untrusted certificate with verification disabled.
        if trust.get('insecure-skip-tls-verify') is True:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                             urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(address, headers={'Authorization': 'Bearer ' + token})
        try:
            with api._dispatch(cid, request, opener=opener) as response:
                chunks, size, deadline = [], 0, time.monotonic() + 45
                while True:
                    api._check_credentials(cid)
                    if time.monotonic() > deadline:
                        raise TimeoutError('Истёк срок загрузки истории метрик.')
                    chunk = response.read(min(65536, 4_000_001 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    api.request_policy.received(len(chunk))
                    if size > 4_000_000:
                        raise ValueError('История метрик превысила 4 МБ.')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
        except urllib.error.HTTPError as exc:
            # Monitoring RBAC/OAuth failures do not invalidate the Kubernetes
            # session and must never cause a password prompt for a valid token.
            raise PermissionError(f'Сервер мониторинга вернул HTTP {exc.code}; проверьте права просмотра метрик.') from None

    def load(self, cid, namespace, pod, *, expected_uid=None):
        api = self.api
        revision = credentials_revision(api.cluster(cid))
        def collect():
            resource = api.resource(cid, 'pod', namespace, pod)
            if expected_uid is not None:
                check_identity(resource, expected_uid)
            end, start = time.time(), time.time() - 1800
            created = resource.get('metadata', {}).get('creationTimestamp')
            if created:
                start = max(start, datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp())
            source = api.cache.get((cid, revision, 'metrics-history-source'),
                                   lambda: self.guarded(lambda: self.discover(cid)), ttl=300)
            params = urllib.parse.urlencode({'query': history_query(namespace, pod),
                                             'start': start, 'end': end, 'step': 10, 'timeout': '20s'})
            path = source[1] + '/api/v1/query_range?' + params
            document = self.external(cid, path) if source[0] == 'route' else api.request(cid, path, max_bytes=4_000_000)
            api._check_credentials(cid)
            if expected_uid is not None:
                check_identity(api.resource(cid, 'pod', namespace, pod), expected_uid)
            return {'source': source[2], 'series': parse_history(document, start, end)}
        return api.cache.get((cid, revision, 'metrics-history', namespace, pod, expected_uid),
                             lambda: self.guarded(collect), ttl=60)
