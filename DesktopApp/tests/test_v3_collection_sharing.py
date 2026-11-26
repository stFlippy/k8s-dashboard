import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from quaber.monitoring import MonitoringState, MonitoringCancelled
from test_v3_requests import fake_api


class CollectionSharingTests(unittest.TestCase):
    def test_inventory_forbidden_aggregate_falls_back_once_and_preserves_scoped_objects(self):
        from quaber.inventory import collect_inventory
        api, clock, clusters = fake_api(forbidden='/apis/apps/v1/deployments')
        clusters[0]['server'] = 'https://synthetic.invalid'
        route = api.opener.route
        def resources(req):
            if '/namespaces/' in req.full_url and req.full_url.split('?')[0].endswith('/deployments'):
                ns = req.full_url.split('/namespaces/')[1].split('/')[0]
                return 200, {}, {'items': [{'metadata': {'namespace': ns, 'name': 'app', 'uid': ns}}]}
            return route(req)
        api.opener.route = resources
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(collect_inventory, api, dict(clusters[0])).result()
            clock.advance(6)
            second = pool.submit(collect_inventory, api, dict(clusters[0])).result()
        self.assertEqual([r['namespace'] for r in first['records']], ['ns0', 'ns1', 'ns2', 'ns3'])
        self.assertEqual(second['records'], first['records'])
        self.assertEqual(first['errors'], [])
        self.assertEqual(sum('/apis/apps/v1/deployments?' in c['path'] for c in api.opener.calls), 1)

    def test_inventory_reuses_complete_matrix_collections_and_bound_credentials(self):
        from quaber.fetch_plan import FetchPlan, collect_cluster
        from quaber.inventory import collect_inventory
        from quaber.monitoring import MonitoringCancelled
        api, clock, clusters = fake_api(pod_count=217)
        cluster = dict(clusters[0], server='https://synthetic.invalid')
        # Use the actual stored revision, including the harmless display address.
        clusters[0].update(cluster)
        plan = FetchPlan.capture(clusters[:1], {}, 0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(collect_cluster, api, plan, plan.clusters[0]).result()
            before = len(api.opener.calls)
            result = pool.submit(collect_inventory, api, dict(cluster)).result()
            self.assertEqual(result['namespace_count'], 4)
            self.assertEqual(result['errors'], [])
            self.assertEqual(len(api.opener.calls), before)
            clusters[0]['username'] = 'new-account'
            with self.assertRaises(MonitoringCancelled):
                pool.submit(collect_inventory, api, dict(cluster)).result()
        self.assertEqual(len(api.opener.calls), before)

    def test_manual_read_bypasses_cached_age_without_bypassing_physical_budget(self):
        from quaber.request_policy import manual_read
        api, clock, clusters = fake_api(pod_count=217)
        def manual():
            with manual_read(): return api.pods('c0', 'ns0')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.pods, 'c0', 'ns0').result()
            pool.submit(api.pods, 'c0', 'ns0').result()
            self.assertEqual(len(api.opener.calls), 1)
            pool.submit(manual).result()
        self.assertEqual(len(api.opener.calls), 2)
        self.assertEqual(api.request_policy.by_purpose, {'poll': 1, 'manual': 1})
        self.assertEqual(api.request_policy.stats['bytes'], sum(c['bytes'] for c in api.opener.calls))
        self.assertEqual(api.request_policy.statuses, {200: 2})

    def test_reconnect_replaces_canceled_owner_without_old_cleanup_erasing_new_snapshot(self):
        api, clock, clusters = fake_api(pod_count=217)
        state = MonitoringState()
        api.monitoring = state
        old_token = state.token()
        entered, release = threading.Event(), threading.Event()
        route = api.opener.route
        def blocked(req):
            if len(api.opener.calls) == 1:
                entered.set()
                release.wait(3)
            return route(req)
        api.opener.route = blocked
        def read(token):
            with state.read_scope(token): return api.all_namespace_resources('c0', 'pod')
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(read, old_token)
            try:
                self.assertTrue(entered.wait(1))
                state.sleep('lock')
                self.assertTrue(state.reconnect())
                current_read = pool.submit(read, state.token())
                self.assertFalse(current_read.done())  # One physical thread per cluster.
            finally:
                release.set()
            current = current_read.result(timeout=2)
            with self.assertRaises(MonitoringCancelled): old.result(timeout=1)
            self.assertEqual(pool.submit(read, state.token()).result(timeout=1), current)
        self.assertEqual(len(api.opener.calls), 2)
        self.assertFalse(api.cache.active)
        self.assertFalse(api.cache.active_tokens)
    def test_complete_coverage_projects_namespace_without_copy_cache_or_reverse_assumption(self):
        api, clock, clusters = fake_api(pod_count=217)
        with ThreadPoolExecutor(max_workers=1) as pool:
            complete = pool.submit(api.all_namespace_resources, 'c0', 'pod').result()
            selected = pool.submit(api.pods, 'c0', 'ns0').result()
            self.assertEqual(len(api.opener.calls), 1)
            self.assertEqual(selected, [p for p in complete if p['metadata']['namespace'] == 'ns0'])
            self.assertTrue(all(any(p is q for q in complete) for p in selected))
            self.assertEqual(len(api.cache.entries), 1)
            api.invalidate_resource('c0', 'pod', 'ns0', 'pod0')
            pool.submit(api.pods, 'c0', 'ns0').result()
            self.assertEqual(len(api.opener.calls), 2)
        api, clock, clusters = fake_api(pod_count=217)
        with ThreadPoolExecutor(max_workers=1) as pool:
            scoped = pool.submit(api.pods, 'c0', 'ns0').result()
            complete = pool.submit(api.all_namespace_resources, 'c0', 'pod').result()
        self.assertEqual(len(api.opener.calls), 2)
        self.assertGreater(len(complete), len(scoped))

    def test_inflight_aggregate_and_namespace_consumer_share_one_physical_list(self):
        api, clock, clusters = fake_api(pod_count=217)
        entered, release = threading.Event(), threading.Event()
        route = api.opener.route
        def blocked(req):
            entered.set()
            release.wait(3)
            return route(req)
        api.opener.route = blocked
        with ThreadPoolExecutor(max_workers=2) as pool:
            parent = pool.submit(api.all_namespace_resources, 'c0', 'pod')
            try:
                self.assertTrue(entered.wait(1))
                child = pool.submit(api.pods, 'c0', 'ns0')
                time.sleep(.08)
                self.assertEqual(len(api.opener.calls), 1)
                self.assertFalse(child.done())
            finally:
                release.set()
            complete, selected = parent.result(timeout=2), child.result(timeout=2)
        self.assertEqual(selected, [p for p in complete if p['metadata']['namespace'] == 'ns0'])
        self.assertEqual(len(api.opener.calls), 1)

    def test_waiting_subscriber_cancels_without_namespace_fallback_dispatch(self):
        api, clock, clusters = fake_api(pod_count=217)
        state = MonitoringState()
        api.monitoring = state
        token = state.token()
        entered, release = threading.Event(), threading.Event()
        route = api.opener.route
        def blocked(req):
            entered.set()
            release.wait(3)
            return route(req)
        api.opener.route = blocked
        def read(call):
            with state.read_scope(token): return call()
        with ThreadPoolExecutor(max_workers=2) as pool:
            parent = pool.submit(read, lambda: api.all_namespace_resources('c0', 'pod'))
            try:
                self.assertTrue(entered.wait(1))
                child = pool.submit(read, lambda: api.pods('c0', 'ns0'))
                time.sleep(.08)
                state.sleep('lock')
                with self.assertRaises(MonitoringCancelled): child.result(timeout=1)
            finally:
                release.set()
            with self.assertRaises(MonitoringCancelled): parent.result(timeout=1)
        self.assertEqual(len(api.opener.calls), 1)

    def test_fast_display_uses_fresh_shared_coverage_then_refreshes_after_one_second(self):
        api, clock, clusters = fake_api(pod_count=217)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.all_namespace_resources, 'c0', 'pod').result()
            pool.submit(api.pods, 'c0', 'ns0', fresh=True).result()
            self.assertEqual(len(api.opener.calls), 1)
            clock.advance(1.1)
            pool.submit(api.pods, 'c0', 'ns0', fresh=True).result()
        self.assertEqual(len(api.opener.calls), 2)
        self.assertIn('/namespaces/ns0/pods', api.opener.calls[-1]['path'])
