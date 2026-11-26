import copy
import threading
import unittest
from unittest.mock import Mock, patch
from PyQt5.QtTest import QSignalSpy
from test_ux import ShellFixture
from quaber.events_page import EventModel, collect_events, event_row
from quaber.kubernetes import AggregateListForbidden
from quaber.monitoring import MonitoringCancelled


def event(namespace='ns', uid='same', count=1):
    return {'metadata': {'namespace': namespace, 'uid': uid}, 'type': 'Normal',
            'reason': 'Started', 'message': 'container started', 'count': count,
            'lastTimestamp': '2026-11-25T00:00:00Z',
            'involvedObject': {'kind': 'Pod', 'name': 'app', 'uid': 'pod-uid'}}


class EventCollectionTests(unittest.TestCase):
    def test_rbac_fallback_preserves_accessible_namespaces_and_reports_denied(self):
        api = Mock()
        api.all_namespace_resources.side_effect = AggregateListForbidden('limited')
        api.namespaces.return_value = ['allowed', 'denied']
        api.snapshot_list.side_effect = [[event('allowed')], PermissionError('no access')]
        rows, errors = collect_events(api, {'id': 'c'})
        self.assertEqual([r['metadata']['namespace'] for r in rows], ['allowed'])
        self.assertEqual(errors, ['denied: no access'])
        api.snapshot_list.side_effect = MonitoringCancelled('hidden')
        with self.assertRaises(MonitoringCancelled):
            collect_events(api, {'id': 'c'}, 'allowed')

    def test_identical_snapshots_do_not_reset_or_emit_data_changes(self):
        model = EventModel()
        rows = [event_row({'id': cid, 'name': cid}, event()) for cid in ('c1', 'c2')]
        model.replace(rows)
        reset, changed = QSignalSpy(model.modelReset), QSignalSpy(model.dataChanged)
        model.replace(copy.deepcopy(rows))
        self.assertEqual((len(reset), len(changed)), (0, 0))
        self.assertEqual(len(model.rows), 2)
        rows[0] = event_row({'id': 'c1', 'name': 'c1'}, event(count=2))
        model.replace(rows)
        self.assertEqual((len(reset), len(changed)), (0, 1))


class EventsPageTests(ShellFixture, unittest.TestCase):
    def test_defaults_merge_all_clusters_and_namespaces_then_filter(self):
        self.window.store.save_clusters([
            {'id': cid, 'name': cid, 'enabled': True} for cid in ('c1', 'c2')])
        self.api.all_namespace_resources.side_effect = lambda cid, kind: [event('a'), event('b', uid='other')]
        page = self.window.events_page
        self.window.navigate_workspace(self.window.tabs.indexOf(page))
        self.wait()
        self.assertEqual(page.clusters.ids(), {'c1', 'c2'})
        self.assertIsNone(page.namespace.currentData())
        self.assertEqual(len(page.model.rows), 4)
        self.assertEqual(self.api.all_namespace_resources.call_count, 2)
        page.search_field.setText('c1')
        self.assertEqual(len(page.model.rows), 2)
        records = self.window.store.clusters()
        records[0]['enabled'] = False
        self.window.store.save_clusters(records)
        page.reconcile()
        self.assertEqual(len(page.model.rows), 0)
        page.search_field.clear()
        self.assertEqual({r.key[0] for r in page.model.rows}, {'c2'})

    def test_leaving_events_cancels_reads_and_open_card_has_no_events_demand(self):
        page = self.window.events_page
        canceled = threading.Event()
        page.operations['c'] = (object(), canceled)
        page.set_demanded(False)
        self.assertTrue(canceled.is_set())
        self.assertFalse(page.operations)
        with patch.object(self.window.background, 'submit', return_value=True) as submit:
            self.window.open_resource('c', 'deployment', 'ns', 'app')
            page.refresh_data()
            self.assertEqual(submit.call_count, 1)
            submit.call_args.kwargs['finalized']('CANCELLED')
