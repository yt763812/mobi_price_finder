#!/usr/bin/env python3
"""DNS Belarus catalogue diagnostic. READ ONLY: no project/database imports.

Usage:
  python -u dns_catalog_dryrun.py --max-pages 2 --json /tmp/dns_probe.json
  python -u dns_catalog_dryrun.py --max-pages 50 --json /tmp/dns_full.json

A 403 or verification page is a hard failure, never an empty price list.
"""
import argparse
import html
from html.parser import HTMLParser
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

BASE = 'https://dns-shop.by'
CATEGORY = '/ru/category/17a8a01d16404e77/smartfony/'
PRODUCT_PATH = re.compile(r'^/ru/product/[0-9a-f-]+/[^/]+/?$', re.I)
PRICE = re.compile(r'(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]\d{3})*[.,]\d{2})\s*(?:BYN|руб\.?|Br\b)', re.I)
PAGING = re.compile(r'"totalCount"\s*:\s*(\d+)\s*,\s*"pageCurrent"\s*:\s*(\d+)\s*,\s*"pageLast"\s*:\s*(\d+)')


def normalized_link(href):
    if not href:
        return None
    link = urlparse(urljoin(BASE, html.unescape(href)))
    if link.netloc.lower() not in ('dns-shop.by', 'www.dns-shop.by'):
        return None
    return link.path if PRODUCT_PATH.fullmatch(link.path) else None


