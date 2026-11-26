"""Bounded five-field schedules using explicit, constant UTC offsets only."""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta, timezone
from functools import lru_cache
import re
import time

UTC = timezone.utc
MACROS = {'@yearly':'0 0 1 1 *', '@annually':'0 0 1 1 *', '@monthly':'0 0 1 * *',
          '@weekly':'0 0 * * 0', '@daily':'0 0 * * *', '@midnight':'0 0 * * *', '@hourly':'0 * * * *'}
MONTHS = dict(zip('jan feb mar apr may jun jul aug sep oct nov dec'.split(), range(1,13)))
DAYS = dict(zip('sun mon tue wed thu fri sat'.split(), range(7)))


@lru_cache(maxsize=128)
def load_zone(key):
    if key == 'UTC':
        return UTC
    offset = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', key) if isinstance(key, str) else None
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        total = (hours * 60 + minutes) * (1 if offset[1] == '+' else -1)
        if minutes not in (0, 15, 30, 45) or not -720 <= total <= 840:
            raise ValueError('invalid UTC offset')
        return timezone(timedelta(minutes=total), name=key)
    raise ValueError('invalid UTC offset')


def _field(text, low, high, names):
    values, star = set(), False
    def number(part):
        value = names.get(part.lower())
        if value is None:
            if not re.fullmatch(r'[0-9]{1,3}',part): raise ValueError('unsupported schedule')
            value = int(part)
        if not low <= value <= high: raise ValueError('schedule value out of range')
        return value
    for part in text.split(','):
        pieces = part.split('/')
        if len(pieces)>2: raise ValueError('unsupported schedule')
        step = int(pieces[1]) if len(pieces)==2 and re.fullmatch(r'[0-9]{1,3}',pieces[1]) else 1
        if len(pieces)==2 and not re.fullmatch(r'[0-9]{1,3}',pieces[1]): raise ValueError('unsupported step')
        if step == 0: raise ValueError('zero schedule step')
        span = pieces[0]
        if span in ('*','?'):
            start,end = low,high
            star |= step == 1
        else:
            endpoints = span.split('-')
            if len(endpoints)>2: raise ValueError('unsupported range')
            start = number(endpoints[0])
            end = number(endpoints[1]) if len(endpoints)==2 else high if len(pieces)==2 else start
            if start>end: raise ValueError('backwards range')
        values.update(range(start,end+1,step))
    return ','.join(map(str,sorted(values))), star


@lru_cache(maxsize=512)
def normalize(schedule):
    if not isinstance(schedule,str) or len(schedule)>512: raise ValueError('unsupported schedule')
    fields = MACROS.get(schedule,schedule).split()
    if len(fields)!=5: raise ValueError('expected five schedule fields')
    parsed = [_field(field,*bounds) for field,bounds in zip(fields,
        ((0,59,{}),(0,23,{}),(1,31,{}),(1,12,MONTHS),(0,6,DAYS)))]
    # */N (N>1) does not retain the star flag in DOM/DOW matching.
    return ' '.join('*' if i in (2,4) and star else value
                    for i,(value,star) in enumerate(parsed))


@dataclass(frozen=True,slots=True)
class ScheduleInput:
    schedule: str
    utc_offset: str | None = None
    suspend: bool = False
    concurrency_policy: str = 'Allow'
    starting_deadline_seconds: int | None = None
    active: bool = False
    offset_source: str = 'connection setting'


@dataclass(frozen=True,slots=True)
class ScheduleResult:
    next_due_utc: datetime | None
    timezone_key: str | None
    timezone_source: str
    state: str
    reason: str = ''

    def tooltip(self, now, *, confirmed_snapshot=False, stale=False):
        if self.next_due_utc is None:
            text = self.reason or self.state
        elif now >= self.next_due_utc:
            late = (now-self.next_due_utc).total_seconds()
            text = 'Run not confirmed' if confirmed_snapshot and late>=60 else 'Awaiting start'
            if self.reason: text += ' · '+self.reason
        else:
            local = self.next_due_utc.astimezone(load_zone(self.timezone_key))
            text = (f'Next run: {local:%Y-%m-%d %H:%M:%S}\n'
                    f'{self.timezone_key} · ' + relative_interval((self.next_due_utc-now).total_seconds()))
            if self.reason: text += '\n'+self.reason
        return text + ('\nSnapshot is stale; schedule is an estimate.' if stale else '')


