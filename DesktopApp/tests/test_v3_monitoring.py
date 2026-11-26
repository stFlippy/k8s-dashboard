import contextvars
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from quaber.monitoring import MonitoringState, MonitoringCancelled, bind_read, idle_minutes
from quaber.cache import SnapshotCache
from quaber.kubernetes import Kubernetes
from quaber.views import validate_preferences
from v3_helpers import FakeClock, FakeHTTP


class MonitoringTests(unittest.TestCase):
    def test_network_due_jitter_preserves_base_without_drifting_or_touching_ttl(self):
        from PyQt5.QtCore import QTimer
        from quaber.polling import jitter_poll_timer, poll_interval
        timer, ttl = QTimer(), QTimer()
        timer.setInterval(1000)
        ttl.setInterval(60000)
        for factor in (1.2, 1., 1.1, 1.2):
            jitter_poll_timer(timer, rng=lambda a, b: factor)
            self.assertEqual(poll_interval(timer), 1000)
            self.assertEqual(timer.interval(), round(1000*factor))
            self.assertEqual(ttl.interval(), 60000)
        timer.setInterval(5000)
        jitter_poll_timer(timer, rng=lambda a, b: 1.2)
        self.assertEqual(poll_interval(timer), 5000)
        self.assertEqual(timer.interval(), 6000)

    def test_rollout_fast_burst_has_fixed_deadline_and_needs_explicit_renewal(self):
        from quaber.rollout import RolloutCadence
        clock = FakeClock()
        cadence = RolloutCadence(clock)
        self.assertEqual(cadence.interval(True), 1000)
        clock.advance(119)
        self.assertEqual(cadence.interval(True), 1000)
        clock.advance(1)
        self.assertEqual(cadence.interval(True), 5000)
        self.assertEqual(cadence.interval(False), 5000)
        clock.advance(3600)
        self.assertEqual(cadence.interval(True), 5000)
        cadence.request_fast()
        self.assertEqual(cadence.interval(True), 1000)
    def test_timeouts_invalid_values_live_reconfiguration_and_latched_sleep(self):
        for value in (None, True, 3.5, '30', 0, -1, 9, 121, 10**30):
            self.assertEqual(idle_minutes(value), 30)
            self.assertEqual(validate_preferences({'monitoring_idle_minutes': value})['monitoring_idle_minutes'], 30)
        for minutes in (10, 11, 30, 120):
            clock = FakeClock()
            state = MonitoringState(minutes, clock)
            clock.advance(minutes*60-1)
            self.assertFalse(state.tick())
            clock.advance(1)
            self.assertTrue(state.tick())
            state.activity()
            state.set_idle_minutes(120)
            self.assertEqual(state.state, 'SLEEPING')
            self.assertTrue(state.reconnect())
            self.assertFalse(state.reconnect())
            state.reconnected(True)
            self.assertEqual(state.state, 'ACTIVE')
        clock = FakeClock()
        state = MonitoringState(30, clock)
        clock.advance(1000)
        self.assertTrue(state.set_idle_minutes(10))
        self.assertEqual(state.state, 'SLEEPING')

    def test_multiple_reasons_cancellation_and_old_epoch_after_reconnect(self):
        state = MonitoringState()
        old = state.token()
        state.soft('minimized', True)
        state.sleep('disconnect')
        state.soft('minimized', False)
        self.assertFalse(state.allows(old))
        self.assertEqual(state.state, 'SLEEPING')
        self.assertTrue(state.reconnect())
        state.reconnected(True)
        with self.assertRaises(MonitoringCancelled): state.check(old)
        state.close()
        self.assertFalse(state.allows())

    def test_pause_inside_first_response_prevents_next_page_physical_dispatch(self):
        clock = FakeClock()
        state = MonitoringState(clock=clock)
        api = Kubernetes.__new__(Kubernetes)
        api.monitoring = state
        api.stopped = False
        api._request_slots = threading.BoundedSemaphore(8)
        api.cluster = lambda _: {'enabled': True}
        api.proxy = SimpleNamespace(url=lambda _: 'http://synthetic.invalid')
        def route(request):
            state.soft('minimized', True)
            return 200, {}, {'items': [], 'metadata': {'continue': 'next'}}
        api.opener = FakeHTTP(route, clock)
        with self.assertRaises(MonitoringCancelled), state.read_scope(state.token()):
            api.list_pages('c', '/api/v1/pods')
        self.assertEqual(len(api.opener.calls), 1)
        state.soft('minimized', False)
        self.assertTrue(state.allows())

    def test_inherited_pool_context_and_cache_cannot_publish_canceled_epoch(self):
        state = MonitoringState()
        cache = SnapshotCache()
        with state.read_scope(state.token()):
            def load():
                state.sleep('lock')
                return {'secret_free': True}
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(bind_read(lambda: cache.get('key', load)))
                with self.assertRaises(MonitoringCancelled): future.result()
        self.assertNotIn('key', cache.entries)
        self.assertEqual(cache.active, {})
