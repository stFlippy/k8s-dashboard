import unittest
from quaber.widgets import pod_status, pod_marker_style, COLORS


class JobPresentationTests(unittest.TestCase):
    def test_completed_is_green_without_recent(self):
        pod = dict(status={'phase':'Succeeded'},_recent=True)
        self.assertEqual(pod_status(pod),'Completed')
        self.assertEqual(pod_marker_style(pod),('#43B694',False))

    def test_only_all_known_regular_containers_successfully_terminated_is_finishing(self):
        pod = dict(spec={'containers':[{'name':'app'},{'name':'sidecar'}]},
                   status={'phase':'Running','containerStatuses':[
                       {'name':'app','ready':False,'state':{'terminated':{'exitCode':0}}},
                       {'name':'sidecar','ready':False,'state':{'terminated':{'exitCode':0}}}]},_recent=True)
        self.assertEqual(pod_status(pod),'Finishing')
        self.assertEqual(pod_marker_style(pod),(COLORS['Finishing'],False))
        pod['status']['containerStatuses'][1]['state'] = {'running':{}}
        self.assertEqual(pod_status(pod),'Pending')
        pod['status']['containerStatuses'][1]['state'] = {'terminated':{'exitCode':7}}
        self.assertEqual(pod_status(pod),'Failed')
        pod['spec']['containers'] = []
        pod['status']['containerStatuses'] = []
        self.assertNotEqual(pod_status(pod),'Finishing')
