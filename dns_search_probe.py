#!/usr/bin/env python3
"""Read-only DNS search diagnostic. No database access or project imports."""
import argparse
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = 'https://dns-shop.by'
PRODUCT = re.compile(r'^/ru/product/[0-9a-f-]+/[^/]+/?$', re.I)
BLOCK = re.compile(r'captcha|access denied|verify you are human|cloudflare|проверка браузера', re.I)

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.products = {}
        self.title = ''
        self.in_title = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'title': self.in_title = True
        if tag == 'a' and a.get('href'):
            u = urllib.parse.urljoin(BASE, a['href'])
            p = urllib.parse.urlparse(u)
            if p.hostname in ('dns-shop.by', 'www.dns-shop.by') and PRODUCT.fullmatch(p.path):
                self.products[p.path] = u
    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
    def handle_data(self, data):
        if self.in_title: self.title += data

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--query', default='iphone 16')
    ap.add_argument('--timeout', type=int, default=30)
    args = ap.parse_args()
    url = BASE + '/ru/search/?' + urllib.parse.urlencode({'q': args.query})
    print('URL:', url, flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; DNSSearchProbe/1.0)', 'Accept-Language': 'ru-RU,ru;q=0.9', 'Accept': 'text/html'})
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as response:
            data = response.read(3_000_000)
            charset = response.headers.get_content_charset() or 'utf-8'
            html = data.decode(charset, errors='replace')
            print('HTTP:', response.status)
            print('Final URL:', response.url)
            print('HTML bytes:', len(data))
            parser = Links()
            parser.feed(html)
            print('Page title:', parser.title.strip()[:200])
            print('Possible verification:', bool(BLOCK.search(html)))
            print('Product links found:', len(parser.products))
            for u in list(parser.products.values())[:10]: print('Product:', u)
            print('Note: links alone do not confirm prices or availability.')
    except urllib.error.HTTPError as e:
        print('HTTP error:', e.code, e.reason)
    except Exception as e:
        print('Network error:', type(e).__name__, str(e)[:300])

if __name__ == '__main__':
    main()
