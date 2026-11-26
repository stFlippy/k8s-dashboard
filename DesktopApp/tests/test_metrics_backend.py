import time
import unittest
from unittest.mock import Mock
from quaber.cache import SnapshotCache
from quaber.metric_history import MetricHistory
from quaber.metrics_backend import HistoryReader, history_query, parse_history, NoRedirect


class BackendHistoryTests(unittest.TestCase):
    def test_missing_backend_is_not_rediscovered_for_each_pod(self):
        clock = [0.]
        api = Mock()
        api.cache = SnapshotCache(clock=lambda: clock[0])
        api.cluster.return_value = {'id': 'c', 'enabled': True}
        api.resource.return_value = {'metadata': {'uid': 'uid'}}
        api.request.side_effect = PermissionError('monitoring unavailable')
        reader = HistoryReader(api)
        with self.assertRaises(Exception):
            reader.load('c', 'ns', 'one', expected_uid='uid')
        self.assertEqual(api.request.call_count, 4)
        clock[0] = 6.
        with self.assertRaises(Exception):
            reader.load('c', 'ns', 'two', expected_uid='uid')
        self.assertEqual(api.request.call_count, 4)

    def test_query_scopes_namespace_and_pod_and_parser_preserves_gaps(self):
        query = history_query('ns', 'pod')
        self.assertIn('namespace="ns",pod="pod"', query)
        document = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'container': 'app', 'quaber_metric': 'cpu'}, 'values': [[10, '0.25'], [20, 'NaN'], [999, '1']]},
            {'metric': {'container': 'app', 'quaber_metric': 'memory'}, 'values': [[10, '1048576']]},
        ]}}
        rows = parse_history(document, 0, 30)['app']
        self.assertEqual(rows, [(10., .25, 1048576., None), (20., None, None, None)])
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))

    def test_live_points_survive_backfill_and_retention_is_bounded(self):
        store = MetricHistory()
        identity = ('c', 'ns', 'pod', 'uid')
        store.append(identity, [{'name': 'app', 'cpu': {'usageCores': 3}}], now=2000)
        store.merge(identity, {'app': [(1, 100, 0, 0), (1900, 1, 10, None), (2000, 2, 10, None)]}, now=2000)
        self.assertEqual(store.window(identity, 'app', 1800, now=2000),
                         [(1900, 1, 10, None), (2000, 3, None, None)])

    def test_backend_fallback_query_range_is_cached_and_uid_is_checked(self):
        api = Mock()
        api.cache = SnapshotCache()
        api.cluster.return_value = {'id': 'c', 'enabled': True}
        api.resource.return_value = {'metadata': {'uid': 'uid'}}
        stamp = time.time() - 30
        document = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'container': 'app', 'quaber_metric': 'cpu'}, 'values': [[stamp, '.5']]}]}}
        def request(cid, path, **kwargs):
            if 'routes/' in path or 'services/thanos-querier' in path:
                raise PermissionError('not OpenShift')
            if 'query_range?' in path:
                self.assertIn('step=10', path)
                return document
            return {'metadata': {'name': 'prometheus'}}
        api.request.side_effect = request
        reader = HistoryReader(api)
        result = reader.load('c', 'ns', 'pod', expected_uid='uid')
        calls = api.request.call_count
        self.assertEqual(result['series']['app'][0][1], .5)
        self.assertEqual(reader.load('c', 'ns', 'pod', expected_uid='uid'), result)
        self.assertEqual(api.request.call_count, calls)
        api.resource.return_value = {'metadata': {'uid': 'replacement'}}
        with self.assertRaises(Exception) as raised:
            reader.load('c', 'ns', 'pod', expected_uid='old-uid')
        self.assertIn('Ресурс изменился', str(raised.exception))
