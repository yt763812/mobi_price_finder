"""Railway worker: read-only catalog sync + administrator review queue. NO publication."""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import requests

from . import comparison, parser, exporter, store

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)


def fetch_positions():
    client_id = os.environ['ONLINER_CLIENT_ID']
    secret = os.environ['ONLINER_CLIENT_SECRET']
    session = requests.Session()
    session.headers['Accept'] = 'application/json'
    token = exporter.request_json(session, 'POST', exporter.TOKEN_URL,
                                  auth=(client_id, secret), data={'grant_type':'client_credentials'})
    access_token = token.get('access_token')
    if not access_token: raise RuntimeError('Onliner OAuth: нет access_token')
    session.headers['Authorization'] = 'Bearer ' + access_token
    items = exporter.get_all_positions(session)
    if not items or not all(isinstance(v,dict) for v in items):
        raise RuntimeError('Onliner /positions: пустой или некорректный ответ')
    return items


def onliner_table(items):
    result = [['Раздел','Бренд','Модель','productId']]
    for item in items:
        if item.get('productId') and item.get('model'):
            result.append([str(item.get('category') or ''),str(item.get('vendor') or ''),
                           str(item['model']),str(item['productId'])])
    if len(result) <= 1: raise RuntimeError('Onliner: нет записей с моделью и productId')
    return result


def shop_table(rows):
    return [['Категория','Модель (карточка)','Цвет','Цена, BYN','Тип цены','Дата выгрузки'],*rows]


def notify(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token: return
    for telegram_id in store.approval_ids():
        try:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              json={'chat_id':telegram_id,'text':message},timeout=15)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning('Notification to admin %s failed: %s', telegram_id, str(exc)[:180])


async def process(job_id):
    store.stage(job_id,'fetch_onliner')
    online = await asyncio.to_thread(fetch_positions)
    log.info('Job %s: Onliner offers %s',job_id,len(online))
    store.stage(job_id,'parse_mobistore')
    catalog = await parser.collect_catalog(persist=False)
    log.info('Job %s: parser %s products, %s issues',job_id,catalog['products_processed'],catalog['problems'])
    # Fail closed on incomplete snapshots; no administrator queue for unreliable crawls.
    if catalog['products_priced'] == 0 or catalog['products_processed'] != catalog['products_priced']:
        raise RuntimeError('Парсинг неполный: количество обработанных карточек не совпало с количеством карточек с ценой')
    if not catalog['rows']:
        raise RuntimeError('Парсер вернул пустой каталог')
    max_problem_rate = float(os.getenv('ONLINER_MAX_PARSER_PROBLEM_RATE','0.10'))
    if catalog['problems'] / catalog['products_priced'] > max_problem_rate:
        raise RuntimeError('Слишком много проблем при парсинге; результат не отправлен на согласование')
    # Exclude fallback prices without proven variant/color from approval candidates.
    valid_rows = [r for r in catalog['rows'] if len(r) >= 5 and not any(
        marker in str(r[4]).lower() for marker in ('цвет не проверен','цвет не определён'))]
    if not valid_rows:
        raise RuntimeError('Нет достаточно проверенных позиций MobiStore')
    store.stage(job_id,'compare')
    aliases = comparison.get_aliases(None)
    shops, positions, excluded = comparison.build(shop_table(valid_rows),onliner_table(online),aliases)
    sheets,stats,_ = comparison.make_report(shops,positions,excluded,{}, {})
    store.stage(job_id,'save_results')
    summary = store.complete(job_id,sheets,stats,{
        key:catalog[key] for key in ('products_discovered','products_priced','products_processed','problems')})
    log.info('Job %s completed: %s',job_id,summary)
    notify(f'✅ Сверка Onlíner #{job_id} завершена.\n'
           f'Кандидатов: {summary.get("candidate_rows",0)}. Требуют проверки: {summary.get("uncertain_rows",0)}.\n'
           'Открой /onliner для просмотра и согласования. Публикации не было.')


def reap_stale_jobs():
    # Worker is single-service; stale running job from a crashed worker is marked failed,
    # not automatically replayed to avoid accidentally publishing partial observations.
    with store.connect() as conn:
        rows = conn.execute("""UPDATE onliner_jobs SET state='failed',stage='worker_interrupted',
            finished_at=now(),error='Worker restarted; rerun manually'
            WHERE state='running' RETURNING id""").fetchall()
    for row in rows:
        notify(f'⚠️ Сверка Onlíner #{row["id"]} прервана при перезапуске Worker. Запусти её заново через /onliner.')


def main():
    store.init()
    if not store.launch_ids() or not store.approval_ids():
        raise RuntimeError('Set ONLINER_LAUNCH_ADMIN_IDS and ONLINER_APPROVAL_ADMIN_IDS')
    reap_stale_jobs()
    log.info('Onliner worker started. Manual jobs only; publication disabled.')
    while True:
        job = store.claim()
        if not job:
            time.sleep(5)
            continue
        try:
            asyncio.run(process(job['id']))
        except Exception as exc:
            log.exception('Job %s failed',job['id'])
            store.fail(job['id'],str(exc))
            notify(f'❌ Сверка Onlíner #{job["id"]} завершилась ошибкой: {str(exc)[:400]}. Проверь Worker Logs.')


if __name__ == '__main__':
    main()
