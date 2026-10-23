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
    if shop.slug == 'mts':
        from scrapers.mts import CardsParser
        from scrapers.mts_detail import detail_price
        from urllib.parse import urljoin, urlparse
        parser = CardsParser()
        parser.feed(html)
        products, issues = [], []
        for card in parser.cards:
            detail_url = urljoin(url, card.url)
            if (not card.sku or not card.name or not card.url
                    or urlparse(detail_url).netloc != shop.domain):
                issues.append((card.sku or '?', 'invalid product card'))
                continue
            try:
                detail_html = fetcher.get(detail_url)
                product, error = detail_price(detail_html, detail_url, card.name, card.sku, args.category)
                if product is None:
                    issues.append((card.sku, error))
                else:
                    products.append(product)
            except Exception as exc:
                issues.append((card.sku, f'{type(exc).__name__}: {exc}'))
        print(f'MTS catalogue cards: {len(parser.cards)}; product page prices: {len(products)}; skipped: {len(issues)}')
        for sku, reason in issues[:12]:
            print(f'  SKIP {sku}: {reason}')
        print('NOTE: these are advertised public product-page prices, NOT verified legal-entity purchase prices.')
    else:
        products = parse_products(html, url, shop.slug, args.category)
        print(f'{shop.slug}/{args.category}: {len(products)} JSON-LD priced products on first page')
    for p in products[:8]:
        print(f'{p.name}: {p.price} {p.currency} — {p.url}')
    if not products:
        raise SystemExit('No unambiguous full prices; no writes performed')
    if args.dry_run:
        return
    if shop.slug == "mts":
        raise SystemExit("MTS database writes are disabled in this diagnostic patch; run with --dry-run")
    import psycopg
    from scrapers.storage import init, save_observations
    if not os.getenv('DATABASE_URL'):
        raise SystemExit('DATABASE_URL must be configured')
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        init(conn)
        save_observations(conn, shop.slug, args.category, products)

if __name__ == '__main__':
    main()
