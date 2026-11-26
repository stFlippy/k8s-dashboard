import json
import unittest
from quaber.capabilities import ActionTarget, AccessOutcome, review_action
from test_v3_requests import fake_api


class CapabilityTests(unittest.TestCase):
    def test_physical_ssar_is_scoped_to_named_scale_subresource(self):
        api, clock, clusters = fake_api()
        bodies = []
        def route(request):
            bodies.append(json.loads(request.data))
            return 201, {}, {'status': {'allowed': True}}
        api.opener.route = route
        target = ActionTarget('c0','deployment','app-ns','app','scale')
        decision = review_action(api,target)
        self.assertEqual(decision.outcome,AccessOutcome.ALLOWED)
        self.assertEqual(bodies[0]['spec']['resourceAttributes'],dict(group='apps',resource='deployments',
            namespace='app-ns',name='app',verb='update',subresource='scale'))
        self.assertEqual(len(api.opener.calls),1)

    def test_denied_malformed_and_evaluation_errors_never_allow(self):
        for status, outcome in [({'allowed':False},AccessOutcome.DENIED),
                                ({'allowed':None},AccessOutcome.UNKNOWN),
                                ({'allowed':True,'evaluationError':'incomplete'},AccessOutcome.UNKNOWN),
                                ({'allowed':True,'denied':True},AccessOutcome.UNKNOWN)]:
            api, clock, clusters = fake_api()
            api.opener.route = lambda request:(201,{},dict(status=status))
            self.assertEqual(review_action(api,ActionTarget('c0','pod','ns','p','exec')).outcome,outcome)

    def test_eviction_and_delete_permissions_are_distinct(self):
        direct = ActionTarget('c0','pod','ns','p','delete').attributes()
        eviction = ActionTarget('c0','pod','ns','p','delete',True).attributes()
        self.assertEqual(direct['verb'],'delete')
        self.assertEqual(eviction['verb'],'create')
        self.assertEqual(eviction['subresource'],'eviction')
        with self.assertRaises(ValueError): ActionTarget('c0','pod','ns','p','scale').attributes()
