"""Daily MTS updater attached to the existing Telegram bot.

Disabled unless MTS_DAILY_ENABLED=1. Schedule: 07:00 and 15:00 Europe/Minsk,
with catch-up after restart. Once per Minsk calendar day across replicas.
"""
import asyncio
import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
MINSK = ZoneInfo('Europe/Minsk')
DDL = '''CREATE TABLE IF NOT EXISTS mts_twice_daily_runs (
  local_day DATE NOT NULL,
  run_slot TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  detail TEXT,
  PRIMARY KEY(local_day,run_slot)
)'''
SLOTS = (("morning", time(7, 0)), ("afternoon", time(15, 0)))


def due_slot(now=None):
    local = (now or datetime.now(MINSK)).astimezone(MINSK)
    for slot, starts in reversed(SLOTS):
        scheduled = datetime.combine(local.date(), starts, MINSK)
        seconds = (local - scheduled).total_seconds()
        if 0 <= seconds <= int(os.getenv('MTS_SCHEDULE_GRACE_MINUTES', '90')) * 60:
            return local.date(), slot
    return None


def claim_slot(day, slot):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute('''INSERT INTO mts_twice_daily_runs(local_day,run_slot,status)
                VALUES (%s,%s,'running') ON CONFLICT (local_day,run_slot) DO NOTHING
                RETURNING local_day''', (day, slot))
            return cur.fetchone() is not None


def finish_slot(day, slot, success, detail):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        conn.execute('''UPDATE mts_twice_daily_runs
            SET status=%s,finished_at=now(),detail=%s
            WHERE local_day=%s AND run_slot=%s''',
            ('success' if success else 'failed', str(detail)[:2000], day, slot))


def run_worker(day, slot):
    from web_worker import run, CATEGORIES
    result = 1
    try:
        result = run(list(CATEGORIES), int(os.getenv('MTS_MAX_PAGES', '100')))
        return result
    finally:
        try:
            finish_slot(day, slot, result == 0, f'exit_code={result}')
        except Exception:
            logger.exception('Failed to record MTS scheduled result')


async def schedule_loop():
    logger.info('MTS scheduler enabled: 07:00 and 15:00 Europe/Minsk')
    while True:
        try:
            due = due_slot()
            if due:
                day, slot = due
                if await asyncio.to_thread(claim_slot, day, slot):
                    logger.info('MTS scheduled crawl started: %s %s', day, slot)
                    result = await asyncio.to_thread(run_worker, day, slot)
                    logger.info('MTS scheduled crawl finished: exit=%s', result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('MTS scheduler error; bot continues running')
        await asyncio.sleep(30)


async def post_init(application):
    if os.getenv('MTS_DAILY_ENABLED', '0').strip() != '1':
        logger.info('MTS daily scraper disabled (set MTS_DAILY_ENABLED=1 to enable)')
        return
    application.create_task(schedule_loop(), name='mts_daily_scheduler')
