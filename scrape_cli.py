"""Public web prices. MTS --dry-run works without DATABASE_URL.

Examples:
  python scrape_cli.py --shop mts --category smartphones --dry-run --max-pages 2
  python scrape_cli.py --shop mts --category smartphones --dry-run
  python scrape_cli.py --shop mts --category smartphones --save
"""
import argparse
import os
from scrapers.core import PublicFetcher, parse_products
from scrapers.shops import SHOPS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--shop', choices=SHOPS, required=True)
    parser.add_argument('--category', choices=('smartphones','tablets','watches','headphones'), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--save', action='store_true', help='Explicitly write verified observations to database')
    parser.add_argument('--max-pages', type=int, default=50)
    args = parser.parse_args()
    if not (1 <= args.max_pages <= 100):
        parser.error('--max-pages must be between 1 and 100')
    shop = SHOPS[args.shop]
    if args.category not in shop.categories:
        parser.error('Category URL not configured/verified for this shop')
    if args.shop != 'mts' and args.save:
        parser.error('--save currently enabled only for MTS')
    fetcher = PublicFetcher(shop.domain, delay=2.0,
                            user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    url = shop.categories[args.category]
    if args.shop == 'mts':
        from scrapers.mts_crawl import crawl_mts
        result = crawl_mts(fetcher, url, args.category, max_pages=args.max_pages)
        products = result.products
        print(f'MTS {args.category}: pages={result.pages}, cards={result.cards}, '
              f'priced={len(products)}, skipped={len(result.issues)}, '
              f'page_limit_reached={result.incomplete}', flush=True)
        for sku, reason in result.issues[:20]:
            print(f'  SKIP {sku}: {reason}')
    else:
        products = parse_products(fetcher.get(url), url, shop.slug, args.category)
        print(f'{shop.slug}/{args.category}: {len(products)} JSON-LD products on FIRST page only')
        result = None
    for product in products[:10]:
        print(f'{product.name}: {product.price} {product.currency} — {product.url}')
    print('Prices are PUBLIC advertised prices, NOT confirmed B2B procurement prices.')
    if not args.save:
        print('DRY RUN: no database changes')
        return
    if result.incomplete or result.issues:
        raise SystemExit('Database unchanged: incomplete crawl or skipped products; inspect output first')
    if not products:
        raise SystemExit('Database unchanged: no trustworthy products')
    import psycopg
    from scrapers.storage import init, save_observations
    dsn = os.getenv('DATABASE_URL')
    if not dsn:
        raise SystemExit('DATABASE_URL must be configured')
    with psycopg.connect(dsn) as conn:
        init(conn)
        save_observations(conn, shop.slug, args.category, products)
    print(f'Saved {len(products)} web observations; existing manual supplier offers untouched')


if __name__ == '__main__':
    main()
