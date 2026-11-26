"""Bounded display-metrics fallback; failures are outcomes, not missing inputs."""
from dataclasses import dataclass
import urllib.error
from .authorization import AuthorizationRequired
from .monitoring import MonitoringCancelled
from .metrics import cpu_cores, memory_bytes
from .safety import check_identity


class SourceUnavailable(RuntimeError):
    def __init__(self, original):
        super().__init__(str(original))
        from .kubernetes import ResourceNotFoundError
        cause = original.__cause__
        self.status = cause.code if isinstance(cause, urllib.error.HTTPError) else (
            404 if isinstance(original, ResourceNotFoundError) else 0)
        self.retry_after = 60 if self.status in (403, 404) else 5


@dataclass
class UsageSnapshot:
    index: dict
    error: str = ''
    individual_budget: int = 0


class MetricsReader:
    def __init__(self, api):
        self.api = api

    def _source(self, key, load):
        def guarded():
            try:
                return load()
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                raise SourceUnavailable(exc) from exc
        return self.api.cache.get(key, guarded)

    def usage(self, cluster_id, namespace):
        from .kubernetes import _part
        from .request_policy import credentials_revision
        revision = credentials_revision(self.api.cluster(cluster_id))
        path = f'/apis/metrics.k8s.io/v1beta1/namespaces/{_part(namespace)}/pods'
        try:
            values = self._source((cluster_id, revision, 'metrics-source', path),
                                  lambda: self.api.snapshot_list(cluster_id, path))
            return UsageSnapshot({m.get('metadata', {}).get('name'): m for m in values})
        except SourceUnavailable as exc:
            # Named GET may be allowed while LIST is forbidden, but a display
            # collection must never fan out to an unbounded number of GETs.
            return UsageSnapshot({}, str(exc), 3 if exc.status == 403 else 0)

    def many(self, cluster_id, namespace, pods):
        if not pods:
            return {}
        usage = self.usage(cluster_id, namespace)
        summaries, result = {}, {}
        for pod in pods:
            self.api._check_credentials(cluster_id)
            meta = pod['metadata']
            identity = (meta['name'], meta.get('uid', ''))
            try:
                result[identity] = self.api.metrics(cluster_id, namespace, meta['name'], include_throttling=False,
                    pod_snapshot=pod, expected_uid=meta.get('uid'), _usage_index=usage, _summary_indexes=summaries)
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                result[identity] = {'available': False, 'error': str(exc)}
        return result

    def load(self, cluster_id, namespace, name, *, include_throttling=True, pod_snapshot=None, expected_uid=None,
             _usage_index=None, _summary_indexes=None):
        from .kubernetes import _part
        from .request_policy import credentials_revision
        api = self.api
        cluster = api.cluster(cluster_id)
        api._check_credentials(cluster_id, cluster)
        revision = credentials_revision(cluster)
        pod = pod_snapshot if pod_snapshot is not None and not include_throttling else api.resource(cluster_id, 'pod', namespace, name)
        if expected_uid is not None:
            check_identity(pod, expected_uid)
        if pod.get('metadata', {}).get('name', name) != name or pod.get('metadata', {}).get('namespace', namespace) != namespace:
            raise ValueError('Display metrics snapshot has a different target.')
        node = pod.get('spec', {}).get('nodeName')
        snapshot = self.usage(cluster_id, namespace) if _usage_index is None else _usage_index
        if isinstance(snapshot, dict):
            snapshot = UsageSnapshot(snapshot)
        metric = snapshot.index.get(name)
        failure = snapshot.error or 'Метрики Pod пока отсутствуют.'
        if metric is None and snapshot.individual_budget:
            snapshot.individual_budget -= 1
            try:
                metric = api.request(cluster_id, f'/apis/metrics.k8s.io/v1beta1/namespaces/{_part(namespace)}/pods/{_part(name)}')
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                failure = str(exc)
        usage = {}
        timestamp = window = source = None
        if metric is not None:
            for container in metric.get('containers', []):
                usage[container['name']] = {
                    'cpu': cpu_cores(container.get('usage', {}).get('cpu')),
                    'memory': memory_bytes(container.get('usage', {}).get('memory'))}
            timestamp, window, source = metric.get('timestamp'), metric.get('window'), 'metrics-api'
        if not usage and node:
            try:
                if _summary_indexes is not None and node in _summary_indexes:
                    summary_index = _summary_indexes[node]
                    if isinstance(summary_index, Exception):
                        raise summary_index
                else:
                    summary = self._source((cluster_id, revision, node, 'summary'), lambda: api.request(
                        cluster_id, f'/api/v1/nodes/{_part(node)}/proxy/stats/summary', max_bytes=25_000_000))
                    summary_index = {(item.get('podRef', {}).get('namespace'), item.get('podRef', {}).get('name')): item
                                     for item in summary.get('pods', [])}
                    if _summary_indexes is not None:
                        _summary_indexes[node] = summary_index
                metric_pod = summary_index.get((namespace, name), {})
                if metric_pod.get('podRef', {}).get('uid') not in (None, pod.get('metadata', {}).get('uid')):
                    metric_pod = {}
                for container in metric_pod.get('containers', []):
                    cpu, memory = container.get('cpu', {}), container.get('memory', {})
                    nano = cpu.get('usageNanoCores')
                    usage[container['name']] = {
                        'cpu': nano / 1e9 if isinstance(nano, (int, float)) else None,
                        'memory': memory.get('workingSetBytes', memory.get('usageBytes'))}
                    timestamp = timestamp or cpu.get('time') or memory.get('time')
                if usage:
                    source = 'kubelet'
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                if _summary_indexes is not None:
                    _summary_indexes[node] = exc
                failure = f'{failure}; {exc}'
        throttle, throttling_error = {}, None
        if node and include_throttling:
            try:
                from .metrics import parse_node_counters
                import time
                sample_identity = (cluster_id, revision, pod.get('metadata', {}).get('uid'))
                observed_after = api.throttle_samples.identity_floor((sample_identity, node, namespace, name))
                def load_counters():
                    text = api.request(cluster_id, f'/api/v1/nodes/{_part(node)}/proxy/metrics/cadvisor', text=True, max_bytes=25_000_000)
                    return {'generation': time.monotonic_ns(), 'counters': parse_node_counters(text)}
                counters = self._source((cluster_id, revision, node, 'cadvisor-counters'), load_counters)
                throttle = api.throttle_samples.from_counters(
                    counters['counters'].get(namespace, {}).get(name, {}), sample_identity, node, namespace, name,
                    counters['generation'], not_before=observed_after)
                if not throttle:
                    throttling_error = 'cAdvisor не вернул CPU CFS counters для контейнеров Pod.'
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                throttling_error = str(exc)
        elif include_throttling:
            throttling_error = 'Pod ещё не назначен на ноду.'
        containers = []
        for container in pod.get('spec', {}).get('containers', []):
            container_name = container.get('name', '')
            resources, current = container.get('resources', {}), usage.get(container_name, {})
            containers.append({'name': container_name,
                'cpu': {'usageCores': current.get('cpu'), 'requestCores': cpu_cores(resources.get('requests', {}).get('cpu')),
                        'limitCores': cpu_cores(resources.get('limits', {}).get('cpu'))},
                'memory': {'usageBytes': current.get('memory'), 'requestBytes': memory_bytes(resources.get('requests', {}).get('memory')),
                           'limitBytes': memory_bytes(resources.get('limits', {}).get('memory'))},
                'cpuThrottlingPercent': throttle.get(container_name)})
        api._check_credentials(cluster_id)
        return dict(available=bool(usage), timestamp=timestamp, window=window, source=source,
                    throttlingAvailable=bool(throttle), throttlingError=throttling_error,
                    throttlingRequested=include_throttling, containers=containers,
                    **({} if usage else {'error': failure}))
