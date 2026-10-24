#!/usr/bin/env python3
"""DNS Belarus electronic catalogue reconnaissance. Standard library only; never writes DB.
Usage: python -u dns_catalog_probe.py --json /tmp/dns_probe.json
       python -u dns_catalog_probe.py --url https://dns-shop.by/catalog/... --pages 2
"""
import argparse
import gzip
import html
from html.parser import HTMLParser
import json
import re
import ssl
import time
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urldefrag, urlencode, parse_qsl, urlunparse
from urllib.request import Request, urlopen

HOME = 'https://dns-shop.by/'
START_URLS = [HOME, 'https://dns-shop.by/catalog/']
KEYWORDS = ('смартфон', 'телефон', 'планшет', 'электронн', 'ноутбук', 'компьютер',
            'монитор', 'видеокарт', 'процессор', 'материнск', 'оперативн', 'памят',
            'накопител', 'ssd', 'жестк', 'наушник', 'акустик', 'колонк', 'аудио',
            'телевизор', 'приставк', 'консол', 'игров', 'веб-камер', 'фотоаппарат',
            'камер', 'роутер', 'маршрутизатор', 'сетев', 'wi-fi', 'умн', 'браслет',
            'час', 'клавиатур', 'мыш', 'перифер', 'аксессуар', 'гаджет', 'заряд',
            'power bank', 'электроник', 'комплектующ', 'принтер', 'сканер')
EXCLUDE = ('ремонт', 'сад', 'туризм', 'офисн', 'мебел', 'посуд', 'освещ', 'бытов',
           'холодильник', 'стиральн', 'пылесос', 'кухн', 'строител', 'сантехник',
           'инструмент', 'автотовар', 'детск', 'косметик', 'красот', 'одежд')
PRICE_RE = re.compile(r'(?<!\d)(\d[\d\s\u00a0\u202f]{0,10}(?:[,.]\d{1,2})?)\s*(?:руб(?:\.|лей)?|byn|br\b|₽)', re.I)
PROD_PATH = re.compile(r'/(?:product|products|tovar|goods|p)/|/product-[\w-]+|/\d{5,}(?:/|$)', re.I)
PAGE_HINT = re.compile(r'(?:^|[?&])(?:page|p|pageNumber|offset)=\d+|/page-\d+', re.I)
BLOCK_HINT = re.compile(r'captcha|access denied|доступ ограничен|проверка браузера|cloudflare', re.I)


def local_url(url, base=HOME):
    absolute = urldefrag(urljoin(base, html.unescape(url)))[0]
    parsed = urlparse(absolute)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('dns-shop.by', 'www.dns-shop.by'):
        return None
    return absolute


class Inspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._current = []
        self.title = ''
        self._in_title = False
        self.meta = {}
        self.jsonld = []
        self._script = None
        self._script_parts = []
        self.script_sources = []
        self.forms = []
        self.canonicals = []
        self.tag_counts = Counter()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tag_counts[tag] += 1
        if tag == 'a':
            self._current.append({'href': a.get('href',''), 'parts': [], 'class': a.get('class',''), 'title': a.get('title','')})
        if tag == 'title':
            self._in_title = True
        if tag == 'meta':
            key = a.get('property') or a.get('name')
            if key and key.lower() in ('description','og:title','og:description','og:price:amount','product:price:amount','robots'):
                self.meta[key] = (a.get('content') or '')[:300]
        if tag == 'link' and a.get('rel') in ('canonical', 'next', 'prev'):
            self.canonicals.append({'rel':a.get('rel'), 'href':a.get('href')})
        if tag == 'script':
            self._script = a.get('type','').lower()
            self._script_parts = []
            if a.get('src'):
                self.script_sources.append(a['src'][:200])
        if tag == 'form':
            self.forms.append({'action':a.get('action',''), 'method':a.get('method','GET')})

    def handle_data(self, data):
        if self._current:
            self._current[-1]['parts'].append(data)
        if self._in_title:
            self.title += data
        if self._script is not None and self._script in ('application/ld+json','application/json'):
            self._script_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._current:
            item = self._current.pop()
            item['text'] = re.sub(r'\s+', ' ', ' '.join(item.pop('parts'))).strip()[:250]
            self.anchors.append(item)
            if self._current:
                self._current[-1]['parts'].append(item['text'])
        if tag == 'title':
            self._in_title = False
        if tag == 'script':
            if self._script == 'application/ld+json':
                body = ''.join(self._script_parts).strip()
                if body:
                    try:
                        self.jsonld.append(json.loads(body))
                    except ValueError:
                        self.jsonld.append({'unparsed_excerpt':body[:200]})
            self._script = None
            self._script_parts = []


