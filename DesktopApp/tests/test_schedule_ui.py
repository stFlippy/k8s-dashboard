from qt_helpers import label_text
import unittest
from unittest.mock import patch
from PyQt5.QtTest import QTest
from PyQt5.QtCore import Qt, QPoint
from quaber.widgets import pod_rects,COLORS
from quaber.views import validate_preferences
import test_perf_matrix as fixture


class ScheduleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.MatrixTests.setUpClass()
        cls.app = fixture.MatrixTests.app
    setUp = fixture.MatrixTests.setUp
    tearDown = fixture.MatrixTests.tearDown

    def cron(self):
        self.window.preferences['controller_timezones'] = {'c': 'UTC'}
        scope = self.window.snapshot['c']['namespaces']['ns']
        scope['cron_jobs'] = [dict(kind='CronJob',metadata=dict(uid='cron-1',name='nightly',namespace='ns'),
                                   spec=dict(schedule='*/5 * * * *',timeZone='UTC'))]
        self.window.render_matrix()
        for _ in range(40):
            QTest.qWait(10)
            if not self.window.schedules.busy: break
        self.window.render_matrix()
        row = self.window._row_names.index('CronJob/nightly')
        return row,scope

    def test_clock_is_owner_not_pod_and_tick_has_no_requests_or_model_reset(self):
        row,scope = self.cron()
        visual = self.window.matrix.cell_visuals(row,1)[0]
        self.assertEqual(visual.identity[1],'cronjob')
        self.assertEqual(visual.status,'Scheduled')
        self.assertNotEqual(COLORS['Scheduled'],COLORS['Pending'])
        self.assertEqual(self.window.matrix_data[(row,1)][4],[])
        self.assertIn('1  Total Pods',label_text(self.window.summary_label))
        old = self.window.matrix.item(row,1)
        with patch.object(self.window.api,'request') as request,patch.object(self.window.matrix,'clear') as clear:
            for _ in range(4): self.window.schedules.tick()
            request.assert_not_called()
            clear.assert_not_called()
        self.assertIs(self.window.matrix.item(row,1),old)
        # Exercise the actual delegate and its clock drawing.
        self.window.show()
        QTest.qWait(30)
        self.assertFalse(self.window.grab().isNull())

    def test_cron_marker_opens_latest_pod_and_cell_background_opens_cronjob(self):
        row,scope = self.cron()
        job = dict(metadata=dict(name='run',uid='j',creationTimestamp='2026-11-22T01:00:00Z',
                                  ownerReferences=[dict(kind='CronJob',uid='cron-1',controller=True)]),
                   status=dict(conditions=[dict(type='Complete',status='True')]))
        pod = dict(metadata=dict(name='batch',uid='p2',ownerReferences=[dict(kind='Job',uid='j',controller=True)]),status={'phase':'Succeeded'})
        scope['jobs'],scope['pods'] = [job],[pod]
        self.window.render_matrix()
        rect = self.window.matrix.content_rect(self.window.matrix.visualItemRect(self.window.matrix.item(row,1)),row)
        self.assertEqual(len(self.window.matrix.cell_visuals(row,1)), 1)
        self.assertEqual(self.window.matrix.cell_visuals(row,1)[0].status, 'Completed')
        self.assertFalse(self.window.matrix.cell_visuals(row,1)[0].recent)
        areas = pod_rects(rect,1)
        with patch.object(self.window,'open_resource') as opened:
            self.window._matrix_click(row,1,areas[0].center())
            self.assertEqual(opened.call_args.args[1], 'pod')
            self.assertEqual(opened.call_args.kwargs['expected_uid'], 'p2')
            self.window._matrix_click(row, 1, rect.topLeft()+QPoint(2, 2))
            self.assertEqual(opened.call_args.args[1], 'cronjob')

    def test_timezone_preferences_reject_wrong_shape_without_credential_changes(self):
        self.assertEqual(validate_preferences({'controller_timezones':[]})['controller_timezones'],{})
        result = validate_preferences({'controller_timezones':{'c':'UTC','bad':9}})
        self.assertEqual(result['controller_timezones'],{'c':'UTC'})
        self.assertEqual(validate_preferences({'controller_timezones': {'c':'Europe/Samara'}})['controller_timezones'], {})

    def test_only_connection_offset_controls_the_schedule(self):
        row, scope = self.cron()
        scope['cron_jobs'][0]['spec']['timeZone'] = 'Europe/Samara'
        self.window.preferences['controller_timezones'] = {'c':'UTC+04:00'}
        self.window.schedules.invalidate()
        self.window.render_matrix()
        for _ in range(40):
            QTest.qWait(10)
            if not self.window.schedules.busy: break
        self.window.render_matrix()
        result = self.window.matrix.cell_visuals(row,1)[0].schedule
        self.assertEqual(result.timezone_key, 'UTC+04:00')
        self.assertEqual(result.timezone_source, 'connection setting')
