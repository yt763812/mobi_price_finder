"""Manual diagnostic collector; not yet a production full-catalogue scraper.

Usage: python scrape_cli.py --shop mts --category smartphones --dry-run
"""
import argparse
import os
from scrapers.core import PublicFetcher, parse_products
from scrapers.shops import SHOPS

def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('--shop', required=True, choices=SHOPS)
    cli.add_argument('--category', required=True, choices=('smartphones','tablets','watches','headphones'))
    cli.add_argument('--dry-run', action='store_true')
    args = cli.parse_args()
    shop = SHOPS[args.shop]
    if args.category not in shop.categories:
        raise SystemExit(f'{shop.slug}/{args.category}: URL not verified; do not guess')
    url = shop.categories[args.category]
    fetcher = PublicFetcher(shop.domain, user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    html = fetcher.get(url)
    products = parse_products(html, url, shop.slug, args.category)
    print(f'{shop.slug}/{args.category}: {len(products)} JSON-LD priced products on first page')
    for p in products[:8]:
        print(f'{p.name}: {p.price} {p.currency} — {p.url}')
    if not products:
        raise SystemExit('No verified structured product prices; no writes performed')
    if args.dry_run:
        return
    import psycopg
    from scrapers.storage import init, save_observations
    if not os.getenv('DATABASE_URL'):
        raise SystemExit('DATABASE_URL must be configured')
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        init(conn)
        save_observations(conn, shop.slug, args.category, products)

if __name__ == '__main__':
    main()