def relative_interval(seconds):
    if seconds < 60:
        return 'менее чем через минуту'
    import math
    days, minutes = divmod(math.ceil(seconds / 60), 1440)
    hours, minutes = divmod(minutes, 60)
    def unit(value, forms):
        ending = 2 if 11 <= value % 100 <= 14 else 0 if value % 10 == 1 else 1 if 2 <= value % 10 <= 4 else 2
        return f'{value} {forms[ending]}'
    parts = [unit(value, forms) for value, forms in (
        (days, ('день', 'дня', 'дней')), (hours, ('час', 'часа', 'часов')),
        (minutes, ('минута', 'минуты', 'минут'))) if value]
    return 'через ' + ' '.join(parts)


class ScheduleEngine:
    def calculate(self, spec, now, *, budget=.25):
        if now.tzinfo is None: raise ValueError('UTC now must be timezone-aware')
        now = now.astimezone(UTC)
        key = spec.utc_offset
        source = spec.offset_source if key else 'unknown'
        def unavailable(reason): return ScheduleResult(None,key,source,'Unavailable','Next run unavailable: '+reason)
        if spec.suspend: return ScheduleResult(None,key,source,'Suspended','Schedule suspended')
        if not key: return unavailable('UTC-смещение не указано.\nНастройки → двойной клик по кластеру → Часовой пояс.')
        try:
            zone, expression = load_zone(key), normalize(spec.schedule)
        except ValueError as exc:
            return unavailable('invalid UTC offset' if 'offset' in str(exc) else 'unsupported schedule')
        wall = now.astimezone(zone)
        fields = expression.split()
        bounds = ((0,59),(0,23),(1,31),(1,12),(0,6))
        minutes, hours, days, months, weekdays = [set(range(low,high+1)) if field == '*'
            else {int(value) for value in field.split(',')} for field,(low,high) in zip(fields,bounds)]
        times = sorted(hour*60+minute for hour in hours for minute in minutes)
        day, deadline = wall.date(), time.monotonic()+budget
        # At most six calendar years of days, never millions of minute steps.
        for _ in range(366*6):
            if time.monotonic()>deadline: return unavailable('calculation budget exceeded')
            if day.year > wall.year+5: break
            dom, dow = day.day in days, (day.weekday()+1)%7 in weekdays
            matches = dom and dow if fields[2] == '*' or fields[4] == '*' else dom or dow
            if day.month in months and matches:
                index = bisect_right(times, wall.hour*60+wall.minute) if day == wall.date() else 0
                if index < len(times):
                    hour, minute = divmod(times[index], 60)
                    instant = datetime.combine(day, wall_time(hour,minute), zone).astimezone(UTC)
                    reason = 'Active Job: Forbid may delay this run.' if spec.active and spec.concurrency_policy=='Forbid' else ''
                    return ScheduleResult(instant,key,source,'Scheduled',reason)
            try:
                day += timedelta(days=1)
            except OverflowError:
                break
        return unavailable('no matching time within five years')


def last_run(cron_uid,jobs):
    """UID ownership and scheduled timestamp, never name order or failed count."""
    if not isinstance(cron_uid,str) or not cron_uid: return None
    owned = [job for job in jobs if any(o.get('controller') and o.get('kind')=='CronJob'
             and o.get('uid')==cron_uid for o in job.get('metadata',{}).get('ownerReferences',[]))]
    def started(job):
        metadata = job.get('metadata',{})
        return metadata.get('annotations',{}).get('batch.kubernetes.io/cronjob-scheduled-timestamp') or metadata.get('creationTimestamp','')
    def timestamp(job):
        try: return datetime.fromisoformat(started(job).replace('Z','+00:00')).astimezone(UTC)
        except (ValueError,TypeError): return datetime.min.replace(tzinfo=UTC)
    if not owned: return None
    job = max(owned,key=timestamp)
    if timestamp(job)==datetime.min.replace(tzinfo=UTC): return None
    status = job.get('status',{})
    conditions = {c.get('type') for c in status.get('conditions',[]) if c.get('status')=='True'}
    result = 'Succeeded' if 'Complete' in conditions else 'Failed' if 'Failed' in conditions else 'Running' if status.get('active') else 'Pending'
    return job,result,started(job),('scheduled timestamp' if job.get('metadata',{}).get('annotations',{}).get('batch.kubernetes.io/cronjob-scheduled-timestamp') else 'creation time; scheduled time unknown')
