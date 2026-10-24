"""Read-only MTS full-store audit v2. Does NOT access PostgreSQL.

Uses existing catalogue parser; retries transient network failures conservatively,
records overlaps and diagnostics. Does NOT assert full-store coverage.
"""
import argparse
import json
import os
import random
import socket
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from mts_store_audit import SECTIONS as ORIGINAL_SECTIONS

# The former accessory_audio URL returned a confirmed 404. Do not request it.
SECTIONS = {k: v for k, v in ORIGINAL_SECTIONS.items() if k != 'accessory_audio'}

# This route returned HTTP 404 in the first audit. Keep it visible in the report.
KNOWN_UNAVAILABLE = set()


def normalize_url(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or '').lower()
    port = f':{parts.port}' if parts.port and parts.port != 443 else ''
    path = '/'.join(filter(None, parts.path.split('/')))
    path = '/' + path + '/' if path else '/'
    # Retain query parameters, except marketing trackers; queries may identify variants.
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                            if not k.lower().startswith(('utm_', 'yclid', 'gclid', 'fbclid'))))
    return urlunsplit((scheme, host + port, path, query, ''))


def transient(exc):
    if isinstance(exc, HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, socket.timeout, URLError, OSError))


class RetryingFetcher:
    def __init__(self, base, retries=3, backoff=2.0):
        self.base = base
        self.retries = retries
        self.backoff = backoff
        self.retries_used = 0

    def get(self, url):
        for attempt in range(self.retries + 1):
            try:
                return self.base.get(url)
            except Exception as exc:
                if attempt == self.retries or not transient(exc):
                    raise
                self.retries_used += 1
                pause = min(20.0, self.backoff * (2 ** attempt))
                if isinstance(exc, HTTPError):
                    value = exc.headers.get('Retry-After') if exc.headers else None
                    if value and value.isdigit():
                        pause = max(pause, min(60.0, float(value)))
                print(f'RETRY {attempt + 1}/{self.retries} {url} '
                      f'{type(exc).__name__}: {exc}; sleeping {pause:g}s', flush=True)
                time.sleep(pause)


def diagnose_cards(fetcher, section, url, limit=12):
    """Collect non-sensitive HTML diagnostics for nonstandard cards, read-only."""
    from scrapers.mts import CardsParser
    html = fetcher.get(url)
    parser = CardsParser()
    parser.feed(html)
    result = []
    for card in parser.cards[:limit]:
        result.append({'sku': card.sku, 'name': card.name, 'url': card.url,
                       'displayed_price': card.displayed_price,
                       'analytics_price': card.ga_price})
    print('CARD_DIAGNOSTICS ' + json.dumps({'section': section, 'cards': result},
                                           ensure_ascii=False), flush=True)


def audit(sections, max_pages=100, delay=2.0, retries=3, backoff=2.0,
          fetcher=None, crawler=None):
    if fetcher is None:
        from scrapers.core import PublicFetcher
        fetcher = PublicFetcher('shop.mts.by', delay=delay,
                                user_agent=os.getenv('SCRAPER_USER_AGENT', 'MobiPriceFinder/0.1'))
    retry_fetcher = RetryingFetcher(fetcher, retries, backoff)
    if crawler is None:
        from scrapers.mts_store_catalog import crawl_mts_catalog
        crawler = crawl_mts_catalog
    rows = []
    by_url = defaultdict(list)
    for section in sections:
        base = SECTIONS[section]
        start = time.monotonic()
        count_before = retry_fetcher.retries_used
        print(f'START {section}: {base}', flush=True)
        try:
            result = crawler(retry_fetcher, base, section, max_pages=max_pages)
            row = {'section': section, 'url': base, 'pages': result.pages,
                   'cards': result.cards, 'priced': len(result.products),
                   'issues': [str(x) for x in result.issues],
                   'incomplete': bool(result.incomplete),
                   'conflicts': len(result.conflicts or []),
                   'sample_urls': [normalize_url(p.url) for p in result.products[:3]]}
            for offer in result.products:
                canon = normalize_url(offer.url)
                if urlsplit(canon).hostname != 'shop.mts.by':
                    row['issues'].append(f'Unexpected host: {canon}')
                    continue
                by_url[canon].append({'section': section, 'price': str(offer.price),
                                      'sku': offer.sku, 'name': offer.name})
            if row['issues'] and section == 'gaming_devices':
                try:
                    diagnose_cards(retry_fetcher, section, base)
                except Exception as exc:
                    print(f'DIAGNOSTICS_FAILED {section}: {exc}', flush=True)
        except Exception as exc:
            row = {'section': section, 'url': base,
                   'error': f'{type(exc).__name__}: {exc}',
                   'known_unavailable_route': section in KNOWN_UNAVAILABLE and
                   isinstance(exc, HTTPError) and exc.code == 404}
        row['retries_used'] = retry_fetcher.retries_used - count_before
        row['seconds'] = round(time.monotonic() - start, 1)
        rows.append(row)
        print('RESULT ' + json.dumps(row, ensure_ascii=False), flush=True)
    repeats = {url: entries for url, entries in by_url.items() if len(entries) > 1}
    cross = {url: entries for url, entries in repeats.items()
             if len({e['section'] for e in entries}) > 1}
    conflicts = {url: entries for url, entries in repeats.items()
                 if len({e['price'] for e in entries}) > 1}
    summary = {'sections': rows, 'unique_priced_urls': len(by_url),
               'repeated_urls': len(repeats), 'cross_section_repeats': len(cross),
               'cross_section_price_conflicts': conflicts,
               'total_retries': retry_fetcher.retries_used,
               'coverage_verified': False,
               'note': 'AUDIT ONLY: no DB writes; full leaf-category coverage not verified.'}
    print('SUMMARY ' + json.dumps({k: v for k, v in summary.items()
                                   if k not in ('sections', 'cross_section_price_conflicts')},
                                  ensure_ascii=False), flush=True)
    for url, entries in list(conflicts.items())[:30]:
        print('PRICE_CONFLICT ' + json.dumps({'url': url, 'entries': entries},
                                             ensure_ascii=False), flush=True)
    for url, entries in list(cross.items())[:10]:
        print('OVERLAP_EXAMPLE ' + json.dumps({'url': url, 'sections': [e['section'] for e in entries]},
                                              ensure_ascii=False), flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description='MTS store read-only stage 3 audit')
    p.add_argument('--sections', nargs='+', choices=tuple(SECTIONS) + ('all',), default=['all'])
    p.add_argument('--max-pages', type=int, default=100)
    p.add_argument('--delay', type=float, default=2.0)
    p.add_argument('--retries', type=int, default=3)
    p.add_argument('--backoff', type=float, default=2.0)
    p.add_argument('--json', metavar='FILE')
    a = p.parse_args()
    if not 1 <= a.max_pages <= 100 or a.delay < 1 or not 0 <= a.retries <= 5 or a.backoff < 1:
        p.error('limits: pages 1..100, delay >=1, retries 0..5, backoff >=1')
    sections = list(SECTIONS) if 'all' in a.sections else list(dict.fromkeys(a.sections))
    output = audit(sections, a.max_pages, a.delay, a.retries, a.backoff)
    if a.json:
        Path(a.json).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REPORT ' + a.json, flush=True)
    failures = [r for r in output['sections']
                if (r.get('error') and not r.get('known_unavailable_route'))
                or r.get('issues') or r.get('incomplete')]
    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
