"""Read-only targeted MTS catalogue probe. Never opens product details or writes DB.

Run from the bot's /mtsupdate command by setting MTS_FAST_PROBE=1.
MTS_PROBE_SECTION=smartphones; MTS_PROBE_PAGES=1,36,37,38,45.
"""
import json
import os
import re
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.mts import CardsParser
from scrapers.mts_modern import discover_cards, _product_link
from scrapers.mts_store_catalog_v4 import (discover_catalogue_preorders,
                                           catalogue_page_url, valid_store_product_url)
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



def page_link_audit(html, page_url):
    """Heuristic audit only: candidates are NOT imported as sellable offers.

    Counts all unique product-like links, including tiles without a purchase
    button. These candidates are for diagnosing gaps, not proof of availability.
    """
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main') or soup.select_one('[role=main]') or soup
    candidates = {}
    for anchor in main.select('a[href]'):
        name = ' '.join(anchor.get_text(' ', strip=True).split())
        if not 7 <= len(name) <= 180:
            continue
        url = _product_link(anchor.get('href', ''), page_url)
        if url and valid_store_product_url(url, page_url):
            candidates.setdefault(url, name)
    return candidates


def run_coverage_probe(section, *, emit, fetcher):
    """Audit all advertised pages without visiting any individual product URL."""
    base_url = SECTIONS[section]
    emit('MTS_COVERAGE_START ' + json.dumps({
        'section': section, 'no_database_write': True}, ensure_ascii=False))
    seen_detected, seen_candidates = set(), set()
    failures = []
    advertised = None
    pages = None
    from math import ceil
    for n in range(1, 101):
        if pages is not None and n > pages:
            break
        page_url = base_url if n == 1 else catalogue_page_url(base_url, n)
        start = time.monotonic()
        try:
            html = fetcher.get(page_url)
            report = inspect_page(html, page_url, section, n)
            if n == 1:
                flat = re.sub(r'<[^>]*>', ' ', html)
                match = re.search(r'Показано\s*1\s*[–—-]\s*(\d+)\s*из\s*(\d+)', flat, re.I)
                if not match:
                    emit('MTS_COVERAGE_ERROR no advertised page count on first page')
                    return 2
                per_page, advertised = map(int, match.groups())
                if per_page <= 0 or advertised < per_page:
                    emit('MTS_COVERAGE_ERROR invalid advertised page count')
                    return 2
                pages = ceil(advertised / per_page)
                if pages > 100:
                    emit('MTS_COVERAGE_ERROR advertised pages exceed 100')
                    return 2
            legacy = CardsParser()
            legacy.feed(html)
            priced = {urljoin(page_url, c.url or '') for c in legacy.cards if c.url}
            if not legacy.cards:
                priced.update(c.url for c in discover_cards(html, page_url))
            preorders = set(discover_catalogue_preorders(html, page_url))
            detected = priced | preorders
            candidates = set(page_link_audit(html, page_url))
            new_detected = detected - seen_detected
            new_candidates = candidates - seen_candidates
            overlooked_candidates = candidates - detected
            seen_detected.update(detected)
            seen_candidates.update(candidates)
            if not detected or (len(detected) > 0 and not new_detected):
                failures.append(n)
            data = {'section': section, 'page': n, 'advertised_pages': pages,
                    'detected': len(detected), 'new_detected': len(new_detected),
                    'preorders': len(preorders), 'product_like_links': len(candidates),
                    'new_product_like_links': len(new_candidates),
                    'unclassified_product_like_links': len(overlooked_candidates),
                    'sample_unclassified_urls': sorted(overlooked_candidates)[:3],
                    'elapsed_seconds': round(time.monotonic() - start, 2)}
            emit('MTS_COVERAGE_PAGE ' + json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            failures.append(n)
            emit('MTS_COVERAGE_ERROR ' + json.dumps({
                'page': n, 'reason': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
    summary = {'section': section, 'advertised_total': advertised,
               'pages_checked': pages, 'unique_detected': len(seen_detected),
               'unique_product_like_links': len(seen_candidates),
               'unexplained_counter_gap': max(0, (advertised or 0) - len(seen_detected)),
               'unclassified_unique_links': len(seen_candidates - seen_detected),
               'failed_or_repeated_pages': failures, 'no_database_write': True}
    emit('MTS_COVERAGE_SUMMARY ' + json.dumps(summary, ensure_ascii=False))
    # This probe NEVER approves a commit. A clean scan is diagnostic only.
    return 2 if failures else 0


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
    if os.getenv('MTS_PROBE_COVERAGE', '0') == '1':
        return run_coverage_probe(section, emit=emit, fetcher=fetcher)
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