def fetch(url, timeout=18):
    req = Request(url, headers={
        'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        'Accept':'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        'Accept-Language':'ru-BY,ru;q=0.9,en;q=0.7',
        'Accept-Encoding':'gzip,identity', 'Connection':'close'
    })
    began = time.monotonic()
    try:
        with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as response:
            data = response.read(5_000_001)
            truncated = len(data)>5_000_000
            data = data[:5_000_000]
            if response.headers.get('Content-Encoding') == 'gzip':
                data = gzip.decompress(data)
            charset = response.headers.get_content_charset() or 'utf-8'
            return {'status':response.status,'final_url':response.url,
                    'content_type':response.headers.get('Content-Type',''),
                    'bytes_read':len(data),'truncated':truncated,
                    'html':data.decode(charset,errors='replace'),
                    'seconds':round(time.monotonic()-began,2)}
    except HTTPError as exc:
        return {'status':exc.code,'final_url':exc.url,'error':str(exc),'seconds':round(time.monotonic()-began,2)}
    except (URLError,TimeoutError, OSError, UnicodeError) as exc:
        return {'status':None,'final_url':url,'error':f'{type(exc).__name__}: {exc}',
                'seconds':round(time.monotonic()-began,2)}


def compact_jsonld(node, found=None):
    if found is None: found=[]
    if len(found)>=12: return found
    if isinstance(node,list):
        for v in node: compact_jsonld(v,found)
    elif isinstance(node,dict):
        kind = node.get('@type')
        if kind:
            found.append({'type':kind,'name':str(node.get('name',''))[:90],
                          'numberOfItems':node.get('numberOfItems'),
                          'price':str(node.get('price',''))[:35],
                          'offers_type':str((node.get('offers') or {}).get('@type','')) if isinstance(node.get('offers'),dict) else ''})
        for key in ('@graph','itemListElement','mainEntity'):
            if key in node: compact_jsonld(node[key],found)
    return found[:12]


def analyze(url, timeout):
    f = fetch(url, timeout)
    result = {k:v for k,v in f.items() if k!='html'}
    if not f.get('html'):
        return result
    source = f['html']
    parser = Inspector()
    try:
        parser.feed(source)
    except Exception as exc:
        result['parse_error'] = str(exc)[:200]
    anchors=[]
    for a in parser.anchors:
        link=local_url(a['href'], f['final_url'])
        if link:
            anchors.append({'url':link, 'text':a['text'],'class':a['class'][:90]})
    seen=set(); unique=[]
    for a in anchors:
        if a['url'] not in seen:
            unique.append(a);seen.add(a['url'])
    categories=[]
    for a in unique:
        text=(a['text']+' '+urlparse(a['url']).path.replace('-',' ')).lower()
        if any(word in text for word in KEYWORDS) and not any(bad in text for bad in EXCLUDE):
            categories.append(a)
    products=[a for a in unique if PROD_PATH.search(urlparse(a['url']).path)]
    pages=[a for a in unique if PAGE_HINT.search(a['url']) or a['text'].isdigit() and len(a['text'])<=3]
    money=PRICE_RE.findall(re.sub(r'<[^>]+>', ' ', source))
    result.update({'title':parser.title.strip()[:200], 'meta':parser.meta,
        'html_characters':len(source), 'anchor_count':len(unique),
        'category_candidates':categories[:65], 'category_candidates_total':len(categories),
        'product_link_candidates':products[:12], 'product_candidates_total':len(products),
        'pagination_candidates':pages[:15],
        'money_samples':money[:18], 'money_matches':len(money),
        'jsonld':compact_jsonld(parser.jsonld),
        'has_next_data':'__NEXT_DATA__' in source,
        'has_nuxt':'__NUXT__' in source or '__nuxt' in source,
        'script_sources':parser.script_sources[:10], 'canonical_links':parser.canonicals[:5],
        'forms':parser.forms[:8], 'maybe_blocked': bool(BLOCK_HINT.search(parser.title+' '+re.sub(r'<[^>]*>',' ',source[:15000]))),
        'note':'Samples are independent: DO NOT associate a money sample with a product or write to PostgreSQL.'})
    return result


def main():
    arg=argparse.ArgumentParser(description='Safe DNS Belarus website reconnaissance: no DB writes')
    arg.add_argument('--url',action='append',help='Additional exact DNS Belarus URL (repeatable)')
    arg.add_argument('--pages',type=int,default=1,help='Number of pages to sample per extra URL (max 3)')
    arg.add_argument('--timeout',type=int,default=18)
    arg.add_argument('--json',default='',help='Optional JSON report path')
    args=arg.parse_args()
    if not 1<=args.pages<=3: arg.error('--pages must be 1..3')
    urls=list(START_URLS)
    for target in args.url or []:
        if local_url(target) != target: arg.error('Only absolute https://dns-shop.by or https://www.dns-shop.by URLs allowed')
        for page in range(1,args.pages+1):
            if page==1: urls.append(target)
            else:
                p=urlparse(target);q=dict(parse_qsl(p.query));q['page']=str(page)
                urls.append(urlunparse(p._replace(query=urlencode(q))))
    results=[]
    for u in dict.fromkeys(urls):
        print('START',u,flush=True)
        r=analyze(u,args.timeout)
        print('RESULT',json.dumps(r,ensure_ascii=False),flush=True)
        results.append(r)
        time.sleep(.5)
    report={'site':HOME,'mode':'DIAGNOSTIC ONLY — NO DB WRITES',
            'results':results,'timestamp_utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    if args.json:
        with open(args.json,'w',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
        print('REPORT',args.json,flush=True)
    print('SUMMARY',json.dumps({'checked':len(results),'http_200':sum(r.get('status')==200 for r in results),
         'errors':sum(bool(r.get('error')) for r in results),
         'note':'NO DB WRITES; prices and product links not yet associated'},ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
