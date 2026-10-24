"""Owner-only A1 manual refresh and 07:20 and 15:20 Minsk scheduler. Does not modify MTS."""
import asyncio
import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
MINSK = ZoneInfo('Europe/Minsk')
LOCK_KEY = 77504434


def collect_and_save(report=None):
    """Blocking operation: global DB lock, full crawl, retry inconsistent sections, atomic commit."""
    import psycopg
    from a1_catalog_dryrun import SECTIONS, crawl
    from a1_supplier_worker import prepare_snapshot, save_supplier
    dsn = os.environ['DATABASE_URL']
    emit = report or (lambda s: None)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as lock_conn:
        if not lock_conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,)).fetchone()[0]:
            emit('Другой сбор A1 уже выполняется')
            return 3, 'Другой сбор A1 уже выполняется'
        try:
            results = {}
            for section in SECTIONS:
                emit('START A1 '+section)
                try:
                    results[section] = crawl(section, 100, 25, 1.0)
                except Exception as exc:
                    results[section] = {'section': section, 'issues': [f'{type(exc).__name__}: {exc}'], 'expected_items':None, 'found':0, 'products':[]}
                result = results[section]
                # A moving catalogue can cause a partial snapshot: repeat only the affected category.
                if result.get('issues') or result.get('expected_items') is None or result.get('found') != result.get('expected_items'):
                    emit(f'RETRY A1 {section}: found={result.get("found")} expected={result.get("expected_items")}')
                    try:
                        results[section] = crawl(section, 100, 25, 1.5)
                    except Exception as exc:
                        results[section] = {'section':section,'issues':[f'{type(exc).__name__}: {exc}'], 'expected_items':None,'found':0,'products':[]}
                r=results[section]
                emit(f'DONE A1 {section}: found={r.get("found")} priced={r.get("priced")} expected={r.get("expected_items")}')
            snapshot=prepare_snapshot(results)
            if not snapshot['ready_to_commit']:
                detail='A1 не сохранён: '+ '; '.join(snapshot['errors'][:4])
                emit(detail)
                return 2,detail
            if os.getenv('A1_SUPPLIER_COMMIT','0')!='1':
                return 4, f'A1 проверен: {snapshot["unique_priced"]} товаров. Режим просмотра; нет записи в БД.'
            saved=save_supplier(snapshot)
            detail=f'✅ shop.a1.by обновлён: {saved["saved"]} товаров (supplier_id={saved["supplier_id"]})'
            emit(detail)
            return 0,detail
        finally:
            lock_conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))


async def manual_command(update, context):
    owner = os.getenv('OWNER_TELEGRAM_ID','').strip()
    if not owner or str(update.effective_user.id) != owner:
        await update.effective_message.reply_text('🔒 Только владелец может запускать сбор A1.')
        return
    if os.getenv('A1_MANUAL_ENABLED','0')!='1':
        await update.effective_message.reply_text('A1_MANUAL_ENABLED=0. Ручной запуск выключен.')
        return
    state=context.application.bot_data.setdefault('a1_scraper', {'running':False})
    if state['running']:
        await update.effective_message.reply_text('⏳ A1 уже собирается.')
        return
    state['running']=True
    chat_id=update.effective_chat.id
    await update.effective_message.reply_text('🔄 Запускаю A1: 7 категорий; результат пришлю сюда.')
    async def perform():
        try:
            code,detail=await asyncio.to_thread(collect_and_save)
            await context.bot.send_message(chat_id,detail[:3900])
        except Exception:
            log.exception('A1 manual update failure')
            await context.bot.send_message(chat_id,'❌ Ошибка A1; смотри Deploy Logs. Старый прайс не заменён, если транзакция не завершилась.')
        finally:
            state['running']=False
    context.application.create_task(perform(),name='a1_manual_update')


SLOTS = (("morning", time(7, 20)), ("afternoon", time(15, 20)))


def due_slot(now=None):
    local = (now or datetime.now(MINSK)).astimezone(MINSK)
    for slot, starts in reversed(SLOTS):
        scheduled = datetime.combine(local.date(), starts, MINSK)
        seconds = (local - scheduled).total_seconds()
        if 0 <= seconds <= int(os.getenv('A1_SCHEDULE_GRACE_MINUTES', '90')) * 60:
            return local.date(), slot
    return None


def claim_slot(day, slot):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS a1_twice_daily_runs (
                local_day DATE NOT NULL, run_slot TEXT NOT NULL,
                status TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ, detail TEXT,
                PRIMARY KEY(local_day,run_slot))''')
            cur.execute('''INSERT INTO a1_twice_daily_runs(local_day,run_slot,status)
                VALUES (%s,%s,'running') ON CONFLICT (local_day,run_slot) DO NOTHING
                RETURNING local_day''', (day,slot))
            return cur.fetchone() is not None


def finish_slot(day,slot,code,detail):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=10) as conn:
        conn.execute('''UPDATE a1_twice_daily_runs SET status=%s,finished_at=now(),detail=%s
            WHERE local_day=%s AND run_slot=%s''',
            ('success' if code==0 else 'failed',detail[:2000],day,slot))


async def daily_loop():
    log.info('A1 scheduler enabled: 07:20 and 15:20 Europe/Minsk')
    while True:
        try:
            due=due_slot()
            if due:
                day,slot=due
                if await asyncio.to_thread(claim_slot,day,slot):
                    code,detail=await asyncio.to_thread(collect_and_save)
                    await asyncio.to_thread(finish_slot,day,slot,code,detail)
                    log.info('A1 scheduled %s result: %s',slot,detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('A1 scheduler failure')
        await asyncio.sleep(30)


async def post_init(application):
    if os.getenv('A1_DAILY_ENABLED','0')=='1':
        application.create_task(daily_loop(),name='a1_twice_daily_scheduler')
    else:
        log.info('A1 scheduled scraper disabled')


def register(application):
    from telegram.ext import CommandHandler
    application.add_handler(CommandHandler('a1update',manual_command))
