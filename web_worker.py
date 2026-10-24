"""MTS catalog-only updater, callable from scheduler or Telegram.

No product-detail HTTP requests. Web prices remain separate from manual offers.
"""
import argparse
import os
import time
import traceback
from dataclasses import dataclass

CATEGORIES = ('smartphones', 'tablets', 'watches', 'headphones', 'laptops')
LOCK_KEY = 77504431

@dataclass(frozen=True)
class StoredOffer:
    shop: str
    category: str
    sku: str
    name: str
    url: str
    price: object
    currency: str
    availability: str


def run(categories, max_pages=50, report=None):
    """Return 0 on success, 2 on validation/error, 3 when already running."""
    import psycopg
    from scrapers.core import PublicFetcher
    from scrapers.mts_catalog import crawl_mts_catalog
    from scrapers.shops import SHOPS
    from scrapers.storage import init, save_observations

    dsn = os.getenv('DATABASE_URL')
    if not dsn:
        raise RuntimeError('DATABASE_URL is not configured')
    shop = SHOPS['mts']
    delay = float(os.getenv('SCRAPER_DELAY_SECONDS', '2'))
    fetcher = PublicFetcher(shop.domain, delay=delay,
                            user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    def emit(message):
        print(message, flush=True)
        if report:
            report(message)

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as lock_conn:
        locked = lock_conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,)).fetchone()[0]
        if not locked:
            emit('MTS: другой сбор уже выполняется')
            return 3
        try:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                init(conn)
                failed = False
                snapshots = {}
                for category in categories:
                    if category not in shop.categories:
                        emit(f'MTS/{category}: категория не настроена')
                        failed = True
                        continue
                    emit(f'START mts/{category}')
                    start = time.monotonic()
                    try:
                        result = crawl_mts_catalog(fetcher, shop.categories[category], category, max_pages=max_pages)
                        emit(f'CRAWL mts/{category}: pages={result.pages} cards={result.cards} '
                             f'priced={len(result.products)} issues={len(result.issues)} '
                             f'incomplete={result.incomplete} elapsed={time.monotonic()-start:.1f}s')
                        if result.incomplete or result.issues or not result.products:
                            emit(f'NOT SAVED mts/{category}: incomplete/invalid crawl')
                            failed = True
                            continue
                        products = [StoredOffer('mts', category, p.sku, p.name, p.url,
                                                p.price, p.currency, p.availability)
                                    for p in result.products]
                        snapshots[category] = result.products
                        emit(f'VALIDATED mts/{category}: {len(products)} catalogue prices')
                    except Exception:
                        traceback.print_exc()
                        emit(f'FAILED mts/{category}: previous observations preserved')
                        failed = True
                if failed:
                    emit('NOT SAVED: failed/incomplete category; supplier snapshot untouched')
                    return 2
                # A single category test is intentionally NOT a supplier replacement.
                if set(categories) != set(CATEGORIES):
                    emit('NOT SAVED: supplier refresh requires all five categories')
                    return 2
                from web_supplier_snapshot import replace_shop_supplier
                summary = replace_shop_supplier(snapshots)
                emit(f'SAVED supplier shop.mts.by: {summary["saved"]} offers, id={summary["supplier_id"]}')
                # Existing web history is secondary; failure must not roll back supplier snapshot.
                for category in categories:
                    try:
                        products = [StoredOffer('mts', category, p.sku, p.name, p.url,
                                                p.price, p.currency, p.availability)
                                    for p in snapshots[category]]
                        save_observations(conn, 'mts', category, products)
                    except Exception:
                        traceback.print_exc()
                        emit(f'WARN mts/{category}: secondary web history not updated')
                return 0
        finally:
            lock_conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--category', choices=(*CATEGORIES, 'all'), default='all')
    parser.add_argument('--max-pages', type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100:
        parser.error('max-pages must be 1..100')
    return run(CATEGORIES if args.category == 'all' else (args.category,), args.max_pages)


if __name__ == '__main__':
    raise SystemExit(main())
