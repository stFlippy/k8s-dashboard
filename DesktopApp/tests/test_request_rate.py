"""The developer rate affects both admission layers and persists as a preference."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from quaber.request_policy import RequestPolicy
from quaber.endpoint_budget import EndpointBudget, endpoint_key
from quaber.views import validate_preferences
from v3_helpers import FakeClock


class RequestRateTests(unittest.TestCase):
    def test_validation_and_default(self):
        self.assertEqual(validate_preferences({})['cluster_request_rate'],2)
        for value in (0,-1,101,True,'10',None,2.5):
            self.assertEqual(validate_preferences(dict(cluster_request_rate=value))['cluster_request_rate'],2)
        for rate in (2,4,6,8,10):
            self.assertEqual(validate_preferences(dict(cluster_request_rate=rate))['cluster_request_rate'],rate)

    def test_rate_change_preserves_tokens_and_keeps_burst_and_concurrency(self):
        clock=FakeClock()
        policy=RequestPolicy(clock=clock,wait=clock.advance)
        cluster=policy._cluster('c')
        cluster['tokens']=0
        clock.advance(.25)
        policy.set_cluster_rate(10)
        self.assertEqual(cluster['tokens'],.5)
        self.assertEqual(policy.limits.cluster_burst,2)
        self.assertEqual(policy.cluster_concurrency,1)
        clock.advance(.05)
        policy._refill(cluster)
        self.assertAlmostEqual(cluster['tokens'],1)
        for value in (0,101,True,2.5):
            with self.assertRaises(ValueError):policy.set_cluster_rate(value)

    def test_shared_endpoint_admission_uses_selected_rate(self):
        clock=FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            budget=EndpointBudget(Path(directory)/'requests.sqlite3',clock=clock)
            policy=RequestPolicy(clock=clock,wait=clock.advance,endpoint_budget=budget)
            policy.set_cluster_rate(10)
            key=endpoint_key('https://lab.invalid:6443')
            for _ in range(2):
                lease=budget.acquire(key)
                self.assertIsNotNone(lease)
                budget.release(key,lease)
            self.assertIsNone(budget.acquire(key))
            clock.advance(.101)
            lease=budget.acquire(key)
            self.assertIsNotNone(lease)
            budget.release(key,lease)
            self.assertEqual(budget.rate,10)


if __name__=='__main__':unittest.main()
