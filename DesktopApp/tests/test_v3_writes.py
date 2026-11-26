import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from quaber.write_guard import WriteGuard
from quaber.request_policy import credentials_revision
from test_v3_requests import fake_api


class WriteTests(unittest.TestCase):
    def test_exec_physical_dispatch_requires_current_single_use_permit_after_get(self):
        from quaber.container_instance import capture_instance
        for outcome in ('accepted', 'lock', 'uid', 'timeout', 'forbidden'):
            api, clock, guard, _, _ = self.setup_api()
            calls = []
            def execute(*args):
                calls.append(args)
                if outcome == 'timeout': raise TimeoutError('synthetic exec outcome unknown')
                if outcome == 'forbidden': raise PermissionError('Error from server (Forbidden): synthetic exec denial')
                return ('ok', '')
            api.proxy.execute = execute
            pod = dict(metadata={'name':'p','namespace':'ns','uid':'pod-uid'},spec={'containers':[{'name':'app'}]},
                       status={'phase':'Running','containerStatuses':[dict(name='app',containerID='synthetic://app',
                               restartCount=0,state={'running':{}})]})
            instance = capture_instance(pod,'app')
            def route(req):
                if outcome == 'lock': guard.invalidate()
                current = copy.deepcopy(pod)
                if outcome == 'uid': current['metadata']['uid'] = 'other'
                return 200, {}, current
            api.opener.route = route
            permit = guard.issue(('c0', 'pod', 'ns', 'p', 'pod-uid'), credentials_revision(api.cluster('c0')),
                                 'exec', authorized=True, parameters={'container': 'app', 'command': 'echo synthetic',
                                                                     'instance':instance.parameters()})
            def run(): return api.execute_pod('c0', 'ns', 'p', 'pod-uid', 'app', 'echo synthetic',
                                               expected_instance=instance,permit=permit)
            with ThreadPoolExecutor(max_workers=1) as pool:
                if outcome == 'accepted': self.assertEqual(pool.submit(run).result(), ('ok', ''))
                else:
                    with self.assertRaises(Exception): pool.submit(run).result()
                with self.assertRaises(PermissionError): pool.submit(run).result()
            self.assertEqual(len(calls), int(outcome in ('accepted', 'timeout', 'forbidden')))
            self.assertEqual(api.request_policy.inflight, 0)
            self.assertEqual(permit.state, {'accepted': 'accepted', 'timeout': 'commit-dispatched', 'forbidden': 'rejected'}.get(outcome, 'canceled-before-dispatch'))

    def test_shared_admission_rechecks_after_validation_and_expired_queue_intent(self):
        api, clock, guard, _, resource = self.setup_api()
        identity = ('c0', 'configmap', 'ns', 'cm', 'u')
        revision, origin = credentials_revision(api.cluster('c0')), guard.context()
        for answer in (None, False):
            with self.assertRaises(PermissionError):
                guard.authorize(identity, revision, 'yaml', parameters=resource, origin=origin,
                                generation=guard.generation, validator=lambda cid: answer, deadline=clock()+60)
        calls = []
        with self.assertRaises(PermissionError):
            guard.authorize(identity, revision, 'yaml', parameters=resource, origin=origin,
                            generation=guard.generation, validator=lambda cid: calls.append(cid) or True, deadline=clock())
        self.assertEqual(calls, [])
        def expired_during_validation(cid):
            clock.advance(61)
            return True
        with self.assertRaises(PermissionError):
            guard.authorize(identity, revision, 'yaml', parameters=resource, origin=origin,
                            generation=guard.generation, validator=expired_during_validation, deadline=clock()+60)
        def locked_during_validation(cid):
            guard.invalidate()
            return True
        with self.assertRaises(PermissionError):
            guard.authorize(identity, revision, 'yaml', parameters=resource, origin=origin,
                            generation=guard.generation, validator=locked_during_validation, deadline=clock()+60)
        self.assertEqual(api.opener.calls, [])
    def test_real_dispatch_outcomes_have_correlated_nonsecret_audit_without_replay(self):
        from quaber.storage import UserStore
        from quaber.write_guard import operation_permit
        for outcome in ('canceled-before-dispatch', 'unknown', 'rejected', 'accepted'):
            api, clock, guard, permit, resource = self.setup_api()
            with tempfile.TemporaryDirectory(prefix='quaber-v3-audit-') as temp:
                # Audit-only fixture uses real operation()/audit(), no encrypted store
                # or DPAPI read is involved in this isolated nonsecret audit test.
                store = UserStore.__new__(UserStore)
                store.root, store._lock = Path(temp), threading.RLock()
                store.clusters = lambda: [{'id': 'c0', 'username': 'synthetic'}]
                if outcome == 'unknown':
                    def timeout(req): raise TimeoutError('synthetic unknown response')
                    api.opener.route = timeout
                elif outcome == 'rejected':
                    api.opener.route = lambda req: (403, {}, {'message': 'synthetic denial'})
                def execute():
                    with operation_permit(permit), store.operation('replace-configmap', 'c0', 'ns', 'cm'):
                        if outcome == 'canceled-before-dispatch': guard.invalidate()
                        api.request('c0', api.path('configmap', 'ns', 'cm'), 'PUT', resource, permit=permit)
                        raise RuntimeError('synthetic display refresh failure')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(Exception): pool.submit(execute).result()
                records = [json.loads(line) for line in (store.root/'audit.jsonl').read_text().splitlines()]
                self.assertEqual([r['phase'] for r in records], ['intent', outcome])
                self.assertTrue(all(r['operationId'] == permit.intent.operation_id for r in records))
                self.assertFalse(any('data' in r or 'command' in r for r in records))
                self.assertEqual(len(api.opener.calls), 0 if outcome == 'canceled-before-dispatch' else 1)
                with self.assertRaises(PermissionError):
                    api.request('c0', api.path('configmap', 'ns', 'cm'), 'PUT', resource, permit=permit)
    def test_unknown_and_negative_authorization_never_issue_dispatch_permit(self):
        api, clock, guard, _, resource = self.setup_api()
        for authorized in (False, None, 'yes', 1):
            with self.assertRaises(PermissionError):
                guard.issue(('c0', 'configmap', 'ns', 'cm', 'u'), credentials_revision(api.cluster('c0')),
                            'yaml', authorized=authorized, parameters=resource)
        self.assertEqual(api.opener.calls, [])

    def setup_api(self, lock_at=None):
        api, clock, clusters = fake_api()
        api.store = SimpleNamespace(operation=lambda *a, **kw: nullcontext())
        allowed = [True]
        guard = WriteGuard(lambda: allowed[0], lambda cid: credentials_revision(api.cluster(cid)), clock)
        resource = {'metadata': {'name': 'cm', 'namespace': 'ns', 'uid': 'u', 'resourceVersion': '1'}, 'data': {'x': 'v'}}
        def route(request):
            if (lock_at == 'get' and request.get_method() == 'GET'
                    or lock_at == 'dry-run' and 'dryRun=All' in request.selector):
                allowed[0] = False
                guard.invalidate()
            return 200, {}, copy.deepcopy(resource)
        api.opener.route = route
        permit = guard.issue(('c0', 'configmap', 'ns', 'cm', 'u'), credentials_revision(clusters[0]), 'yaml', authorized=True, parameters=resource)
        return api, clock, guard, permit, resource

    def test_lock_during_get_or_dryrun_prevents_commit(self):
        for boundary, writes in (('get', 0), ('dry-run', 1)):
            api, clock, guard, permit, resource = self.setup_api(boundary)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(PermissionError):
                    pool.submit(api.replace, 'c0', 'configmap', 'ns', 'cm', resource, '1', permit=permit).result()
            attempts = [call for call in api.opener.calls if call['method'] != 'GET']
            self.assertEqual(len(attempts), writes)
            self.assertTrue(all('dryRun=All' in call['path'] for call in attempts))

    def test_explicit_permit_missing_foreign_expired_and_duplicate_commit(self):
        api, clock, guard, permit, resource = self.setup_api()
        with self.assertRaises(PermissionError):
            api.replace('c0', 'configmap', 'ns', 'cm', resource, '1')
        with self.assertRaises(PermissionError):
            api.request('c0', '/apis/authorization.k8s.io/v1/anything', 'POST', {})
        with self.assertRaises(PermissionError):
            api.request('c0', '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', 'POST', {})
        self.assertEqual(api.opener.calls, [])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.replace, 'c0', 'configmap', 'ns', 'cm', resource, '1', permit=permit).result()
            self.assertEqual(permit.state, 'accepted')
            with self.assertRaises(PermissionError):
                pool.submit(api.request, 'c0', api.path('configmap', 'ns', 'cm'), 'PUT', resource, permit=permit).result()
        self.assertEqual(sum(c['method'] == 'PUT' and 'dryRun' not in c['path'] for c in api.opener.calls), 1)
        api, clock, guard, permit, resource = self.setup_api()
        clock.advance(61)
        with self.assertRaises(PermissionError): api.replace('c0', 'configmap', 'ns', 'cm', resource, '1', permit=permit)
        self.assertEqual(api.opener.calls, [])

    def test_lock_while_waiting_budget_is_checked_before_physical_opener(self):
        api, clock, guard, permit, resource = self.setup_api()
        api.request_policy.global_tokens = 0
        def wait(seconds):
            clock.advance(seconds)
            guard.invalidate()
        api.request_policy.wait = wait
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(PermissionError):
                pool.submit(api.request, 'c0', api.path('configmap', 'ns', 'cm'), 'PUT', resource, permit=permit).result()
        self.assertEqual(api.opener.calls, [])
        self.assertEqual(api.request_policy.inflight, 0)

    def test_changed_confirmed_parameters_never_dispatch(self):
        api, clock, guard, permit, resource = self.setup_api()
        changed = copy.deepcopy(resource)
        changed['data']['x'] = 'unconfirmed'
        for call in (lambda: api.replace('c0', 'configmap', 'ns', 'cm', changed, '1', permit=permit),
                     lambda: api.request('c0', api.path('configmap', 'ns', 'cm'), 'PUT', changed, permit=permit)):
            with self.assertRaises(PermissionError): call()
        self.assertEqual(api.opener.calls, [])
        self.assertEqual(permit.state, 'issued')

    def test_scale_exec_and_delete_bind_exact_user_input(self):
        api, clock, guard, _, resource = self.setup_api()
        for kind, action, confirmed, changed in (
                ('deployment', 'scale', {'replicas': 4}, {'replicas': 0}),
                ('pod', 'exec', {'container': 'c', 'command': 'ls'}, {'container': 'c', 'command': 'rm -rf /'}),
                ('pod', 'delete', {'mode': 'restart'}, {'mode': 'delete'})):
            permit = guard.issue(('c0', kind, 'ns', 'cm', 'u'), credentials_revision(api.cluster('c0')),
                                 action, authorized=True, parameters=confirmed)
            with self.assertRaises(PermissionError): permit.check_parameters(changed)
            permit.check_parameters(confirmed)
        self.assertEqual(api.opener.calls, [])
