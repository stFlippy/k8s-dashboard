"""Selector events, runtime races and physical log API status classification."""
import copy
import threading
import time
import unittest
from unittest.mock import Mock
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMenu
from helpers_log_ui import LogFixture as LogUXTests
from test_v3_requests import fake_api
from quaber.kubernetes import KubernetesAPIError
from quaber.log_source import runtime_fingerprint
from test_ux import ShellFixture


class LogSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): LogUXTests.setUpClass(); cls.app = LogUXTests.app
    def setUp(self):
        LogUXTests.setUp(self)
        self.bg.finish('current')
        self.owner.show()
        self.view.show()
        self.app.processEvents()

    def resource(self,restarts=0,container_id='synthetic://1'):
        return dict(metadata={'uid':'pod-uid'},spec={'containers':[{'name':'app'}]},
                    status={'containerStatuses':[dict(name='app',restartCount=restarts,containerID=container_id)]})

    def test_unavailable_receives_real_click_enter_space_without_menu_or_get(self):
        self.view.update_resource(self.resource())
        selector = self.view.previous
        self.assertEqual(selector.availability,'UNAVAILABLE')
        self.assertTrue(selector.isEnabled())
        calls = len(self.bg.jobs)
        QTest.mouseClick(selector,Qt.LeftButton)
        QTest.keyClick(selector,Qt.Key_Return)
        QTest.keyClick(selector,Qt.Key_Space)
        self.assertFalse(any(menu.isVisible() for menu in selector.findChildren(QMenu)))
        self.assertEqual(len(self.bg.jobs),calls)
        selector.setChecked(True)
        self.assertFalse(selector.isChecked())
        self.view.load()
        self.bg.finish('fresh current')
        self.assertEqual(self.view.output.toPlainText(),'fresh current')

    def test_physical_empty_previous_probe_and_one_read_without_polling(self):
        api, clock, clusters = fake_api()
        self.owner.api = api
        calls = []
        def route(request):
            calls.append(request.selector)
            return 200,{},b''
        api.opener.route = route
        # Existing UI fake has cid c; physical client is bound to its synthetic c0.
        self.owner.cluster_id = 'c0'
        self.view.update_resource(self.resource(1))
        self.bg.finish(execute=True)
        self.assertEqual(self.view.previous.availability,'AVAILABLE')
        self.assertIn('tailLines=1',calls[0])
        self.view.previous.setChecked(True)
        self.bg.finish(execute=True)
        self.assertIn('previous=true',calls[-1])
        self.assertIn('Контейнер пока',self.view.status.text())
        for _ in range(3): self.view.load(poll=True)
        self.assertFalse(self.bg.jobs)
        self.assertEqual(len(calls),2)

    def test_exact_missing_previous_is_cached_but_forbidden_is_error(self):
        api, clock, clusters = fake_api()
        self.owner.api = api
        self.owner.cluster_id = 'c0'
        api.opener.route = lambda r:(400,{},dict(reason='BadRequest',message='previous terminated container "app" in pod "pod" not found'))
        self.view.update_resource(self.resource(1))
        self.bg.finish(execute=True)
        self.assertEqual(self.view.previous.availability,'UNAVAILABLE')
        self.view._check_previous()
        self.assertFalse(self.bg.jobs)
        api.opener.route = lambda r:(403,{},dict(reason='Forbidden'))
        self.view.update_resource(self.resource(2,'synthetic://2'))
        self.bg.finish(execute=True)
        self.assertEqual(self.view.previous.availability,'ERROR')

    def test_restarted_instance_discards_inflight_current_and_old_probe(self):
        self.view.update_resource(self.resource())
        self.view.load()
        self.view.update_resource(self.resource(1,'synthetic://2'))
        self.bg.finish('old instance')
        self.assertNotEqual(self.view.output.toPlainText(),'old instance')
        self.view.update_resource(self.resource(2,'synthetic://3'))
        self.bg.finish('AVAILABLE')  # old fingerprint probe cannot change current availability
        self.assertEqual(self.view.previous.availability,'CHECKING')

    def test_normal_init_and_ephemeral_statuses_do_not_mix(self):
        resource = self.resource()
        resource['spec']['initContainers'] = [{'name':'init'}]
        resource['status']['initContainerStatuses'] = [dict(name='init',restartCount=4,containerID='init://4')]
        self.assertEqual(runtime_fingerprint(resource,'app')[2],0)
        self.assertEqual(runtime_fingerprint(resource,'init')[2],4)


