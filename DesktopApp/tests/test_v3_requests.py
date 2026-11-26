import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from quaber.cache import SnapshotCache
from quaber.fetch_plan import FetchPlan, collect_cluster
from quaber.kubernetes import Kubernetes
from quaber.request_policy import RequestPolicy
from v3_helpers import FakeClock, FakeHTTP


def fake_api(namespaces=4, forbidden=None, pod_count=0, cluster_count=4):
    clock = FakeClock()
    clusters = [{'id': f'c{i}', 'name': f'C{i}', 'enabled': True} for i in range(cluster_count)]
    api = Kubernetes.__new__(Kubernetes)
    api.stopped = False
    api.cluster = lambda cid: next(c for c in clusters if c['id'] == cid)
    api.proxy = SimpleNamespace(url=lambda cluster: 'http://'+cluster['id']+'.invalid')
    api.cache = SnapshotCache(clock=clock)
    api.request_policy = RequestPolicy(clock=clock, wait=clock.advance)
    def route(req):
        path = urlsplit(req.full_url).path
        if path == '/api/v1/namespaces':
            return 200, {}, {'items': [{'metadata': {'name': f'ns{i}'}} for i in range(namespaces)]}
        if forbidden and path == forbidden:
            return 403, {}, {'message': 'synthetic forbidden'}
        if path.endswith('/pods'):
            cid = urlsplit(req.full_url).hostname.split('.')[0]
            items = [{'metadata': {'name': f'pod{i}', 'uid': f'u{i}', 'resourceVersion': '1',
                       'namespace': f'ns{(i//cluster_count)%namespaces}'}, 'status': {'phase': 'Running'}}
                     for i in range(pod_count) if f'c{i%cluster_count}' == cid]
            if '/namespaces/' in path:
                ns = path.split('/namespaces/')[1].split('/')[0]
                items = [r for r in items if r['metadata']['namespace'] == ns]
            query = parse_qs(urlsplit(req.full_url).query)
            start = int(query.get('continue', ['0'])[0])
            end = start+int(query.get('limit', ['200'])[0])
            return 200, {}, {'items': items[start:end], 'metadata': {'continue': str(end) if end < len(items) else ''}}
        return 200, {}, {'items': []}
    api.opener = FakeHTTP(route, clock)
    return api, clock, clusters


