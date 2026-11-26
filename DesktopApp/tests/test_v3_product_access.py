"""Real GUI admission and Kubernetes dispatch, synthetic physical MVCC only."""
import copy
import json
import unittest
from unittest.mock import patch
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from test_ux import ShellFixture
from test_v3_requests import fake_api


class ScopedAccessIntegration(ShellFixture, unittest.TestCase):
    def test_namespace_scale_editor_uses_scoped_ssar_and_one_conditional_commit(self):
        panel = self.open_panel('deployment')
        cluster = self.api.cluster('c')
        api, clock, clusters = fake_api()
        clusters[:] = [cluster]
        api.store = self.window.store
        resource = copy.deepcopy(self.deployment)
        resource['metadata']['namespace'] = 'ns'
        calls, commits = [], []
        def route(request):
            path = request.selector.split('?',1)[0]
            body = json.loads(request.data) if request.data else None
            method = request.get_method()
            calls.append((method,path,body))
            if path.endswith('/selfsubjectaccessreviews'):
                a = body['spec']['resourceAttributes']
                allowed = a == dict(group='apps',resource='deployments',subresource='scale',
                                    namespace='ns',name='app',verb='update')
                return 201,{},dict(status={'allowed':allowed})
            if method == 'GET':
                if path.endswith('/deployments/app'): return 200,{},copy.deepcopy(resource)
                return 200,{},dict(items=[])
            if method == 'PUT' and path.endswith('/deployments/app/scale'):
                metadata = body['metadata']
                if metadata.get('uid') != resource['metadata']['uid'] or metadata.get('resourceVersion') != resource['metadata']['resourceVersion']:
                    return 409,{},dict(message='synthetic MVCC conflict')
                if 'dryRun=All' not in request.selector:
                    commits.append(copy.deepcopy(body))
                    resource['spec']['replicas'] = body['spec']['replicas']
                    resource['metadata']['resourceVersion'] = 'opaque:committed'
                return 200,{},copy.deepcopy(resource)
            raise AssertionError((method,path))
        api.opener.route = route
        self.window.api = panel.api = api
        api.monitoring = self.window.monitoring
        self.window.admin_access.check('c',force=True)
        self.wait()
        self.assertTrue(self.window.admin_access.allows('scale'))
        self.assertFalse(self.window.admin_access.allows('yaml'))
        self.window.set_read_only(False)
        panel.update_write_controls()
        self.assertTrue(panel.replica_up.isEnabled())
        self.assertFalse(panel.yaml_save.isEnabled())
        QTest.mouseClick(panel.replica_up,Qt.LeftButton)
        with patch('quaber.ui.error') as error:
            QTest.mouseClick(panel.scale_apply,Qt.LeftButton)
            self.wait()
        error.assert_not_called()
        self.assertEqual(len(commits),1)
        self.assertEqual(commits[0]['metadata']['resourceVersion'],'1')
        self.assertEqual(resource['spec']['replicas'],2)
        ssar = [body['spec']['resourceAttributes'] for method,path,body in calls if path.endswith('/selfsubjectaccessreviews')]
        self.assertGreaterEqual(sum(a.get('subresource')=='scale' for a in ssar),2)
        self.assertFalse(any(a.get('resource')=='*' for a in ssar))
        self.assertTrue(self.window.preferences['read_only'])
