import copy
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import unittest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from quaber.container_instance import capture_instance
from quaber.request_policy import credentials_revision,RequestPolicy
import test_v3_writes as write_fixture
from test_ux import ShellFixture
from test_v3_requests import fake_api


def pod():
    return dict(metadata={'name':'p','uid':'pod-uid'},spec={'containers':[{'name':'app'}]},
                status={'phase':'Running','containerStatuses':[dict(name='app',containerID='synthetic://1',
                    restartCount=0,state={'running':{'startedAt':'2026-11-20T00:00:00Z'}})]})


class InstanceContracts(unittest.TestCase):
    def test_same_pod_instance_restart_and_lifecycle_changes_block_physical_exec(self):
        for change in ('id','restart','terminated','deleting','phase','missing'):
            with self.subTest(change=change):
                api,clock,guard,_,_ = write_fixture.WriteTests.setup_api(self)
                approved = capture_instance(pod(),'app')
                current = pod()
                item = current['status']['containerStatuses'][0]
                if change=='id': item['containerID']='synthetic://2'
                if change=='restart': item['restartCount']=1
                if change=='terminated': item['state']={'terminated':{'exitCode':0}}
                if change=='deleting': current['metadata']['deletionTimestamp']='2026-11-20T00:01:00Z'
                if change=='phase': current['status']['phase']='Succeeded'
                if change=='missing': current['status']['containerStatuses']=[]
                api.opener.route = lambda request:(200,{},copy.deepcopy(current))
                boundary = api.proxy.execute = Mock()
                permit = guard.issue(('c0','pod','ns','p','pod-uid'),credentials_revision(api.cluster('c0')),
                    'exec',authorized=True,parameters={'container':'app','command':'printf synthetic','instance':approved.parameters()})
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(PermissionError):
                        pool.submit(api.execute_pod,'c0','ns','p','pod-uid','app','printf synthetic',
                                    expected_instance=approved,permit=permit).result()
                boundary.assert_not_called()
                self.assertEqual(permit.state,'canceled-before-dispatch')
                self.assertEqual(len(api.opener.calls),1)

    def test_baseline_is_immutable_and_bool_restart_is_not_a_counter(self):
        instance = capture_instance(pod(),'app')
        with self.assertRaises(FrozenInstanceError): instance.restart_count=7
        broken = pod()
        broken['status']['containerStatuses'][0]['restartCount']=False
        with self.assertRaises(PermissionError): capture_instance(broken,'app')


class TerminalInputContracts(ShellFixture,unittest.TestCase):
    def test_real_enter_rejects_changed_instance_and_preserves_command(self):
        panel = self.open_panel('pod')
        cluster = self.api.cluster('c')
        api,clock,clusters = fake_api()
        clusters[:] = [cluster]
        api.store = self.window.store
        api.request_policy = RequestPolicy()
        api.monitoring = self.window.monitoring
        boundary = api.proxy.execute = Mock()
        current = copy.deepcopy(self.pod)
        current['status']['containerStatuses'][0]['containerID']='synthetic://replacement'
        def route(request):
            if request.selector.endswith('/selfsubjectaccessreviews'):
                return 201,{},dict(status={'allowed':True})
            return 200,{},copy.deepcopy(current)
        api.opener.route = route
        self.window.api = panel.api = api
        self.window.admin_access.check('c',force=True)
        self.wait()
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        command = 'printf reviewed'
        panel.console_input.setText(command)
        QTest.keyClick(panel.console_input,Qt.Key_Return)
        self.wait()
        boundary.assert_not_called()
        self.assertEqual(panel.console_input.text(),command)
        self.assertTrue(self.window.preferences['read_only'])
        self.assertIn('Экземпляр контейнера изменился',panel.console_output.toPlainText())
