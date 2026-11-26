"""Explicit frozen-runtime health check; ordinary GUI startup is untouched."""
import sys

if sys.argv[1:]==['--verify-schedule-runtime']:
    try:
        from datetime import datetime,timezone
        from quaber.schedule import ScheduleEngine,ScheduleInput,load_zone
        for expression,offset,base,expected in (
            ('0 9 * * *','UTC+04:00','2026-11-25T04:00:00Z','2026-11-25T05:00:00Z'),
            ('30 1 * * *','UTC-03:30','2026-11-25T03:00:00Z','2026-11-25T05:00:00Z'),
            ('0 0 29 2 *','UTC','2026-11-25T00:00:00Z','2028-02-29T00:00:00Z')):
            result=ScheduleEngine().calculate(ScheduleInput(expression,offset),datetime.fromisoformat(base))
            if result.next_due_utc!=datetime.fromisoformat(expected).astimezone(timezone.utc): raise RuntimeError('Frozen calendar check failed.')
    except Exception:
        raise SystemExit(1)
    raise SystemExit(0)
