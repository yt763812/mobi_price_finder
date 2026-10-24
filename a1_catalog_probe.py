#!/usr/bin/env python3
"""A1 catalogue reconnaissance. Read-only; Python standard library only."""
import argparse
import html
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

BASE = 'https://shop.a1.by'
SECTIONS = {
    'smartphones': '/catalog/phones/smartphones',
    'tablets': '/catalog/tablets-and-books/tablets',
    'laptops': '/catalog/computers-and-gaming/laptops',
    'watches': '/catalog/smart-watch',
    'headphones': '/catalog/accessories/headphones',
}
MONEY = re.compile(r'(?<!\d)(?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d{2,6})[,.]\d{2}\s*(?:руб\.?|BYN)', re.I)
PRODUCT = re.compile(r'^/catalog/p/[^?#]+')

class Probe(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.current = None
        self.a_depth = 0
        self.text_chunks = []
        self.script_types = {}
        self._script = None
        self.jsonld = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and self.current is None:
            self.current = {'href': attrs.get('href', ''), 'text': []}
            self.a_depth = 1
        elif tag == 'a' and self.current is not None:
            self.a_depth += 1
        if tag == 'script':
            self._script = {'type': attrs.get('type', ''), 'id': attrs.get('id', ''), 'text': []}
            key = self._script['type'] or '(none)'
            self.script_types[key] = self.script_types.get(key, 0) + 1
    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.a_depth -= 1
            if self.a_depth == 0:
                self.current['text'] = ' '.join(' '.join(self.current['text']).split())
                self.anchors.append(self.current)
                self.current = None
        if tag == 'script' and self._script is not None:
            if self._script['type'] == 'application/ld+json':
                self.jsonld.append(''.join(self._script['text'])[:400])
            self._script = None
    def handle_data(self, data):
        if self._script is not None:
            self._script['text'].append(data)
        elif self.current is not None:
            self.current['text'].append(data)
        if data.strip() and self._script is None:
            self.text_chunks.append(data.strip())

def read(url, timeout):
    request = Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; CatalogueCheck/1.0)', 'Accept':'text/html,application/xhtml+xml', 'Accept-Language':'ru-RU,ru;q=0.9'})
    with urlopen(request, timeout=timeout) as resp:
        raw = resp.read(8_000_000)
        charset = resp.headers.get_content_charset() or 'utf-8'
        return resp.status, resp.geturl(), raw.decode(charset, errors='replace')

def audit(name, url, timeout):
    status, final_url, source = read(url, timeout)
    p = Probe(); p.feed(source)
    products = {}
    for a in p.anchors:
        full = urljoin(BASE, a['href'])
        parsed = urlparse(full)
        if parsed.netloc == 'shop.a1.by' and PRODUCT.match(parsed.path):
            key = parsed.path.rstrip('/')
            if key not in products or len(a['text']) > len(products[key]['text']):
                products[key] = {'url':full.split('?')[0], 'text':a['text'][:240]}
    pagination = []
    for a in p.anchors:
        href = a['href']; txt = a['text']
        if re.search(r'(?i)(page=|/page/|страниц|следующ|показать\s+ещё|next)', href+' '+txt):
            pagination.append({'href':urljoin(BASE,href)[:220], 'text':txt[:80]})
    return {'section':name, 'requested_url':url, 'status':status, 'final_url':final_url,
            'html_characters':len(source), 'product_link_count':len(products),
            'product_samples':list(products.values())[:8],
            'money_text_samples':list(dict.fromkeys(MONEY.findall(' '.join(p.text_chunks))))[:18],
            'pagination_candidates':pagination[:20], 'script_types':p.script_types,
            'jsonld_samples':p.jsonld[:2],
            'note':'Link and price samples are independent. Do NOT match price to product or write to DB from this audit.'}

def main():
    ap=argparse.ArgumentParser(description='A1 HTML diagnostics, no database writes')
    ap.add_argument('--sections', nargs='+', choices=sorted(SECTIONS), default=['smartphones'])
    ap.add_argument('--timeout', type=int, default=25)
    ap.add_argument('--json', default=None)
    args=ap.parse_args()
    results=[]
    for name in args.sections:
        url=urljoin(BASE,SECTIONS[name]); start=time.monotonic()
        print('START',name,url,flush=True)
        try:
            result=audit(name,url,args.timeout)
        except Exception as ex:
            result={'section':name,'requested_url':url,'error':f'{type(ex).__name__}: {ex}'}
        result['elapsed_seconds']=round(time.monotonic()-start,1)
        results.append(result)
        print('RESULT',json.dumps(result,ensure_ascii=False),flush=True)
        time.sleep(1)
    if args.json:
        with open(args.json,'w',encoding='utf8') as f:
            json.dump(results,f,ensure_ascii=False,indent=2)
        print('REPORT',args.json,flush=True)
if __name__=='__main__': main()
