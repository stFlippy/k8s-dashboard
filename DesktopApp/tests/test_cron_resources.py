"""CronJob navigation remains available after removal of the Job log tab."""
import copy
import unittest
from unittest.mock import patch
from test_ux import ShellFixture


class CronResourceTests(ShellFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cron = dict(metadata=dict(name='nightly', namespace='ns', uid='cron-uid', resourceVersion='1'))
        self.jobs, self.pods = [], []
        for number in (1, 2):
            self.jobs.append(dict(metadata=dict(name=f'job-{number}', uid=f'job-uid-{number}',
                creationTimestamp=f'2026-11-2{number}T00:00:00Z',
                ownerReferences=[dict(kind='CronJob', uid='cron-uid', controller=True)])))
            pod = copy.deepcopy(self.pod)
            pod['metadata'].update(name=f'job-pod-{number}', uid=f'pod-uid-{number}',
                ownerReferences=[dict(kind='Job', uid=f'job-uid-{number}', controller=True)])
            self.pods.append(pod)
        foreign = copy.deepcopy(self.jobs[1])
        foreign['metadata'].update(name='foreign', uid='foreign')
        foreign['metadata']['ownerReferences'][0]['uid'] = 'deleted-cron-uid'
        self.jobs.append(foreign)
        self.api.list_resources.side_effect = lambda cid, kind, ns: copy.deepcopy(self.jobs)
        self.api.pods.return_value = self.pods
        self.api.resource.side_effect = lambda cid, kind, ns, name: self.cron if kind == 'cronjob' else next(p for p in self.pods if p['metadata']['name'] == name)
        self.window.open_resource('c', 'cronjob', 'ns', 'nightly')
        self.wait()
        self.panel = self.window._resource_panel

    def test_job_log_tab_is_absent_and_overview_does_not_read_job_or_pod_lists(self):
        self.assertNotIn('Логи Job', [self.panel.tabs.tabText(i) for i in range(self.panel.tabs.count())])
        self.assertFalse(hasattr(self.panel, 'cron_logs'))
        self.api.logs.assert_not_called()
        self.api.list_resources.assert_not_called()

    def test_jobs_and_pods_are_lazy_uid_scoped_and_open_the_owned_resource(self):
        jobs = self.panel.cron_jobs
        self.panel.tabs.setCurrentWidget(jobs)
        self.wait()
        self.assertEqual(jobs.table.rowCount(), 2)
        self.assertEqual(jobs.table.item(0, 0).text(), 'job-2')
        self.api.pods.assert_not_called()
        self.panel.tabs.setCurrentWidget(self.panel.cron_pods)
        self.wait()
        pods = self.panel.cron_pods
        self.assertEqual(pods.table.rowCount(), 2)
        with patch.object(self.window, 'open_resource') as opened: pods.open_row(0, 0)
        self.assertEqual(opened.call_args.args[1], 'pod')
        self.assertTrue(opened.call_args.kwargs['expected_uid'].startswith('pod-uid-'))

    def test_unchanged_resources_keep_table_item_identity(self):
        jobs = self.panel.cron_jobs
        self.panel.tabs.setCurrentWidget(jobs)
        self.wait()
        item = jobs.table.topLevelItem(0)
        jobs.refresh_data()
        self.wait()
        self.assertIs(jobs.table.topLevelItem(0), item)

    def test_latest_available_pod_can_come_from_older_retained_job(self):
        from quaber.cron_history import latest_available_pod
        chosen = latest_available_pod('cron-uid', self.jobs, [self.pods[0]])
        self.assertEqual(chosen['metadata']['uid'], 'pod-uid-1')
        wrong = copy.deepcopy(self.pods[1])
        wrong['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        self.assertEqual(latest_available_pod('cron-uid', self.jobs, [self.pods[0], wrong]), chosen)
