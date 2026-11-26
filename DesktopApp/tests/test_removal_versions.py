import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_ux import ShellFixture
from test_v3_requests import fake_api
from helpers_permits import bound_permit
from quaber.conflicts import ResourceVersionConflict
from quaber.request_policy import RequestPolicy
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest


class RemovalVersionTests(ShellFixture,unittest.TestCase):
    def physical(self):
        api,clock,clusters = fake_api()
        clusters[:] = [self.api.cluster('c')]
        api.store = self.window.store
        api.request_policy = RequestPolicy()
        api.monitoring = self.window.monitoring
        self.window.api = self.api = api
        resource = copy.deepcopy(self.pod)
        calls,commits = [],[]
        def route(request):
            method,path = request.get_method(),request.selector.split('?')[0]
            body = json.loads(request.data) if request.data else None
            calls.append((method,path,body))
            if path.endswith('/selfsubjectaccessreviews'):
                return 201,{},dict(status={'allowed':True})
            if method=='GET': return 200,{},copy.deepcopy(resource)
            options = body if method=='DELETE' else body['deleteOptions']
            preconditions = options['preconditions']
            if preconditions != {key:resource['metadata'][key] for key in ('uid','resourceVersion')}:
                return 409,{},dict(message='synthetic MVCC removal conflict')
            commits.append(copy.deepcopy(body))
            return 200,{},dict(status='Success')
        api.opener.route = route
        return resource,calls,commits

    def test_context_menu_same_uid_changed_version_is_rejected_after_confirmation(self):
        resource,calls,commits = self.physical()
        def approve(*args):
            resource['metadata']['resourceVersion'] = 'opaque:changed'
            return 'restart'
        self.window.set_read_only(False)
        with patch('quaber.resource_actions.confirm_pod_removal',side_effect=approve), \
             patch('quaber.resource_actions.QMessageBox.warning') as warning:
            self.window.resource_actions.delete_pod(self.identity)
            self.wait()
        self.assertFalse(commits)
        self.assertFalse(any(method in ('DELETE',) or path.endswith('/eviction') for method,path,body in calls))
        self.assertTrue(any(path.endswith('/selfsubjectaccessreviews') for method,path,body in calls))
        self.assertEqual(warning.call_count,1)
        self.assertFalse(self.window._pod_operations)
        self.assertTrue(self.window.preferences['read_only'])

    def test_context_menu_approved_version_reaches_actual_eviction(self):
        resource,calls,commits = self.physical()
        self.window.set_read_only(False)
        with patch('quaber.resource_actions.confirm_pod_removal',return_value='restart'), \
             patch('quaber.resource_actions.QMessageBox.warning') as warning:
            self.window.resource_actions.delete_pod(self.identity)
            self.wait()
        warning.assert_not_called()
        self.assertEqual(len(commits),1)
        self.assertEqual(commits[0]['deleteOptions']['preconditions'],
                         {'uid':self.identity[-1],'resourceVersion':'1'})

    def test_card_button_keeps_confirmed_rv_when_live_pod_changes_in_dialog(self):
        panel = self.open_panel('pod')
        resource,calls,commits = self.physical()
        panel.api = self.api
        self.window.admin_access.check('c',force=True)
        self.wait()
        self.window.set_read_only(False)
        panel.update_write_controls()
        button = next(b for b in panel.write_controls if b.property('operationAction')=='delete')
        self.assertTrue(button.isEnabled())
        def approve(*args):
            resource['metadata']['resourceVersion'] = '2'
            return 'restart'
        with patch('quaber.resource_actions.confirm_pod_removal',side_effect=approve), \
             patch('quaber.ui.error') as error:
            QTest.mouseClick(button,Qt.LeftButton)
            self.wait()
        self.assertEqual(error.call_count,1)
        self.assertFalse(commits)
        self.assertFalse(any(path.endswith('/eviction') for method,path,body in calls))

    def test_race_after_preflight_is_rejected_by_physical_mvcc_without_replay(self):
        resource,calls,commits = self.physical()
        route = self.api.opener.route
        reads = []
        def racing(request):
            response = route(request)
            if request.get_method()=='GET':
                reads.append(True)
                if len(reads)==2:
                    resource['metadata']['resourceVersion'] = 'opaque:after-get'
            return response
        self.api.opener.route = racing
        self.window.set_read_only(False)
        with patch('quaber.resource_actions.confirm_pod_removal',return_value='restart'), \
             patch('quaber.resource_actions.QMessageBox.warning') as warning:
            self.window.resource_actions.delete_pod(self.identity)
            self.wait()
        self.assertFalse(commits)
        attempted = [body for method,path,body in calls if path.endswith('/eviction')]
        self.assertEqual(len(attempted),1)
        self.assertEqual(attempted[0]['deleteOptions']['preconditions']['resourceVersion'],'1')
        self.assertEqual(warning.call_count,1)

    def test_rest_boundary_rejects_rebased_rv_under_old_permit_before_http(self):
        resource,calls,commits = self.physical()
        permit = bound_permit(self.api,self.identity,'delete',{'mode':'restart','resourceVersion':'1'})
        body = dict(apiVersion='policy/v1',kind='Eviction',metadata={'name':'app-pod','namespace':'ns'},
                    deleteOptions={'preconditions':{'uid':self.identity[-1],'resourceVersion':'2'}})
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(PermissionError):
                pool.submit(self.api.request,'c',self.api.path('pod','ns','app-pod')+'/eviction',
                            'POST',body,permit=permit).result()
        self.assertFalse(calls)
        self.assertFalse(commits)
