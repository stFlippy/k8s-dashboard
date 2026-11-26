"""Behavioral regressions for the 2026-11-22 audit; all transports are local fakes."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
import os
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from quaber.authorization import AuthorizationRequired
from quaber.background import Background, ReadJobPlan
from quaber.cache import SnapshotCache
from quaber.collection_reader import CollectionReader
from quaber.endpoint_budget import EndpointBudget, endpoint_key
from quaber.fetch_plan import FetchPlan, collect_cluster
from quaber.kubernetes import Kubernetes, ResponseTooLarge
from quaber.request_policy import credentials_revision
from test_v3_requests import fake_api


class AuditReadTests(unittest.TestCase):
    def test_full_explicit_namespace_selection_shares_aggregate_plan(self):
        for count in (4, 16):
            api, clock, clusters = fake_api(namespaces=count, pod_count=217, cluster_count=4)
            plan = FetchPlan.capture(clusters, {'selectedNamespaces': [f'ns{i}' for i in range(count)]}, 1)
            pods = 0
            for cluster in plan.clusters:
                data, errors = collect_cluster(api, plan, cluster)
                self.assertFalse(errors)
                pods += sum(len(scope['pods']) for scope in data['namespaces'].values())
            self.assertEqual(pods, 217)
            self.assertEqual(len(api.opener.calls), 36)

    def test_empty_scope_does_no_io_and_narrow_filter_projects_one_aggregate_list(self):
        api, _, _ = fake_api(namespaces=100, cluster_count=1)
        reader = CollectionReader(api)
        empty = reader.plan('c0', [])
        self.assertFalse(reader.read('c0', 'pod', empty).groups)
        self.assertEqual(api.opener.calls, [])
        narrow = reader.plan('c0', ['ns0', 'ns1'])
        self.assertTrue(narrow.aggregate)
        reader.read('c0', 'pod', narrow)
        self.assertEqual(len(api.opener.calls), 2)  # discovery + one aggregate LIST

    def test_metrics_degradation_is_bounded_and_negative_capability_is_shared(self):
        for status in (200, 404):
            api, clock, _ = fake_api(cluster_count=1)
            pods = [dict(metadata=dict(name=f'p{i}', uid=f'u{i}', namespace='ns0'),
                         spec=dict(nodeName=f'n{i%2}', containers=[{'name': 'app'}])) for i in range(50)]
            def route(req):
                if '/nodes/' in req.full_url:
                    return 403, {}, {}
                return status, {}, {'items': []}
            api.opener.route = route
            values = api.metrics_many('c0', 'ns0', pods)
            self.assertEqual(len(values), 50)
            self.assertFalse(any(v['available'] for v in values.values()))
            self.assertEqual(len(api.opener.calls), 3)
            clock.advance(10)
            api.metrics_many('c0', 'ns0', pods)
            # A successful empty LIST may refresh; denied node capabilities do not.
            self.assertEqual(len(api.opener.calls), 4 if status == 200 else 3)

    def test_401_stops_dispatch_until_credentials_change_without_stopping_other_clusters(self):
        from types import SimpleNamespace
        api, _, clusters = fake_api(cluster_count=2)
        for cluster in clusters:
            cluster['encryptedKubeconfig'] = 'synthetic'
        api.authorization_required = Mock()
        api.authorization = SimpleNamespace(renew=Mock(side_effect=AuthorizationRequired('sign in')), block=Mock())
        api.opener.route = lambda req: (401 if req.full_url.startswith('http://c0.') else 200, {}, {'items': []})
        with self.assertRaises(AuthorizationRequired):
            api.snapshot_list('c0', '/api/v1/pods')
        first = len(api.opener.calls)
        for _ in range(3):
            with self.assertRaises(AuthorizationRequired):
                api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), first)
        api.snapshot_list('c1', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), first + 1)
        clusters[0]['encryptedKubeconfig'] = 'new revision'
        api.opener.route = lambda req: (200, {}, {'items': []})
        api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), first + 2)

    def test_quota_collection_is_one_list_per_cluster(self):
        api, _, clusters = fake_api(namespaces=16)
        reader = CollectionReader(api)
        for cluster in clusters:
            plan = reader.plan(cluster['id'])
            result = reader.read(cluster['id'], 'quota', plan)
            self.assertEqual(len(result.groups), 16)
        self.assertEqual(len(api.opener.calls), 8)

    def test_access_summary_cache_does_not_cache_write_authorization(self):
        api, clock, _ = fake_api(cluster_count=1)
        def route(req):
            return 200, {}, {'status': {'allowed': False, 'resourceRules': []}}
        api.opener.route = route
        api.access_summary('c0', ['ns0', 'ns1'])
        count = len(api.opener.calls)
        self.assertEqual(count, 3)
        api.access_summary('c0', ['ns1', 'ns0'])
        self.assertEqual(len(api.opener.calls), count)
        api.access_summary('c0', ['ns0', 'ns1'], force=True)
        self.assertEqual(len(api.opener.calls), count * 2)
        api.is_cluster_administrator('c0')
        self.assertEqual(len(api.opener.calls), count * 2 + 1)

    def test_large_page_restarts_at_smaller_limit_and_publishes_complete_list(self):
        api, _, _ = fake_api(cluster_count=1)
        resources = [dict(metadata=dict(name=f'p{i}'), data='x'*25000) for i in range(200)]
        def route(req):
            query = parse_qs(urlsplit(req.full_url).query)
            limit, start = int(query['limit'][0]), int(query.get('continue', ['0'])[0])
            end = start + limit
            return 200, {}, {'items': resources[start:end],
                             'metadata': {'continue': str(end) if end < len(resources) else ''}}
        api.opener.route = route
        result = api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(result), 200)
        self.assertEqual(len(api.opener.calls), 5)
        self.assertEqual(result[-1]['metadata']['name'], 'p199')

    def test_expired_continuation_restarts_without_mixing_snapshots(self):
        api, _, _ = fake_api(cluster_count=1)
        phase = [0]
        def route(req):
            query = parse_qs(urlsplit(req.full_url).query)
            if phase[0] == 0 and 'continue' not in query:
                return 200, {}, {'items': [{'metadata': {'name': 'old'}}], 'metadata': {'continue': 'old-token'}}
            if phase[0] == 0:
                phase[0] = 1
                return 410, {}, {}
            return 200, {}, {'items': [{'metadata': {'name': 'new'}}]}
        api.opener.route = route
        result = api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual([p['metadata']['name'] for p in result], ['new'])
        self.assertEqual(len(api.opener.calls), 3)

    def test_oversized_single_object_has_bounded_retries_and_negative_cache(self):
        api, clock, _ = fake_api(cluster_count=1)
        api.opener.route = lambda req: (200, {}, {'items': [{'data': 'x' * 4_300_000}]})
        with self.assertRaises(ResponseTooLarge):
            api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), 4)
        clock.advance(10)
        with self.assertRaises(ResponseTooLarge):
            api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), 4)

    def test_configmap_write_keeps_unrelated_workload_cache(self):
        api, _, _ = fake_api(cluster_count=1)
        api.snapshot_list('c0', '/apis/batch/v1/cronjobs')
        api.snapshot_list('c0', '/api/v1/namespaces/ns0/configmaps')
        api.invalidate_resource('c0', 'configmap', 'ns0', 'cm')
        self.assertTrue(any('/apis/batch/v1/cronjobs' in key for key in api.cache.entries))
        self.assertFalse(any('/api/v1/namespaces/ns0/configmaps' in key for key in api.cache.entries))

    def test_cache_is_lru_and_encodes_outside_its_lock(self):
        cache = SnapshotCache(capacity=2)
        original = json.dumps
        held = []
        def encode(value, *args, **kwargs):
            acquired = cache.lock.acquire(False)
            held.append(not acquired)
            if acquired:
                cache.lock.release()
            return original(value, *args, **kwargs)
        with patch('quaber.cache.json.dumps', side_effect=encode):
            cache.get('a', lambda: {})
            cache.get('b', lambda: {})
            cache.get('a', lambda: {})
            cache.get('c', lambda: {})
        self.assertFalse(any(held))
        self.assertEqual(list(cache.entries), ['a', 'c'])
        self.assertEqual(cache.total_weight, sum(cache.weights.values()))
        cache.invalidate()
        self.assertEqual(cache.total_weight, 0)


class AuditBudgetTests(unittest.TestCase):
    def test_waiting_heavy_request_does_not_block_ready_light_request(self):
        from quaber.request_policy import RequestPolicy
        policy = RequestPolicy()
        entered, release = threading.Event(), threading.Event()
        def first():
            with policy.slot('a', lambda: None, heavy=True):
                entered.set()
                release.wait(3)
        def read(cid, heavy):
            with policy.slot(cid, lambda: None, heavy=heavy):
                return cid
        with ThreadPoolExecutor(max_workers=3) as pool:
            initial = pool.submit(first)
            self.assertTrue(entered.wait(1))
            blocked = pool.submit(read, 'a', True)
            try:
                deadline = time.monotonic()+1
                while not policy.queue and time.monotonic() < deadline:
                    time.sleep(.005)
                light = pool.submit(read, 'c', False)
                self.assertEqual(light.result(timeout=1), 'c')
                self.assertFalse(blocked.done())
            finally:
                release.set()
            initial.result(timeout=2)
            self.assertEqual(blocked.result(timeout=2), 'a')

    def test_other_process_observes_lease_and_dead_process_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'shared.sqlite'
            budget = EndpointBudget(path)
            key = endpoint_key('https://cluster.test:6443')
            first = budget.acquire(key, heavy=True)
            code = ('import json,sys; from quaber.endpoint_budget import EndpointBudget; '
                    'print(json.dumps(EndpointBudget(sys.argv[1]).acquire(sys.argv[2],heavy=True)))')
            def child():
                result = subprocess.run([sys.executable, '-c', code, str(path), key],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertIsNone(child())
            budget.release(key, first)
            self.assertTrue(child())  # Deliberately exit without releasing its lease.
            deadline = time.monotonic()+2
            recovered = None
            while recovered is None and time.monotonic() < deadline:
                recovered = budget.acquire(key, heavy=True)
                if recovered is None:
                    time.sleep(.05)
            self.assertTrue(recovered)
            budget.release(key, recovered)

    def test_proxy_touch_reuses_one_timer_and_old_expiry_cannot_close_replacement(self):
        from quaber.kubectl import ProxyManager
        manager = ProxyManager(None, Mock())
        proxy = Mock(idle_timer=None)
        manager._proxies['c'] = proxy
        with patch('quaber.kubectl.threading.Timer') as timer:
            for _ in range(100):
                manager._touch('c', proxy)
            timer.assert_called_once()
            replacement = manager._proxies['c'] = Mock()
            with patch.object(manager, 'close') as close:
                manager._expire('c', proxy)
                close.assert_not_called()
            self.assertIs(manager._proxies['c'], replacement)

    def test_independent_budget_instances_share_endpoint_inflight_and_bytes(self):
        from v3_helpers import FakeClock
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            a = EndpointBudget(Path(directory)/'budget.sqlite', clock=clock)
            b = EndpointBudget(Path(directory)/'budget.sqlite', clock=clock)
            key = endpoint_key('https://EXAMPLE.com:6443/')
            self.assertEqual(key, endpoint_key('https://example.com:6443'))
            first = a.acquire(key, heavy=True)
            self.assertTrue(first)
            self.assertIsNone(b.acquire(key, heavy=True))
            second = b.acquire(key)
            self.assertTrue(second)
            self.assertIsNone(a.acquire(key))
            a.release(key, first, 25_000_000)
            b.release(key, second)
            clock.advance(1)
            self.assertIsNone(b.acquire(key))
            write = b.acquire(key, monitor=False)
            self.assertTrue(write)
            b.release(key, write)
            clock.advance(10)
            last = a.acquire(key)
            self.assertTrue(last)
            a.release(key, last)


def worker_test(function):
    @wraps(function)
    def wrapped(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(function, self).result(timeout=15)
    return wrapped


for _name, _function in tuple(vars(AuditReadTests).items()):
    if _name.startswith('test_'):
        setattr(AuditReadTests, _name, worker_test(_function))


class AuditAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_completed_jobs_retire_while_unrelated_worker_remains_active(self):
        backend, release, completed = Background(), threading.Event(), []
        backend.submit(lambda: release.wait(5), monitor=False)
        try:
            for i in range(30):
                backend.submit(lambda: 1, completed.append, monitor=False)
            until = time.monotonic()+3
            while (len(completed) < 30 or backend._retired) and time.monotonic() < until:
                QTest.qWait(10)
            self.assertEqual(len(completed), 30)
            self.assertFalse(backend._retired)
            self.assertEqual(backend.pool.activeThreadCount(), 1)
        finally:
            release.set()
            backend.pool.waitForDone(2000)
            self.app.processEvents()
            backend.stop()
