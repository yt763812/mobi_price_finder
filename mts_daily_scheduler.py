"""Daily MTS updater attached to the existing Telegram bot.

Disabled unless MTS_DAILY_ENABLED=1. Schedule: 07:00 Europe/Minsk,
with catch-up after restart. Once per Minsk calendar day across replicas.
"""
import asyncio
import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
MINSK = ZoneInfo('Europe/Minsk')
DDL = '''CREATE TABLE IF NOT EXISTS mts_daily_runs (
  local_day DATE PRIMARY KEY,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  detail TEXT
)'''


def due_day(now=None):
    """Return Minsk date after 07:00, otherwise None."""
    local = (now or datetime.now(MINSK)).astimezone(MINSK)
    return local.date() if local.time() >= time(7, 0) else None


def claim_day(day):
    """Atomically allow only one bot instance to run on each calendar date."""
    import psycopg
    dsn = os.environ['DATABASE_URL']
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute('''INSERT INTO mts_daily_runs(local_day,status)
                VALUES (%s,'running') ON CONFLICT (local_day) DO NOTHING
                RETURNING local_day''', (day,))
            return cur.fetchone() is not None


def finish_day(day, success, detail):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('''UPDATE mts_daily_runs SET status=%s, finished_at=now(), detail=%s
                           WHERE local_day=%s''',
                        ('success' if success else 'failed', str(detail)[:2000], day))


def run_worker(day):
    """Blocking HTTP + SQL runs in worker thread, never in Telegram event loop."""
    from web_worker import run
    categories = ['smartphones', 'tablets', 'watches', 'headphones']
    result = 1
    try:
        result = run(categories, int(os.getenv('MTS_MAX_PAGES', '50')))
        return result
    finally:
        # If container stops mid-crawl the row remains 'running': no duplicate run.
        try:
            finish_day(day, result == 0, f'exit_code={result}')
        except Exception:
            logger.exception('Failed to record MTS daily result')


async def schedule_loop():
    logger.info('MTS daily scheduler enabled: 07:00 Europe/Minsk')
    while True:
        try:
            day = due_day()
            if day and await asyncio.to_thread(claim_day, day):
                logger.info('MTS scheduled crawl started: %s', day)
                result = await asyncio.to_thread(run_worker, day)
                logger.info('MTS scheduled crawl finished: exit=%s', result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('MTS scheduler error; Telegram bot continues running')
        await asyncio.sleep(300)


async def post_init(application):
    if os.getenv('MTS_DAILY_ENABLED', '0').strip() != '1':
        logger.info('MTS daily scraper disabled (set MTS_DAILY_ENABLED=1 to enable)')
        return
    application.create_task(schedule_loop(), name='mts_daily_scheduler')
