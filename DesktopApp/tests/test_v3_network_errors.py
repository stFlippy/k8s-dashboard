import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from urllib.parse import urlsplit, parse_qs
from quaber.fetch_plan import FetchPlan, collect_cluster
from quaber.request_policy import RequestPolicy
from test_v3_requests import fake_api
from v3_helpers import FakeClock


class NetworkErrorsTests(unittest.TestCase):
    def test_retry_after_http_date_long_and_malformed_values(self):
        for header in ('600', formatdate(1600, usegmt=True)):
            clock = FakeClock()
            policy = RequestPolicy(clock=clock, wall=lambda: 1000, jitter=lambda a, b: 1.)
            policy.failed('c', 429, header)
            self.assertEqual(policy.clusters['c']['until'], 600)
        for header in ('invalid-date', 'NaN', 'Infinity', '-20'):
            clock = FakeClock()
            policy = RequestPolicy(clock=clock, jitter=lambda a, b: 1.)
            policy.failed('c', 429, header)
            self.assertEqual(policy.clusters['c']['until'], 5)

    def test_failed_second_page_preserves_all_previous_pods_and_marks_stale(self):
        api, clock, clusters = fake_api(pod_count=217, cluster_count=1)
        plan = FetchPlan.capture(clusters, {}, 0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            old, errors = pool.submit(collect_cluster, api, plan, plan.clusters[0]).result()
            self.assertEqual(errors, [])
            clock.advance(6)
            route = api.opener.route
            def expired(req):
                parsed = urlsplit(req.full_url)
                if parsed.path == '/api/v1/pods' and parse_qs(parsed.query).get('continue'):
                    return 410, {}, {'message': 'synthetic expired continue'}
                return route(req)
            api.opener.route = expired
            current, errors = pool.submit(collect_cluster, api, plan, plan.clusters[0], old['namespaces']).result()
        self.assertTrue(errors)
        self.assertEqual(sum(len(s['pods']) for s in current['namespaces'].values()), 217)
        for ns, scope in current['namespaces'].items():
            self.assertEqual(scope['pods'], old['namespaces'][ns]['pods'])
            self.assertTrue(scope['_stale'])
            self.assertIn('pod', scope['_type_errors'])
        self.assertEqual(api.request_policy.statuses[410], 4)  # bounded relist, then retain the old complete snapshot

    def test_three_failures_allow_one_half_open_probe_while_other_cluster_progresses(self):
        api, clock, clusters = fake_api()
        api.request_policy.jitter = lambda a, b: 1.
        api.opener.route = lambda req: (503, {}, {'message': 'synthetic transient'})
        with ThreadPoolExecutor(max_workers=3) as pool:
            for failures, delay in enumerate((0., 10., 20.), 1):
                before = clock()
                with self.assertRaises(RuntimeError): pool.submit(api.request, 'c0', '/api/v1/pods').result()
                self.assertEqual(api.request_policy.clusters['c0']['failures'], failures)
                self.assertEqual(api.request_policy.clusters['c0']['until'], before+delay)
                clock.advance(delay)
            entered, release = threading.Event(), threading.Event()
            api.request_policy.wait = api.request_policy.condition.wait
            def recovered(req):
                if 'c0.invalid' in req.full_url:
                    entered.set()
                    release.wait(3)
                return 200, {}, {'items': []}
            api.opener.route = recovered
            first = pool.submit(api.request, 'c0', '/api/v1/pods')
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(api.request, 'c0', '/api/v1/pods')
                self.assertFalse(second.done())  # Queued at the single cluster worker, before the HTTP policy.
                pool.submit(api.request, 'c1', '/api/v1/pods').result(timeout=1)
                self.assertEqual(sum(c['cluster'] == 'c0.invalid' for c in api.opener.calls), 4)
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
            api.request_policy.wait=clock.advance
            clock.advance(1)
            pool.submit(api.request,'c0','/api/v1/pods').result(timeout=2)
        self.assertEqual(api.request_policy.clusters['c0']['failures'], 0)
        self.assertEqual(api.request_policy.inflight, 0)
