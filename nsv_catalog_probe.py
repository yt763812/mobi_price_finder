#!/usr/bin/env python3
"""Read-only NSV Belarus catalog diagnostic. Standard library only. No database access."""
import argparse
import gzip
import html
import json
import re
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'https://nsv.by/'
DEFAULT_PATHS = ['/', '/catalog/', '/robots.txt', '/sitemap.xml']
UA = 'Mozilla/5.0 (compatible; NSVCatalogDiagnostic/1.0; +read-only)'
MAX_BYTES = 1_500_000

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []; self.scripts = []; self.title = []; self._title = False
        self._script = None; self.jsonld = []; self.meta = {}; self.labels = []
        self._anchor = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title': self._title = True
        if tag == 'meta' and (attrs.get('name') or attrs.get('property')):
            key = attrs.get('name') or attrs.get('property')
            if key in ('description','og:title','og:description','robots'): self.meta[key] = attrs.get('content','')[:300]
        if tag == 'a' and attrs.get('href'):
            self._anchor = {'url':attrs['href'], 'text':''}
            self.links.append(self._anchor)
        if tag == 'script':
            self._script = {'type':attrs.get('type',''), 'src':attrs.get('src',''), 'data':[]}
            self.scripts.append(self._script)
    def handle_endtag(self, tag):
        if tag == 'title': self._title = False
        if tag == 'a': self._anchor = None
        if tag == 'script':
            if self._script and self._script['type'] == 'application/ld+json':
                data = ''.join(self._script['data']).strip()
                if data: self.jsonld.append(data[:1200])
            self._script = None
    def handle_data(self, data):
        if self._title: self.title.append(data)
        if self._anchor: self._anchor['text'] += data
        if self._script: self._script['data'].append(data)

KEYWORDS = re.compile(r'смартф|телефон|планшет|ноутбук|компьютер|монитор|наушник|час|аксессуар|игр|телевизор|колонк|электроник|гаджет|каталог|smartphon|phone|tablet|laptop|catalog|watch|headphone',re.I)
EXCLUDE = re.compile(r'бытов|кухн|сад|мебел|ремонт|посуд|туризм|освещ|холодильник|стиральн', re.I)
PRICE = re.compile(r'(?<!\d)(?:\d[\d\s\u00a0\u202f]{0,12}[.,]\d{2}|\d{2,6})\s*(?:BYN|бел\.?\s*руб\.?|руб\.?|р\.)\b',re.I)
BLOCK = re.compile(r'(access denied|checking your browser|just a moment|captcha|verification|cloudflare|доступ запрещ|проверка браузера)',re.I)

def clean_url(value, parent):
    u = urlsplit(urljoin(parent, value))
    if u.scheme not in ('http','https') or u.hostname not in ('nsv.by','www.nsv.by'): return None
    return urlunsplit((u.scheme,u.netloc,u.path,u.query,''))

def fetch(url, timeout):
    req = Request(url, headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5','Accept-Language':'ru-RU,ru;q=0.9','Accept-Encoding':'gzip'})
    start=time.monotonic()
    try:
        with build_opener(HTTPRedirectHandler()).open(req,timeout=timeout) as r:
            data=r.read(MAX_BYTES+1)
            status=r.status; final=r.url; ct=r.headers.get('Content-Type','')
            if r.headers.get('Content-Encoding','').lower()=='gzip': data=gzip.decompress(data)
    except HTTPError as exc:
        return {'url':url,'status':exc.code,'error':f'HTTP {exc.code}: {exc.reason}','seconds':round(time.monotonic()-start,2),'blocked':exc.code in (401,403,429,503)}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {'url':url,'error':f'{type(exc).__name__}: {exc}','seconds':round(time.monotonic()-start,2),'blocked':False}
    truncated=len(data)>MAX_BYTES
    data=data[:MAX_BYTES]
    encoding='utf-8'
    charset=re.search(r'charset\s*=\s*([\w-]+)',ct,re.I)
    if charset: encoding=charset.group(1)
    try: body=data.decode(encoding,errors='replace')
    except LookupError: body=data.decode('utf-8',errors='replace')
    result={'url':url,'status':status,'final_url':final,'content_type':ct,'bytes_read':len(data),'truncated':truncated,'seconds':round(time.monotonic()-start,2)}
    if 'html' not in ct.lower() and '<html' not in body[:500].lower():
        result.update({'text_sample':body[:600]})
        return result
    parser=Parser()
    try: parser.feed(body)
    except Exception as exc: result['html_parse_warning']=str(exc)
    title=html.unescape(' '.join(parser.title)).strip()
    selected=[]; products=[]; unique=set(); product_seen=set()
    for a in parser.links:
        u=clean_url(a['url'],final)
        if not u: continue
        name=re.sub(r'\s+',' ',a['text']).strip()[:110]
        if (KEYWORDS.search(name+' '+urlsplit(u).path) and not EXCLUDE.search(name+' '+urlsplit(u).path)) and u not in unique:
            unique.add(u); selected.append({'url':u,'text':name})
        if re.search(r'/(?:product|products|tovar|item|catalog)/',urlsplit(u).path,re.I) and u not in product_seen:
            product_seen.add(u); products.append({'url':u,'text':name})
    prices=PRICE.findall(re.sub(r'<script\b.*?</script>', ' ',body, flags=re.I|re.S))
    block=bool(BLOCK.search(title)) or (len(body)<10000 and bool(BLOCK.search(body)))
    result.update({'title':title[:250],'html_characters':len(body),'anchor_count':len(parser.links),
        'category_candidates_total':len(selected),'category_candidates':selected[:30],
        'product_link_candidates_total':len(products),'product_link_candidates':products[:12],
        'price_hints_count':len(prices),'price_hints':prices[:12],
        'jsonld_types':[x[:250] for x in parser.jsonld[:3]],
        'script_src_samples':[s['src'][:160] for s in parser.scripts if s['src']][:8],
        'pagination_links':[{'url':clean_url(a['url'],final),'text':a['text'].strip()[:20]} for a in parser.links if re.search(r'(?:[?&]page=|/page/|страниц|далее)',a['url']+' '+a['text'],re.I)][:12],
        'blocked_or_challenge':block,
        'note':'Link and price samples are independent. NEVER use this diagnostic to write supplier offers.'})
    return result

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--json',default='')
    ap.add_argument('--timeout',type=int,default=18)
    ap.add_argument('--url',action='append',default=[],help='Optional extra nsv.by page; repeated flag allowed')
    args=ap.parse_args()
    urls=[urljoin(BASE,p) for p in DEFAULT_PATHS]
    for candidate in args.url:
        u=clean_url(candidate,BASE)
        if not u: ap.error('--url must be on nsv.by')
        if u not in urls: urls.append(u)
    results=[]
    for idx,url in enumerate(urls):
        if idx: time.sleep(0.7)
        print('START',url,flush=True)
        item=fetch(url,args.timeout);results.append(item)
        print('RESULT',json.dumps(item,ensure_ascii=False),flush=True)
    summary={'checked':len(results),'http_200':sum(x.get('status')==200 for x in results),
        'blocked_or_challenge':sum(bool(x.get('blocked') or x.get('blocked_or_challenge')) for x in results),
        'no_db_writes':True}
    print('SUMMARY',json.dumps(summary,ensure_ascii=False))
    if args.json:
        with open(args.json,'w',encoding='utf-8') as f: json.dump({'results':results,'summary':summary},f,ensure_ascii=False,indent=2)
        print('REPORT',args.json)
    print('NO DB WRITES')

if __name__=='__main__': main()
