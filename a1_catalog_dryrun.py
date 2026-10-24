#!/usr/bin/env python3
"""Read-only A1 catalogue crawler. stdlib, NO database writes.

Prices are *advertised catalogue* prices and may be conditional; never silently
interpret monthly instalments as full payment. Not a verified B2B price.
"""
import argparse
import json
import re
import time
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode
from urllib.request import Request, urlopen

BASE = 'https://shop.a1.by'
SECTIONS = {
    'smartphones': '/catalog/phones/smartphones',
    'tablets': '/catalog/tablets-and-books/tablets',
    'laptops': '/catalog/computers-and-gaming/laptops',
    'watches_sim': '/catalog/smart-watch/smart-watch-with-sim',
    'watches_no_sim': '/catalog/smart-watch/smart-watch-without-sim',
    'headphones_wireless': '/catalog/accessories/headphones/wireless-headphones',
    'headphones_wired': '/catalog/accessories/headphones/wired-headphones',
}
PRICE = re.compile(r'(?<![\d.,])((?:\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{1,7})[,.]\d{2})\s*(?:руб\.?|BYN)(?:\s*/\s*мес\.?|\s*в\s*мес\.)?', re.I)
PROMO = re.compile(r'промокод|wi-fi\s*\+\s*sim|wi-fi\s*\+\s*сим|с\s*тарифом|по\s*тарифу|акци|успей\s*купить|неделя\s+\S+', re.I)

def canonical_url(url):
    u = urlsplit(urljoin(BASE, url))
    if u.hostname not in ('shop.a1.by', 'www.shop.a1.by') or not u.path.startswith('/catalog/p/'):
        return None
    return urlunsplit(('https', 'shop.a1.by', u.path.rstrip('/'), '', ''))

def money_to_decimal(raw):
    return Decimal(raw.replace(' ', '').replace('\u00a0','').replace('\u202f','').replace(',', '.'))

def parse_advertised_price(text):
    """Parse prices only from one product anchor; never from whole page.
    The last full-payment amount before an instalment is the displayed offer
    on the A1 samples, but promotional terms still require human validation.
    """
    found = []
    for m in PRICE.finditer(text):
        trailing = text[m.end():m.end()+15].lower()
        raw_segment = m.group(0).lower()
        monthly = bool(re.search(r'/\s*мес|в\s*мес', raw_segment)) or bool(re.match(r'^\s*/\s*мес', trailing))
        if not monthly:
            found.append(money_to_decimal(m.group(1)))
    if not found:
        return None, [], 'no_full_price'
    # Only the prices preceding the product name are relevant for displayed offer;
    # if more than two amounts or unusual data, flag for manual review.
    price = found[-1]
    flags = []
    if len(found) > 1:
        flags.append('multiple_full_prices')
    if PROMO.search(text):
        flags.append('promotional_terms')
    if len(found) > 2:
        flags.append('price_sequence_review')
    return price, found, ','.join(flags)

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors=[]; self.a=None; self.depth=0
        self.script=None; self.jsonld=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='a' and self.a is None:
            self.a={'href':attrs.get('href',''),'text':[]}; self.depth=1
        elif tag=='a' and self.a is not None:
            self.depth+=1
        if tag=='script' and attrs.get('type','').lower()=='application/ld+json':
            self.script=[]
    def handle_data(self, data):
        if self.script is not None: self.script.append(data)
        if self.a is not None: self.a['text'].append(data)
    def handle_endtag(self, tag):
        if tag=='script' and self.script is not None:
            try: self.jsonld.append(json.loads(''.join(self.script)))
            except (ValueError,TypeError): pass
            self.script=None
        if tag=='a' and self.a is not None:
            self.depth-=1
            if self.depth==0:
                self.a['text']=' '.join(' '.join(self.a['text']).split())
                self.anchors.append(self.a); self.a=None

def fetch(url, timeout=25, retries=3):
    for attempt in range(retries):
        try:
            request=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; A1CatalogueAudit/1.0)', 'Accept-Language':'ru-RU,ru;q=0.9'})
            with urlopen(request,timeout=timeout) as response:
                if response.status!=200: raise RuntimeError('HTTP '+str(response.status))
                return response.read(10_000_000).decode(response.headers.get_content_charset() or 'utf-8',errors='replace')
        except Exception:
            if attempt+1==retries: raise
            time.sleep(min(2**attempt, 5))

def expected_count(parser):
    for obj in parser.jsonld:
        if isinstance(obj,dict) and obj.get('@type')=='ItemList':
            try: return int(obj['numberOfItems'])
            except (ValueError,KeyError,TypeError): pass
    return None

def page_url(path,page):
    return BASE+path+(('?'+urlencode({'page':page})) if page>1 else '')

def _page_choices(raw):
    parsed = PageParser()
    parsed.feed(raw)
    choices = {}
    for anchor in parsed.anchors:
        url = canonical_url(anchor['href'])
        if not url:
            continue
        text = anchor['text']
        if url not in choices or len(text) > len(choices[url]):
            choices[url] = text
    return parsed, choices


