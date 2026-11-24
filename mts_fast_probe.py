"""Read-only targeted MTS catalogue probe. Never opens product details or writes DB.

Run from the bot's /mtsupdate command by setting MTS_FAST_PROBE=1.
MTS_PROBE_SECTION=smartphones; MTS_PROBE_PAGES=1,36,37,38,45.
"""
import json
import os
import re
import time
from urllib.parse import urljoin
from scrapers.mts import CardsParser
from scrapers.mts_modern import discover_cards
from scrapers.mts_store_catalog_v4 import (discover_catalogue_preorders,
                                           catalogue_page_url)
from mts_store_audit_v4 import SECTIONS, RetryingFetcher


def parse_pages(value):
    parts = [p.strip() for p in value.split(',') if p.strip()]
    if not 1 <= len(parts) <= 8:
        raise ValueError('Expected 1..8 page numbers')
    pages = []
    for part in parts:
        if not part.isascii() or not part.isdecimal():
            raise ValueError('Page numbers must be positive decimal integers')
        n = int(part)
        if not 1 <= n <= 100:
            raise ValueError('Pages must be 1..100')
        if n not in pages:
            pages.append(n)
    return pages


def inspect_page(html, page_url, section, page):
    legacy = CardsParser()
    legacy.feed(html)
    modern = discover_cards(html, page_url) if not legacy.cards else []
    preorders = discover_catalogue_preorders(html, page_url)
    legacy_urls = {urljoin(page_url, c.url or '') for c in legacy.cards if c.url}
    modern_urls = {c.url for c in modern}
    priced = legacy_urls | modern_urls
    overlap = priced & set(preorders)
    # Log coverage rather than asserting that all product tiles were discovered.
    flat = re.sub(r'<[^>]*>', ' ', html)
    totals = re.findall(r'Показано\s*[\d\s]+\s*[–—-]\s*[\d\s]+\s*из\s*(\d+)', flat, re.I)
    return {'section': section, 'page': page, 'url': page_url,
            'legacy_cards': len(legacy.cards), 'modern_cards': len(modern),
            'preorder_cta_cards': len(preorders),
            'priced_and_preorder_overlap': len(overlap),
            'unique_detected_products': len(priced | set(preorders)),
            'has_preorder_text': bool(re.search(r'предзаказ', flat, re.I)),
            'advertised_total': int(totals[-1]) if totals else None,
            'sample_preorder_urls': list(preorders)[:2],
            'sample_product_urls': list(priced - set(preorders))[:2]}


def run_fast_probe(*, emit=print, fetcher=None):
    section = os.getenv('MTS_PROBE_SECTION', 'smartphones').strip()
    if section not in SECTIONS:
        emit('MTS_PROBE_ERROR invalid section: ' + section)
        return 2
    try:
        pages = parse_pages(os.getenv('MTS_PROBE_PAGES', '1,36,37,38,45'))
    except ValueError as exc:
        emit('MTS_PROBE_ERROR ' + str(exc))
        return 2
    if fetcher is None:
        from scrapers.core import PublicFetcher
        base = PublicFetcher('shop.mts.by',
                             delay=float(os.getenv('SCRAPER_DELAY_SECONDS', '1')),
                             user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
        fetcher = RetryingFetcher(base, retries=1, backoff=2.0)
    base_url = SECTIONS[section]
    emit('MTS_PROBE_START ' + json.dumps({'section': section, 'pages': pages,
                                          'no_database_write': True}, ensure_ascii=False))
    failures = []
    for n in pages:
        url = base_url if n == 1 else catalogue_page_url(base_url, n)
        start = time.monotonic()
        try:
            html = fetcher.get(url)
            report = inspect_page(html, url, section, n)
            report['elapsed_seconds'] = round(time.monotonic() - start, 2)
            emit('MTS_PROBE_PAGE ' + json.dumps(report, ensure_ascii=False))
            # Preorder text without localized preorder cards needs further work.
            if report['has_preorder_text'] and report['preorder_cta_cards'] == 0:
                failures.append(n)
        except Exception as exc:
            failures.append(n)
            emit('MTS_PROBE_ERROR ' + json.dumps({'page': n, 'url': url,
                        'reason': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
    emit('MTS_PROBE_SUMMARY ' + json.dumps({'section': section, 'pages_tested': len(pages),
                           'failed_or_unresolved_pages': failures,
                           'no_database_write': True}, ensure_ascii=False))
    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(run_fast_probe())
