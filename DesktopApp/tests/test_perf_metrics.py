import unittest
from quaber.metrics import parse_node_counters, ThrottleSamples


class NodeMetricsTests(unittest.TestCase):
    def test_new_uid_does_not_baseline_against_an_older_cached_node_generation(self):
        samples = ThrottleSamples()
        counters = {'app': {'periods_total': 100, 'throttled_periods_total': 10}}
        key = ('c', 'revision', 'new-uid')
        self.assertEqual(samples.from_counters(counters, key, 'n', 'ns', 'pod', 10, not_before=11), {'app': None})
        self.assertEqual(samples.samples, {})
        self.assertEqual(samples.from_counters(counters, key, 'n', 'ns', 'pod', 12, not_before=11), {'app': None})
        counters['app'].update(periods_total=200, throttled_periods_total=30)
        self.assertEqual(samples.from_counters(counters, key, 'n', 'ns', 'pod', 13, not_before=11), {'app': 20})

    def test_real_metrics_singleflight_and_usage_only(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import Mock, patch
        from quaber.kubernetes import Kubernetes
        from quaber.cache import SnapshotCache
        import quaber.metrics as metrics
        api = Kubernetes.__new__(Kubernetes)
        api.cluster = lambda cid: {'id': cid, 'enabled': True}
        api.stopped = False
        api.cache, api.throttle_samples = SnapshotCache(), ThrottleSamples()
        api.resource = Mock(return_value={'spec': {'nodeName': 'n', 'containers': [{'name': 'app'}]}})
        api.list_pages = Mock(return_value=[{'metadata': {'name': f'p{i}'}, 'containers': [{'name': 'app', 'usage': {'cpu': '1m', 'memory': '1Mi'}}]} for i in range(10)])
        text = '\n'.join(f'container_cpu_cfs_{counter}{{namespace="ns",pod="p{i}",container="app"}} 100'
                         for i in range(10) for counter in ('periods_total', 'throttled_periods_total'))
        api.request = Mock(return_value=text)
        with patch('quaber.metrics.parse_node_counters', wraps=metrics.parse_node_counters) as parse:
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda i: api.metrics('c', 'ns', f'p{i}'), range(10)))
            self.assertEqual(parse.call_count, 1)
            self.assertTrue(all(v['throttlingAvailable'] for v in values))
            self.assertEqual(api.request.call_count, 1)
            api.cache.invalidate()
            api.metrics('c', 'ns', 'p0', include_throttling=False)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(api.request.call_count, 1)
            api.metrics('c', 'ns', 'p0')
            self.assertEqual(parse.call_count, 2)
    def test_generations_nodes_reset_and_repeat(self):
        samples = ThrottleSamples()
        def value(p, t): return {'app': {'periods_total': p, 'throttled_periods_total': t}}
        def get(p, t, gen, node='n'):
            return samples.from_counters(value(p, t), 'c', node, 'ns', 'pod', gen)['app']
        self.assertIsNone(get(100, 10, 1))
        self.assertEqual(get(200, 30, 2), 20)
        self.assertEqual(get(200, 30, 2), 20)
        self.assertEqual(get(100, 10, 1), 20)
        self.assertIsNone(get(200, 30, 3, 'other'))
        self.assertIsNone(get(10, 1, 3))

    def test_node_index_preserves_all_pods_and_labels(self):
        text = '\n'.join(f'container_cpu_cfs_{counter}{{namespace="ns",pod="p{i}",container="app"}} {i+10}'
                         for i in range(10) for counter in ('periods_total', 'throttled_periods_total'))
        index = parse_node_counters(text)
        self.assertEqual(len(index['ns']), 10)
        self.assertEqual(index['ns']['p9']['app']['periods_total'], 19)
