"""Read-only coverage audit of MTS catalogue sections.

Runs against project's existing fast crawler. Never imports storage/DB.
Does not claim complete coverage until category validation is done.
"""
import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

# Verified public section URLs. Some are overlapping by design; audit deduplicates by URL.
SECTIONS = {
    'smartphones': 'https://shop.mts.by/phones/',
    'tablets': 'https://shop.mts.by/tabletpc/tablet/',
    'laptops': 'https://shop.mts.by/tabletpc/laptops/',
    'watches': 'https://shop.mts.by/gadgets/smart-watch/',
    'headphones': 'https://shop.mts.by/gadgets/headphones/',
    'tabletpc_all': 'https://shop.mts.by/tabletpc/',
    'gadgets_all': 'https://shop.mts.by/gadgets/',
    'home_all': 'https://shop.mts.by/for-home/',
    'tv_all': 'https://shop.mts.by/tv/',
    'televisions': 'https://shop.mts.by/tv/tv2/',
    'accessories_all': 'https://shop.mts.by/accessories/',
    'terminals': 'https://shop.mts.by/terminals/',
    'gaming_devices': 'https://shop.mts.by/tv/igrovye-devaysy/',
    'accessory_audio': 'https://shop.mts.by/accessories/audio/',
}


def audit(sections, max_pages, delay):
    from scrapers.core import PublicFetcher
    from scrapers.mts_catalog import crawl_mts_catalog
    fetcher = PublicFetcher('shop.mts.by', delay=delay,
                            user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    rows = []
    by_url = defaultdict(list)
    for section in sections:
        start = time.monotonic()
        base = SECTIONS[section]
        print(f'START {section}: {base}', flush=True)
        try:
            r = crawl_mts_catalog(fetcher, base, section, max_pages=max_pages)
            row = {'section': section, 'url': base, 'pages': r.pages,
                   'cards': r.cards, 'priced': len(r.products),
                   'issues': [str(x) for x in r.issues],
                   'incomplete': bool(r.incomplete),
                   'seconds': round(time.monotonic() - start, 1)}
            for offer in r.products:
                url = offer.url.split('#', 1)[0].rstrip('/') + '/'
                if urlparse(url).hostname != 'shop.mts.by':
                    row['issues'].append(f'Unexpected host: {url}')
                    continue
                by_url[url].append({'section': section, 'price': str(offer.price),
                                    'name': offer.name})
        except Exception as exc:
            row = {'section': section, 'url': base,
                   'error': f'{type(exc).__name__}: {exc}',
                   'seconds': round(time.monotonic() - start, 1)}
        rows.append(row)
        print('RESULT ' + json.dumps(row, ensure_ascii=False), flush=True)
    duplicates = {url: entries for url, entries in by_url.items() if len(entries) > 1}
    price_conflicts = {url: entries for url, entries in duplicates.items()
                       if len({entry['price'] for entry in entries}) > 1}
    summary = {
        'sections': rows, 'unique_priced_urls': len(by_url),
        'repeated_urls': len(duplicates), 'cross_section_price_conflicts': price_conflicts,
        'coverage_verified': False,
        'note': ('AUDIT ONLY. Parent sections might list only featured products; '
                 'some leaf categories may not be covered. Do not replace supplier from this report.'),
    }
    print('SUMMARY ' + json.dumps({k: v for k, v in summary.items()
                                   if k != 'sections' and k != 'cross_section_price_conflicts'},
                                  ensure_ascii=False), flush=True)
    for url, entries in list(price_conflicts.items())[:20]:
        print('CROSS_CATEGORY_PRICE_CONFLICT ' + json.dumps({'url': url, 'entries': entries},
                                                             ensure_ascii=False), flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description='Read-only MTS store catalogue coverage audit')
    p.add_argument('--max-pages', type=int, default=100)
    p.add_argument('--delay', type=float, default=float(os.getenv('SCRAPER_DELAY_SECONDS', '2')))
    p.add_argument('--sections', nargs='+', choices=tuple(SECTIONS) + ('all',), default=['all'])
    p.add_argument('--json', metavar='PATH', help='Write full audit report')
    a = p.parse_args()
    if not 1 <= a.max_pages <= 100 or a.delay < 0.5:
        p.error('max-pages must be 1..100 and delay >= 0.5')
    selected = list(SECTIONS) if 'all' in a.sections else list(dict.fromkeys(a.sections))
    result = audit(selected, a.max_pages, a.delay)
    if a.json:
        Path(a.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'REPORT {a.json}', flush=True)
    return 0 if all(not s.get('error') and not s.get('incomplete') and not s.get('issues')
                    for s in result['sections']) else 2

if __name__ == '__main__':
    raise SystemExit(main())