class Cards(HTMLParser):
    """Capture product-list LI blocks, nesting correctly including child LI tags."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.list_depth = 0
        self.capture = None
        self.depth = 0
        self.cards = []
        self.all_product_links = []
        self._anchor = None
        self._title = False
        self.page_title = ''

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        css = d.get('class', '')
        if tag == 'title':
            self._title = True
        if tag == 'a':
            product = normalized_link(d.get('href'))
            if product:
                self.all_product_links.append(product)
            self._anchor = product
        if self.capture is not None:
            if tag not in ('img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'area', 'wbr'):
                self.depth += 1
            self.capture['tags'].append((tag, css))
            if tag == 'a' and self._anchor:
                self.capture['links'].append(self._anchor)
            if tag == 'img' and d.get('alt'):
                self.capture['alt'].append(d['alt'])
            return
        if tag == 'li' and ('catalog-category-products__product' in css or 'catalog-product' in css):
            self.capture = {'links': [], 'text': [], 'alt': [], 'tags': [(tag, css)]}
            self.depth = 1

    def handle_endtag(self, tag):
        if tag == 'title':
            self._title = False
        if tag == 'a':
            self._anchor = None
        if self.capture is not None:
            self.depth -= 1
            if self.depth <= 0:
                self.cards.append(self.capture)
                self.capture = None
                self.depth = 0

    def handle_data(self, data):
        if self._title:
            self.page_title += data
        if self.capture is not None:
            self.capture['text'].append(data)


def parse_page(body):
    p = Cards()
    p.feed(body)
    m = PAGING.search(body)
    metadata = None if not m else dict(zip(('totalCount', 'pageCurrent', 'pageLast'), map(int, m.groups())))
    title = html.unescape(p.page_title).strip()
    blocked = bool(re.search(r'(?i)^(verification|just a moment|access denied|captcha)', title))
    blocked |= len(body) < 25000 and bool(re.search(r'(?i)(captcha|cloudflare|verification|access denied)', body))
    records, ambiguities = [], []
    for card in p.cards:
        links = list(dict.fromkeys(card['links']))
        if len(links) != 1:
            if links:
                ambiguities.append({'reason': 'multiple_product_links', 'urls': links[:3]})
            continue
        content = html.unescape(' '.join(card['text']))
        clean = re.sub(r'\s+', ' ', content).strip()
        prices = list(dict.fromkeys(m.group(1).replace('\xa0',' ').replace('\u202f', ' ').replace(' ', '').replace(',', '.') for m in PRICE.finditer(clean)))
        # Multiple distinct amounts can be old/sale prices. Never auto-select.
        records.append({'url': urljoin(BASE, links[0]), 'text_sample': clean[:240],
                        'price_candidates_byn': prices, 'price_byn': prices[0] if len(prices) == 1 else None})
    urls = list(dict.fromkeys(p.all_product_links))
    return {'title': title, 'blocked': blocked, 'paging': metadata,
            'card_blocks': len(p.cards), 'unique_product_links': len(urls),
            'associated_cards': len(records), 'priced_unambiguous': sum(r['price_byn'] is not None for r in records),
            'price_conflicts': sum(len(r['price_candidates_byn']) > 1 for r in records),
            'all_product_urls': urls, 'records': records, 'card_issues': ambiguities}


def fetch(url, timeout):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; DNSCatalogDiagnostic/1.0)',
                                 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ru-BY,ru;q=0.9'})
    try:
        with urlopen(req, timeout=timeout) as res:
            data = res.read(5_000_001)
            if len(data) > 5_000_000:
                raise ValueError('Response exceeds 5 MB safety limit')
            charset = res.headers.get_content_charset() or 'utf-8'
            return res.status, res.url, data.decode(charset, errors='replace'), None
    except HTTPError as e:
        return e.code, url, '', f'HTTP {e.code}: {e.reason}'
    except (URLError, OSError, ValueError) as e:
        return None, url, '', f'{type(e).__name__}: {e}'


def run(max_pages, delay, timeout):
    output = {'source': 'dns-shop.by', 'category': 'smartphones', 'dry_run': True,
              'pages': [], 'errors': [], 'records': [], 'complete': False}
    seen = set()
    total = last = None
    for n in range(1, max_pages + 1):
        url = BASE + CATEGORY + (f'?page={n}' if n > 1 else '')
        print(f'START DNS page={n} {url}', flush=True)
        status, final, body, error = fetch(url, timeout)
        if error or status != 200:
            output['errors'].append(f'Page {n}: {error or "HTTP " + str(status)}; access denied or unavailable')
            print(f'STOP DNS HTTP={status}: {error}. NO DB WRITES', flush=True)
            break
        detail = parse_page(body)
        summary = {k: v for k, v in detail.items() if k not in ('records', 'all_product_urls', 'card_issues')}
        summary.update({'page': n, 'http_status': status, 'html_chars': len(body)})
        output['pages'].append(summary)
        print('PAGE ' + json.dumps(summary, ensure_ascii=False), flush=True)
        if detail['blocked'] or not detail['paging']:
            output['errors'].append(f'Page {n}: verification/challenge or missing pagination metadata')
            break
        p = detail['paging']
        if p['pageCurrent'] != n or p['pageLast'] < n or p['totalCount'] < 1:
            output['errors'].append(f'Page {n}: inconsistent pagination metadata {p}')
            break
        if total is None:
            total, last = p['totalCount'], p['pageLast']
        elif (total, last) != (p['totalCount'], p['pageLast']):
            output['errors'].append(f'Page {n}: catalogue total/pages changed during crawl')
            break
        if not detail['all_product_urls']:
            output['errors'].append(f'Page {n}: no product links found')
            break
        new = set(detail['all_product_urls']) - seen
        if not new:
            output['errors'].append(f'Page {n}: repeated page, no new products')
            break
        seen.update(new)
        output['records'].extend(detail['records'])
        if n == last:
            if len(seen) != total:
                output['errors'].append(f'Expected {total} distinct products, found {len(seen)}')
            else:
                output['complete'] = True
            break
        if n < max_pages:
            time.sleep(delay)
    output['expected'] = total
    output['unique_links'] = len(seen)
    output['records_with_unambiguous_price'] = sum(r['price_byn'] is not None for r in output['records'])
    output['ready_to_save'] = False  # diagnostic only, regardless of completeness
    return output


def main():
    ap = argparse.ArgumentParser(description='Read-only DNS smartphone catalogue diagnostic')
    ap.add_argument('--max-pages', type=int, default=2, help='Pages to check (default: 2; 50 for full)')
    ap.add_argument('--json', default='/tmp/dns_catalog_dryrun.json')
    ap.add_argument('--delay', type=float, default=1.3)
    ap.add_argument('--timeout', type=float, default=15)
    args = ap.parse_args()
    if not (1 <= args.max_pages <= 60 and 0.5 <= args.delay <= 30 and 3 <= args.timeout <= 90):
        ap.error('max-pages must be 1..60; delay 0.5..30; timeout 3..90')
    result = run(args.max_pages, args.delay, args.timeout)
    with open(args.json, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print('SUMMARY ' + json.dumps({k: v for k, v in result.items() if k not in ('records', 'pages')}, ensure_ascii=False), flush=True)
    print('REPORT ' + args.json, flush=True)
    print('NO DB WRITES', flush=True)
    if result['errors']:
        sys.exit(2)


if __name__ == '__main__':
    main()
