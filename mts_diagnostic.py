"""Read-only, bounded MTS diagnostics for the /mtsupdate flow.

MTS_DIAGNOSTIC_ONLY=1 makes /mtsupdate inspect the first catalogue page
and at most twelve product pages without calling supplier replacement.
No HTML, credentials or private response bodies are stored or logged.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import json
import os
import time
from urllib.parse import urljoin


def run_diagnostic(*, emit=print, fetcher=None, section='smartphones', limit=None, workers=None):
    from mts_store_audit_v4 import SECTIONS
    from scrapers.mts import CardsParser, read_price
    from scrapers.mts_modern import detail_offer, discover_cards

    if section not in SECTIONS:
        raise ValueError(f'Unknown MTS section: {section}')
    limit = int(limit if limit is not None else os.getenv('MTS_DIAGNOSTIC_LIMIT', '12'))
    workers = int(workers if workers is not None else os.getenv('MTS_DIAGNOSTIC_WORKERS', '2'))
    if not 1 <= limit <= 12 or not 1 <= workers <= 3:
        raise ValueError('Diagnostic limits: 1..12 products; 1..3 workers')
    if fetcher is None:
        from scrapers.core import PublicFetcher
        # Use the same safe public fetcher as the production crawler, but
        # do not wrap it in a retry loop during diagnosis.
        fetcher = PublicFetcher('shop.mts.by', delay=float(os.getenv('SCRAPER_DELAY_SECONDS', '2')),
                                user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    url = SECTIONS[section]
    started = time.monotonic()
    emit(f'MTS_DIAG_START section={section} limit={limit} workers={workers} no_database_write=true')
    try:
        html = fetcher.get(url)
    except Exception as exc:
        emit('MTS_DIAG_CATALOG_ERROR ' + json.dumps({'type': type(exc).__name__, 'message': str(exc)[:220],
                                                    'elapsed_seconds': round(time.monotonic()-started, 2)}, ensure_ascii=False))
        return 2
    catalog_secs = round(time.monotonic() - started, 2)
    legacy = CardsParser()
    legacy.feed(html)
    modern = discover_cards(html, url) if not legacy.cards else []
    cards = legacy.cards if legacy.cards else modern
    mode = 'legacy' if legacy.cards else 'modern'
    emit('MTS_DIAG_CATALOG ' + json.dumps({'section': section, 'mode': mode,
         'catalog_seconds': catalog_secs, 'cards_found_on_first_page': len(cards),
         'cards_to_test': min(limit, len(cards))}, ensure_ascii=False))
    if not cards:
        emit('MTS_DIAG_SUMMARY ' + json.dumps({'status': 'no_cards', 'tested': 0, 'priced': 0,
             'no_database_write': True}, ensure_ascii=False))
        return 2
    selected = cards[:limit]

    def one(index, card):
        started_one = time.monotonic()
        detail_url = urljoin(url, card.url)
        try:
            # In legacy layout, capture the catalogue observation separately.
            listed = (str(read_price(card.displayed_price)) if read_price(card.displayed_price) is not None
                      else None) if mode == 'legacy' else None
            content = fetcher.get(detail_url)
            sku, price, error = detail_offer(content, detail_url, card.name)
            outcome = 'priced' if error is None and isinstance(price, Decimal) and price > 0 else 'unpriced'
            return {'index': index, 'name': card.name[:110], 'url': detail_url,
                    'status': outcome, 'sku': sku, 'price_byn': str(price) if outcome == 'priced' else None,
                    'catalogue_price_byn': listed, 'reason': str(error)[:240] if error else None,
                    'elapsed_seconds': round(time.monotonic() - started_one, 2)}
        except Exception as exc:
            return {'index': index, 'name': card.name[:110], 'url': detail_url,
                    'status': 'http_error', 'reason': f'{type(exc).__name__}: {str(exc)[:200]}',
                    'elapsed_seconds': round(time.monotonic() - started_one, 2)}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, i, c) for i, c in enumerate(selected, 1)]
        for f in as_completed(futures):
            row = f.result()
            results.append(row)
            emit('MTS_DIAG_ITEM ' + json.dumps(row, ensure_ascii=False))
    results.sort(key=lambda x: x['index'])
    counts = Counter(x['status'] for x in results)
    emit('MTS_DIAG_SUMMARY ' + json.dumps({
        'section': section, 'mode': mode, 'tested': len(results),
        'priced': counts['priced'], 'unpriced': counts['unpriced'], 'http_errors': counts['http_error'],
        'elapsed_seconds': round(time.monotonic()-started, 2),
        'no_database_write': True,
    }, ensure_ascii=False))
    return 0 if counts['priced'] == len(results) else 2
