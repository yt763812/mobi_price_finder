"""Full-store MTS snapshot preview/crawl. No database access in this module."""
import argparse
import json
import os
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlsplit
from mts_store_audit_v4 import SECTIONS, BASELINE_MIN_PAGES, RetryingFetcher, normalize_url

# More specific sections take precedence over their parent categories.
PRIORITY = ('smartphones','tablets','laptops','watches','headphones','televisions',
            'gaming_devices','terminals','accessories_all','home_all',
            'tabletpc_all','gadgets_all','tv_all')
ALL_SECTIONS = tuple(SECTIONS)


def name_key(s):
    return ' '.join(unicodedata.normalize('NFKC', str(s)).casefold().split())


def merge_catalogue(results, sections=ALL_SECTIONS):
    """Merge exact (SKU, name, price) duplicates; never guess on differing prices.

    Same URL with different identities or different prices is excluded. Different
    URLs with matching sku+name but different prices are excluded as conflicts.
    """
    by_identity = defaultdict(list)
    by_url = defaultdict(list)
    for section, result in results.items():
        for item in result.products:
            url = normalize_url(item.url)
            if urlsplit(url).hostname != 'shop.mts.by':
                raise ValueError(f'Unexpected store hostname: {url}')
            identity = (str(item.sku).strip(), name_key(item.name))
            if not all(identity) or not isinstance(item.price, Decimal) or item.price <= 0 or not item.price.is_finite():
                raise ValueError(f'Invalid catalogue item: {section}/{url}')
            entry = (section, item, url, identity)
            by_identity[identity].append(entry)
            by_url[url].append(entry)
    bad_urls = {url for url, entries in by_url.items()
                if len({e[3] for e in entries}) != 1 or len({e[1].price for e in entries}) != 1}
    bad_ids = {ident for ident, entries in by_identity.items()
               if len({e[1].price for e in entries}) != 1 or any(e[2] in bad_urls for e in entries)}
    selected = {}
    deduplicated = 0
    excluded = []
    for identity, entries in by_identity.items():
        if identity in bad_ids:
            excluded.append({'sku': identity[0], 'name': identity[1],
                             'prices': sorted({str(e[1].price) for e in entries}),
                             'urls': sorted({e[2] for e in entries})})
            continue
        entries = sorted(entries, key=lambda e: (PRIORITY.index(e[0]), len(e[2]), e[2]))
        section, item, url, _ = entries[0]
        selected[identity] = {'section': section, 'item': item, 'url': url,
                              'also_in': sorted({e[0] for e in entries})}
        deduplicated += len(entries) - 1
    return {'selected': selected, 'duplicates_removed': deduplicated,
            'conflicts_excluded': excluded, 'source_urls': len(by_url)}


def collect_full_store(*, max_pages=100, delay=2.0, emit=print, sections=ALL_SECTIONS,
                       fetcher=None, crawler=None):
    if not 1 <= max_pages <= 100:
        raise ValueError('max_pages must be 1..100')
    if fetcher is None:
        from scrapers.core import PublicFetcher
        fetcher = PublicFetcher('shop.mts.by', delay=delay,
                                user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    retrying = RetryingFetcher(fetcher)
    if crawler is None:
        from scrapers.mts_store_catalog_v4 import crawl_mts_catalog as crawler
    results, rows, failures = {}, [], []
    for section in sections:
        emit(f'START MTS full/{section}')
        try:
            result = crawler(retrying, SECTIONS[section], section, max_pages=max_pages)
            row = {'section': section, 'pages': result.pages, 'cards': result.cards,
                   'priced': len(result.products), 'issues': [str(x) for x in result.issues],
                   'incomplete': bool(result.incomplete),
                   'conflicts': len(result.conflicts or []),
                   'pagination_anomalies': result.pagination_anomalies or []}
            minimum = BASELINE_MIN_PAGES.get(section, 1)
            if result.pages < minimum:
                baseline_message = f'pages {result.pages} below historical baseline {minimum}'
                if section == 'accessories_all':
                    # This category changes size on the live MTS site. A historical
                    # page count alone must not discard an otherwise valid crawl.
                    # Keep the warning visible and retain all structural checks.
                    row['coverage_warning'] = baseline_message
                    emit(f'COVERAGE_WARNING MTS {section}: {baseline_message}; '
                         'allowing save only if all other validation passes')
                else:
                    row['error'] = baseline_message
            if result.incomplete or result.issues or not result.products:
                row['error'] = row.get('error') or 'incomplete, invalid or empty section'
            if row.get('error'):
                failures.append(f'{section}: {row["error"]}')
            else:
                results[section] = result
        except Exception as exc:
            row = {'section': section, 'error': f'{type(exc).__name__}: {exc}'}
            failures.append(f'{section}: {row["error"]}')
        rows.append(row)
        emit('SECTION ' + json.dumps(row, ensure_ascii=False))
    # Do not build/write a partial snapshot.
    merged = merge_catalogue(results, sections) if not failures else None
    summary = {'sections': rows, 'failed': failures,
               'total_unique_products': len(merged['selected']) if merged else 0,
               'duplicates_removed': merged['duplicates_removed'] if merged else 0,
               'conflicts_excluded': merged['conflicts_excluded'] if merged else [],
               'ready_to_commit': not failures and merged is not None and bool(merged['selected']),
               'coverage_verified': False,
               'note': 'All configured sections checked; unlisted leaf categories may exist.'}
    emit('FULL_STORE_SUMMARY ' + json.dumps({k:v for k,v in summary.items() if k != 'sections'}, ensure_ascii=False))
    return merged, summary


def main():
    p = argparse.ArgumentParser(description='MTS full-store preview only; no DB writes')
    p.add_argument('--max-pages', type=int, default=100)
    p.add_argument('--delay', type=float, default=float(os.getenv('SCRAPER_DELAY_SECONDS', '2')))
    p.add_argument('--json', help='Write preview report, no DB write')
    args = p.parse_args()
    merged, summary = collect_full_store(max_pages=args.max_pages, delay=args.delay)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump({**summary, 'preview': [
                {'sku': v['item'].sku, 'name':v['item'].name,'price':str(v['item'].price),
                 'url':v['url'],'category':v['section']} for v in (merged or {'selected':{}})['selected'].values()]},
                f, ensure_ascii=False, indent=2)
        print('REPORT ' + args.json, flush=True)
    return 0 if summary['ready_to_commit'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
