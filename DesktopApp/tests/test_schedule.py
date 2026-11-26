"""Independent fixed-offset calendar expectations and DOM/DOW semantics."""
from datetime import datetime,timezone
import unittest
from quaber.schedule import ScheduleEngine,ScheduleInput,normalize,load_zone,last_run


class ScheduleTests(unittest.TestCase):
    def due(self,expression,now,expected,zone='UTC'):
        result = ScheduleEngine().calculate(ScheduleInput(expression,zone),datetime.fromisoformat(now))
        self.assertEqual(result.next_due_utc,datetime.fromisoformat(expected).astimezone(timezone.utc),result)

    def test_offset_is_constant_in_winter_and_summer(self):
        self.due('0 9 * * *','2026-01-01T04:00:00Z','2026-01-01T05:00:00Z','UTC+04:00')
        self.due('0 9 * * *','2026-07-01T04:00:00Z','2026-07-01T05:00:00Z','UTC+04:00')

    def test_fractional_offsets_and_local_day_boundary(self):
        self.due('30 1 * * *','2026-11-25T03:00:00Z','2026-11-25T05:00:00Z','UTC-03:30')
        self.due('0 0 * * *','2026-11-25T20:00:00Z','2026-11-26T18:15:00Z','UTC+05:45')
        self.due('10,40 9-10 * * *','2026-11-25T09:40:01Z','2026-11-25T10:10:00Z')

    def test_strictly_after_and_robfig_star_semantics(self):
        self.due('0 0 * * mon','2026-11-20T00:00:00+00:00','2026-11-23T00:00:00+00:00','UTC')
        self.due('0 0 */2 * mon','2026-11-20T00:00:00+00:00','2026-11-21T00:00:00+00:00','UTC')
        self.due('0-0 1-1 * * *','2026-11-20T01:00:00+00:00','2026-11-21T01:00:00+00:00','UTC')
        self.due('5/15 * * * *','2026-11-20T00:05:00+00:00','2026-11-20T00:20:00+00:00','UTC')
        self.due('@weekly','2026-11-20T00:00:00+00:00','2026-11-22T00:00:00+00:00','UTC')
        self.due('0 0 29 feb ?','2026-11-20T00:00:00+00:00','2028-02-29T00:00:00+00:00','UTC')

    def test_invalid_forms_are_bounded_and_readonly(self):
        for expression in ('* * * * * *','* * * * 7','0 0 L * *','@every 1h','0 0 * * mon#2','9-2 * * * *','*/0 * * * *'):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError): normalize(expression)
        result = ScheduleEngine().calculate(ScheduleInput('0 0 31 feb *','UTC'),datetime.now(timezone.utc))
        self.assertIsNone(result.next_due_utc)
        self.assertIn('five years',result.reason)

    def test_unknown_zone_never_uses_machine_timezone(self):
        now = datetime.now(timezone.utc)
        result = ScheduleEngine().calculate(ScheduleInput('* * * * *'),now)
        self.assertIn('UTC-смещение не указано',result.reason)
        for key in ('../UTC','/etc/passwd','C:/Windows','Not/AZone'):
            with self.assertRaises(ValueError): load_zone(key)
        result = ScheduleEngine().calculate(ScheduleInput('* * * * *',utc_offset='UTC'),now)
        self.assertEqual(result.timezone_source,'connection setting')
        self.assertEqual(ScheduleEngine().calculate(ScheduleInput('* * * * *',suspend=True),now).state,'Suspended')

    def test_deadline_is_not_fake_failure_and_last_job_uses_uid(self):
        now = datetime(2026,11,20,tzinfo=timezone.utc)
        result = ScheduleEngine().calculate(ScheduleInput('* * * * *','UTC'),now)
        self.assertIn('Awaiting start',result.tooltip(datetime(2026,11,20,0,1,tzinfo=timezone.utc)))
        self.assertIn('Run not confirmed',result.tooltip(datetime(2026,11,20,0,2,tzinfo=timezone.utc),confirmed_snapshot=True))
        jobs = [dict(metadata=dict(name='z-old',uid='j1',creationTimestamp='2026-11-20T00:00:00Z',ownerReferences=[dict(controller=True,kind='CronJob',uid='c')]),status={'conditions':[dict(type='Complete',status='True')]}),
                dict(metadata=dict(name='a-new',uid='j2',creationTimestamp='2026-11-20T00:01:00Z',ownerReferences=[dict(controller=True,kind='CronJob',uid='c')]),status={'failed':1,'active':1}),
                dict(metadata=dict(name='foreign',uid='j3',creationTimestamp='2026-11-20T00:02:00Z',ownerReferences=[dict(controller=True,kind='CronJob',uid='other')]),status={})]
        self.assertEqual(last_run('c',jobs)[0]['metadata']['uid'],'j2')
        self.assertEqual(last_run('c',jobs)[1],'Running')