class LogPhysicalWorkerTests(ShellFixture,unittest.TestCase):
    def test_tab_show_old_cancel_cannot_clear_current_read_loading(self):
        from helpers_background import DeferredBackend
        from quaber.background import PanelBackground
        panel = self.open_panel('pod')
        backend = DeferredBackend()
        panel.background = PanelBackground(backend,panel)
        panel.tabs.setCurrentWidget(panel.log_view)
        self.app.processEvents()
        self.assertEqual(len(backend.jobs),2)  # showEvent, then current tab epoch
        old,current = backend.jobs
        self.assertTrue(panel.log_view.loading)
        operation = panel.log_view._load_operation
        backend.finish(job_id=old.id,cancel=True)
        self.assertTrue(panel.log_view.loading)
        self.assertIs(panel.log_view._load_operation,operation)
        backend.finish('current instance',job_id=current.id)
        self.assertFalse(panel.log_view.loading)
        self.assertEqual(panel.log_view.output.toPlainText(),'current instance')

    def test_actual_panel_worker_probe_and_previous_are_bound_to_runtime(self):
        panel = self.open_panel('pod')
        cluster = self.api.cluster('c')
        api, clock, clusters = fake_api()
        clusters[:] = [cluster]
        # Concurrent SSAR/probe workers need actual monotonic admission. The
        # shared fake clock's zero-wait advance can exhaust a neighbour's budget.
        from quaber.request_policy import RequestPolicy
        api.request_policy = RequestPolicy()
        self.window.api = panel.api = api
        api.monitoring = self.window.monitoring
        calls = []
        gui = threading.get_ident()
        def route(request):
            calls.append((threading.get_ident(),request.selector))
            if '/log?' in request.selector:
                return 200,{},b'' if request.selector.endswith('tailLines=1') else b'synthetic log'
            if request.selector.split('?')[0].endswith('/pods/app-pod'):
                return 200,{},copy.deepcopy(resource)
            return 200,{},dict(items=[])
        api.opener.route = route
        resource = copy.deepcopy(self.pod)
        resource['status']['containerStatuses'] = [dict(name='app',containerID='test://2',restartCount=1)]
        panel._adopt_live(resource)
        panel.tabs.setCurrentWidget(panel.log_view)
        # showEvent's old tab read can settle before its replacement probe is
        # queued. Observe the user-visible result instead of that idle gap.
        deadline = time.monotonic() + 5
        while (panel.log_view.previous.availability in ('UNKNOWN','CHECKING')
               and time.monotonic() < deadline):
            QTest.qWait(10)
        self.assertEqual(panel.log_view.previous.availability,'AVAILABLE',
                         repr((panel.log_view.isVisible(),panel.log_view._resource,
                               panel.log_view._availability_key,panel.log_view.previous.toolTip(),calls)))
        panel.log_view.previous.setChecked(True)
        deadline = time.monotonic() + 5
        while (panel.log_view._display_source != ('app', True, 1000)
               and time.monotonic() < deadline):
            QTest.qWait(10)
        self.assertEqual(panel.log_view._display_source,('app', True, 1000))
        self.wait()
        logs = [path for thread,path in calls if '/log?' in path]
        self.assertEqual(sum('previous=true' in path and path.endswith('tailLines=1') for path in logs),1)
        self.assertEqual(sum('previous=true' in path and 'tailLines=1000' in path for path in logs),1)
        self.assertTrue(all(thread != gui for thread,path in calls))
        count = len(calls)
        panel.log_view.load(poll=True)
        self.wait()
        self.assertEqual(len(calls),count)
