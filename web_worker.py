"""One-shot Railway cron job for MTS; independent of Telegram bot.

Runs sequentially; database locks prevent overlapping jobs. Does not create
supplier rows or modify manual offers. Requires DATABASE_URL.
"""
import argparse
import os
import sys
import time
import traceback


def run(categories, max_pages):
    import psycopg
    from scrapers.core import PublicFetcher
    from scrapers.mts_crawl import crawl_mts
    from scrapers.shops import SHOPS
    from scrapers.storage import init, save_observations

    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise RuntimeError('DATABASE_URL is required; configure it on the scraper service')
    shop = SHOPS['mts']
    fetcher = PublicFetcher(shop.domain, delay=float(os.getenv('SCRAPER_DELAY_SECONDS', '2')),
                            user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    # Advisory lock belongs to a single open connection for the entire job.
    with psycopg.connect(dsn, autocommit=True) as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)', (77504431,))
            locked = cur.fetchone()[0]
        if not locked:
            print('MTS worker already running: skip overlapping execution', flush=True)
            return 0
        try:
            with psycopg.connect(dsn) as conn:
                init(conn)
                for category in categories:
                    print(f'START mts/{category}', flush=True)
                    started = time.monotonic()
                    try:
                        result = crawl_mts(fetcher, shop.categories[category], category, max_pages=max_pages)
                        print(f'CRAWL mts/{category}: pages={result.pages} cards={result.cards} '
                              f'priced={len(result.products)} issues={len(result.issues)} '
                              f'incomplete={result.incomplete} elapsed={time.monotonic()-started:.1f}s', flush=True)
                        if result.incomplete or result.issues or not result.products:
                            print('NOT SAVED: incomplete run, missing prices, or no products', flush=True)
                            return 2
                        save_observations(conn, shop.slug, category, result.products)
                        print(f'SAVED mts/{category}: {len(result.products)} public prices', flush=True)
                    except Exception:
                        print(f'FAILED mts/{category}: previous data remain unchanged', flush=True)
                        traceback.print_exc()
                        return 2
        finally:
            with lock_conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', (77504431,))
    return 0


def main():
    parser = argparse.ArgumentParser(description='One-shot public MTS price scraper for Railway cron')
    parser.add_argument('--category', choices=['smartphones','tablets','watches','headphones','all'], default='all')
    parser.add_argument('--max-pages', type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100:
        parser.error('--max-pages must be between 1 and 100')
    categories = ['smartphones','tablets','watches','headphones'] if args.category == 'all' else [args.category]
    return run(categories, args.max_pages)


if __name__ == '__main__':
    sys.exit(main())