def crawl(section, max_pages, timeout, pause):
    """Crawl A1 with per-page reconciliation. Never fill missing slots from stale runs.

    Some middle pages intermittently contain 17-19 products instead of 20.
    Re-fetch suspicious pages, then keep the best current response. A
    persistent shortfall still blocks database updates in prepare_snapshot().
    """
    path = SECTIONS[section]
    items = {}
    seen_page_keys = set()
    issues, warnings = [], []
    pages = 0
    expected = None
    limit_hit = False
    page_retries = 0
    for page in range(1, max_pages + 1):
        url = page_url(path, page)
        parsed, choices = _page_choices(fetch(url, timeout))
        if page == 1:
            expected = expected_count(parsed)
        next_link = lambda doc: any(
            'page=' + str(page + 1) in a['href'] and '/catalog/' in a['href']
            for a in doc.anchors
        )
        has_next = next_link(parsed)
        overlap = len(set(choices) & set(items))
        # Middle pages normally have 20 distinct products. A shorter page
        # or cross-page overlap can result from an in-flight catalogue update.
        # Retry this page and retain the best (largest, least-overlapping)
        # single response; never merge partial/stale page responses.
        suspicious = has_next and (len(choices) < 20 or overlap > 0)
        if suspicious:
            best = (parsed, choices, has_next)
            def quality(candidate):
                _, links, _ = candidate
                return (len(set(links) - set(items)), len(links))
            for attempt in range(3):
                time.sleep(max(pause, 0.3))
                try:
                    alternative_parsed, alternative_choices = _page_choices(fetch(url, timeout))
                except Exception as exc:
                    warnings.append(f'Page {page} reread {attempt + 1} failed: {type(exc).__name__}')
                    continue
                page_retries += 1
                candidate = (alternative_parsed, alternative_choices, next_link(alternative_parsed))
                if quality(candidate) > quality(best):
                    best = candidate
                if len(set(best[1]) - set(items)) >= 20:
                    break
            parsed, choices, has_next = best
            if len(choices) < 20 or len(set(choices) & set(items)) > 0:
                warnings.append(
                    f'Page {page}: {len(choices)} products, '
                    f'{len(set(choices) & set(items))} overlaps after rereads'
                )
        if not choices:
            if page == 1:
                issues.append('No product links on first page: ' + url)
            break
        fingerprint = frozenset(choices)
        if fingerprint in seen_page_keys:
            print(f'A1 {section}: repeated page {page}; ending at {pages}', flush=True)
            break
        seen_page_keys.add(fingerprint)
        pages += 1
        for product_url, txt in choices.items():
            price, amounts, flags = parse_advertised_price(txt)
            normalized_price = str(price) if price is not None else None
            if product_url not in items:
                items[product_url] = {
                    'url': product_url, 'text': txt, 'price_byn': normalized_price,
                    'full_price_candidates': [str(p) for p in amounts],
                    'flags': flags.split(',') if flags else [], 'section': section,
                }
            elif items[product_url]['price_byn'] != normalized_price:
                issues.append('Conflicting price for ' + product_url)
        print(
            f'A1 {section}: pages={pages} unique={len(items)} '
            f'priced={sum(x["price_byn"] is not None for x in items.values())} '
            f'next={has_next}', flush=True
        )
        if not has_next:
            if len(choices) < 20:
                break
            warnings.append(f'Page {page} full but no next link; probing next page')
        if page == max_pages:
            limit_hit = has_next or len(choices) == 20
            break
        time.sleep(pause)
    if expected is not None and len(items) < expected:
        warnings.append(f'JSON-LD advertises {expected} items; found {len(items)} distinct links')
    if limit_hit:
        issues.append('max-pages limit reached; result potentially incomplete')
    if pages == 0:
        issues.append('No pages successfully parsed')
    missing = sum(x['price_byn'] is None for x in items.values())
    if missing:
        warnings.append(f'{missing} products have no recognized full-price amount')
    return {
        'section': section, 'pages': pages, 'expected_items': expected,
        'found': len(items), 'priced': len(items) - missing,
        'page_retries': page_retries,
        'issues': issues, 'warnings': warnings, 'products': list(items.values()),
    }

def main():
    ap=argparse.ArgumentParser(description='Read-only A1 catalogue extraction. NO DATABASE WRITES.')
    ap.add_argument('--sections',nargs='+',choices=sorted(SECTIONS),default=list(SECTIONS))
    ap.add_argument('--max-pages',type=int,default=100)
    ap.add_argument('--timeout',type=int,default=25)
    ap.add_argument('--pause',type=float,default=1.0)
    ap.add_argument('--json',default='/tmp/a1_catalog_dryrun.json')
    args=ap.parse_args()
    results=[]; allproducts={}; cross_conflicts=[]
    for section in args.sections:
        print('START',section,BASE+SECTIONS[section],flush=True)
        try: result=crawl(section,args.max_pages,args.timeout,args.pause)
        except Exception as exc:
            result={'section':section,'issues':[f'{type(exc).__name__}: {exc}'],'pages':0,'products':[],'warnings':[]}
        for item in result['products']:
            if item['url'] in allproducts and item['price_byn']!=allproducts[item['url']]['price_byn']:
                cross_conflicts.append(item['url'])
            else: allproducts.setdefault(item['url'],item)
        print('RESULT',json.dumps({k:v for k,v in result.items() if k!='products'},ensure_ascii=False),flush=True)
        results.append(result)
        time.sleep(args.pause)
    summary={'unique_products':len(allproducts),'priced':sum(p['price_byn'] is not None for p in allproducts.values()),
             'cross_section_conflicts':cross_conflicts,'failed_sections':[r['section'] for r in results if r['issues']],
             'note':'DRY RUN ONLY; sale and promo prices are catalogue observations, not verified unconditional/B2B prices.'}
    with open(args.json,'w',encoding='utf-8') as f: json.dump({'summary':summary,'sections':results},f,ensure_ascii=False,indent=2)
    print('SUMMARY',json.dumps(summary,ensure_ascii=False),flush=True)
    print('REPORT',args.json,flush=True)
    print('NO DB WRITES',flush=True)
if __name__=='__main__': main()