class RequestTests(unittest.TestCase):
    def test_fifty_display_metrics_share_one_usage_read_and_keep_uid_order_without_cadvisor(self):
        api, clock, clusters = fake_api()
        pods = [{'metadata': {'name': f'p{i}', 'namespace': 'ns', 'uid': f'u{i}'},
                 'spec': {'nodeName': 'n', 'containers': [{'name': 'app'}]}} for i in range(50)]
        api.opener.route = lambda req: (200, {}, {'items': [
            {'metadata': {'name': f'p{i}'}, 'containers': [{'name': 'app', 'usage': {'cpu': f'{i+1}m', 'memory': '1Mi'}}]}
            for i in range(50)]})
        with ThreadPoolExecutor(max_workers=1) as pool:
            values = pool.submit(api.metrics_many, 'c0', 'ns', pods).result()
        self.assertEqual(list(values), [(f'p{i}', f'u{i}') for i in range(50)])
        self.assertTrue(all(v['available'] and not v['throttlingRequested'] for v in values.values()))
        self.assertEqual(values[('p49', 'u49')]['containers'][0]['cpu']['usageCores'], .05)
        self.assertEqual(len(api.opener.calls), 1)
        self.assertIn('/apis/metrics.k8s.io/', api.opener.calls[0]['path'])

    def test_graph_replacement_uid_stops_before_usage_or_node_dispatch(self):
        api, clock, clusters = fake_api()
        api.opener.route = lambda req: (200, {}, {'metadata': {'name': 'p', 'namespace': 'ns', 'uid': 'replacement'},
                                                     'spec': {'nodeName': 'n'}})
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError):
                pool.submit(api.metrics, 'c0', 'ns', 'p', expected_uid='opened-uid').result()
        self.assertEqual(len(api.opener.calls), 1)
        self.assertNotIn('/metrics.k8s.io/', api.opener.calls[0]['path'])

    def test_manual_priority_preserves_write_reserve_and_cluster_fairness(self):
        policy = RequestPolicy()
        for cid in ('a', 'b', 'c'): policy._cluster(cid)
        auto = (object(), 'a', True, False, False)
        manual = (object(), 'b', True, False, True)
        write = (object(), 'c', False, False, False)
        self.assertIs(policy._next_ticket([auto, manual]), manual[0])
        self.assertIs(policy._next_ticket([auto, manual, write]), write[0])

    def test_old_revision_cannot_use_new_cache_or_fall_through_to_node_metrics(self):
        from quaber.monitoring import MonitoringCancelled
        from quaber.request_policy import credentials_revision, expected_credentials
        api, clock, clusters = fake_api()
        old = credentials_revision(clusters[0])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.snapshot_list, 'c0', '/api/v1/pods').result()
        clusters[0]['username'] = 'other'
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.snapshot_list, 'c0', '/api/v1/pods').result()
        count = len(api.opener.calls)
        with expected_credentials({'c0': old}):
            with self.assertRaises(MonitoringCancelled): api.snapshot_list('c0', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), count)
        current = credentials_revision(clusters[0])
        def route(req):
            clusters[0]['username'] = 'changed-during-response'
            return 200, {}, {'items': []}
        api.opener.route = route
        pod = {'metadata': {'name': 'p', 'namespace': 'ns'}, 'spec': {'nodeName': 'n', 'containers': []}}
        def work():
            with expected_credentials({'c0': current}):
                return api.metrics('c0', 'ns', 'p', include_throttling=False, pod_snapshot=pod)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(MonitoringCancelled): pool.submit(work).result()
        self.assertEqual(len(api.opener.calls), count+1)
        self.assertFalse(any('/nodes/' in c['path'] for c in api.opener.calls))
    def test_round_robin_does_not_starve_third_ready_cluster(self):
        policy = RequestPolicy()
        for cid in ('a', 'b', 'c'): policy._cluster(cid)
        candidates = [(object(), cid, True, False) for cid in ('a', 'a', 'b', 'b', 'c')]
        chosen = []
        while candidates:
            ticket = policy._next_ticket(candidates)
            entry = next(e for e in candidates if e[0] is ticket)
            candidates.remove(entry)
            policy.last_cluster = entry[1]
            chosen.append(entry[1])
        self.assertEqual(chosen, ['a', 'b', 'c', 'a', 'b'])

    def test_long_cooldown_skips_monitor_without_occupying_worker_deadline_or_dispatch(self):
        from quaber.request_policy import RequestDeferred
        api, clock, clusters = fake_api()
        api.request_policy.failed('c0', 429, '600')
        before = clock()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RequestDeferred): pool.submit(api.request, 'c0', '/api/v1/pods').result()
        self.assertEqual(clock(), before)
        self.assertEqual(api.opener.calls, [])
        self.assertEqual(api.request_policy.inflight, 0)
        self.assertFalse(api.request_policy.queue)

    def test_reserved_write_slot_is_not_blocked_by_queued_monitor(self):
        from contextlib import nullcontext
        from quaber.write_guard import WriteGuard
        from quaber.request_policy import credentials_revision, RequestLimits
        api, clock, clusters = fake_api(cluster_count=5)
        api.request_policy = RequestPolicy(limits=RequestLimits(inflight=4, monitoring_inflight=3))
        api.store = SimpleNamespace(operation=lambda *a, **k: nullcontext())
        ready, release = threading.Event(), threading.Event()
        def route(req):
            if req.get_method() == 'GET':
                if sum(c['method'] == 'GET' for c in api.opener.calls) == 3:
                    ready.set()
                release.wait(3)
            return 200, {}, {}
        api.opener.route = route
        guard = WriteGuard(lambda: True, lambda cid: credentials_revision(api.cluster(cid)))
        body = {'metadata': {'name': 'cm', 'namespace': 'ns', 'uid': 'u', 'resourceVersion': '1'}}
        permit = guard.issue(('c4', 'configmap', 'ns', 'cm', 'u'), credentials_revision(clusters[4]),
                             'yaml', authorized=True, parameters=body)
        with ThreadPoolExecutor(max_workers=5) as pool:
            reads = [pool.submit(api.request, cid, '/api/v1/pods') for cid in ('c0', 'c1', 'c2')]
            try:
                self.assertTrue(ready.wait(1))
                waiting = pool.submit(api.request, 'c3', '/api/v1/pods')
                until = time.monotonic()+1
                while not api.request_policy.queue and time.monotonic() < until:
                    time.sleep(.001)
                self.assertTrue(api.request_policy.queue)
                write = pool.submit(api.request, 'c4', api.path('configmap', 'ns', 'cm'), 'PUT', body, permit=permit)
                write.result(timeout=1)
                self.assertFalse(waiting.done())
                self.assertEqual(permit.state, 'accepted')
                self.assertEqual(api.opener.peak, 4)
            finally:
                release.set()
            for future in reads+[waiting]: future.result(timeout=2)
        self.assertEqual(api.request_policy.inflight, 0)

    def test_full_log_does_not_block_unrelated_monitor_and_only_one_download(self):
        api, clock, clusters = fake_api()
        api.request_policy = RequestPolicy()
        entered, release = threading.Event(), threading.Event()
        def route(req):
            if '/log?' in req.selector:
                entered.set()
                release.wait(3)
            return 200, {}, {'items': []}
        api.opener.route = route
        path = '/api/v1/namespaces/ns/pods/p/log?timestamps=true'
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(api.request, 'c0', path)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(api.request, 'c0', path)
                until = time.monotonic()+1
                while not api.request_policy.queue and time.monotonic() < until:
                    time.sleep(.001)
                pool.submit(api.request, 'c2', '/api/v1/pods').result(timeout=1)
                self.assertEqual(sum('/log?' in c['path'] for c in api.opener.calls), 1)
                self.assertFalse(second.done())
                self.assertEqual(api.request_policy.downloads, 1)
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(api.request_policy.downloads, 0)

    def test_metric_snapshot_credentials_revision(self):
        from quaber.metrics import ThrottleSamples
        api, clock, clusters = fake_api()
        api.throttle_samples = ThrottleSamples()
        api.opener.route = lambda req: (200, {}, {'items': [{'metadata': {'name': 'p'},
            'containers': [{'name': 'c', 'usage': {'cpu': '1m', 'memory': '1Mi'}}]}]})
        pod = {'metadata': {'name': 'p', 'namespace': 'ns'}, 'spec': {'containers': [{'name': 'c'}]}}
        with ThreadPoolExecutor(max_workers=1) as pool:
            for _ in range(2):
                pool.submit(api.metrics, 'c0', 'ns', 'p', include_throttling=False, pod_snapshot=pod).result()
            self.assertEqual(len(api.opener.calls), 1)
            clusters[0]['username'] = 'changed-synthetic-account'
            pool.submit(api.metrics, 'c0', 'ns', 'p', include_throttling=False, pod_snapshot=pod).result()
        self.assertEqual(len(api.opener.calls), 2)

    def test_actual_monitor_dispatch_concurrency_limits(self):
        api, clock, clusters = fake_api()
        api.request_policy = RequestPolicy(clock=clock)
        ready, release = threading.Event(), threading.Event()
        counts, peaks = {}, {}
        lock = threading.Lock()
        def route(req):
            cid = urlsplit(req.full_url).hostname
            with lock:
                counts[cid] = counts.get(cid, 0)+1
                peaks[cid] = max(peaks.get(cid, 0), counts[cid])
                if sum(counts.values()) == 2:
                    ready.set()
            release.wait(2)
            with lock:
                counts[cid] -= 1
            return 200, {}, {'items': []}
        api.opener.route = route
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(api.request, cid, '/api/v1/pods') for cid in ('c0', 'c0', 'c1', 'c1')]
            try:
                self.assertTrue(ready.wait(1))
                self.assertEqual(api.opener.peak, 2)
                self.assertTrue(all(peak == 1 for peak in peaks.values()))
            finally:
                release.set()
            for future in futures:
                future.result(timeout=2)
        self.assertEqual(api.request_policy.inflight, 0)
        self.assertEqual(api.request_policy.monitoring, 0)

    def test_actual_cold_steady_and_scoped_counts(self):
        for n in (4, 16):
            api, clock, clusters = fake_api(n)
            plan = FetchPlan.capture(clusters, {}, 0)
            with ThreadPoolExecutor(max_workers=1) as pool:
                for cluster in plan.clusters:
                    value, errors = pool.submit(collect_cluster, api, plan, cluster).result()
                    self.assertEqual(len(value['namespaces']), n)
                    self.assertEqual(errors, [])
                self.assertEqual(len(api.opener.calls), 36)
                before = len(api.opener.calls)
                clock.advance(6)
                for cluster in plan.clusters:
                    pool.submit(collect_cluster, api, plan, cluster).result()
                self.assertEqual(len(api.opener.calls)-before, 32)
            # Namespace filters project a complete per-cluster snapshot locally.
            api, clock, clusters = fake_api(n)
            plan = FetchPlan.capture(clusters, {'selectedClusterIds': ['c1'], 'selectedNamespaces': ['ns2']}, 1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(collect_cluster, api, plan, plan.clusters[0]).result()
            self.assertEqual(len(api.opener.calls), 9)
            self.assertTrue(all('/namespaces/ns2/' not in call['path'] for call in api.opener.calls))

    def test_empty_scope_and_forbidden_capability(self):
        api, clock, clusters = fake_api(4, '/apis/batch/v1/jobs')
        plan = FetchPlan.capture(clusters, {'selectedNamespaces': []}, 0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            for cluster in plan.clusters:
                pool.submit(collect_cluster, api, plan, cluster).result()
        self.assertEqual(api.opener.calls, [])
        plan = FetchPlan.capture(clusters[:1], {}, 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(collect_cluster, api, plan, plan.clusters[0]).result()
            clock.advance(6)
            pool.submit(collect_cluster, api, plan, plan.clusters[0]).result()
        self.assertEqual(sum(call['path'].split('?')[0] == '/apis/batch/v1/jobs' for call in api.opener.calls), 1)

    def test_token_bounds_physical_attempts_and_retry_after(self):
        api, clock, clusters = fake_api()
        with ThreadPoolExecutor(max_workers=1) as pool:
            for _ in range(12):
                pool.submit(api.request, 'c0', '/api/v1/pods').result()
        stamps = [call['start'] for call in api.opener.calls]
        for a in range(len(stamps)):
            for b in range(a, len(stamps)):
                self.assertLessEqual(b-a+1, 2+2*(stamps[b]-stamps[a])+1e-8)
        api.request_policy.failed('c0', 429, '600')
        self.assertGreaterEqual(api.request_policy.clusters['c0']['until'], clock()+600)
        self.assertEqual(api.request_policy.inflight, 0)

    def test_invalidation_while_old_loader_is_running(self):
        cache = SnapshotCache()
        entered, release = threading.Event(), threading.Event()
        def old():
            entered.set()
            release.wait(2)
            return 'old'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cache.get, ('c', 'pod'), old)
            self.assertTrue(entered.wait(1))
            cache.invalidate(lambda key: key[0] == 'c')
            self.assertEqual(cache.get(('c', 'pod'), lambda: 'new'), 'new')
            release.set()
            self.assertEqual(future.result(), 'old')
        self.assertEqual(cache.get(('c', 'pod'), lambda: 'wrong'), 'new')
