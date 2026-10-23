"""MTS public product detail: conservative JSON-LD price reader.

Data represent a site's advertised price, NOT a confirmed business/cash price.
"""
import json
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlparse
from scrapers.core import Product


class JSONLDScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.collect = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.collect = True
            self.parts = []

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.collect:
            self.scripts.append(''.join(self.parts))
            self.collect = False


def _nodes(node):
    if isinstance(node, list):
        for x in node:
            yield from _nodes(x)
    elif isinstance(node, dict):
        yield node
        if '@graph' in node:
            yield from _nodes(node['@graph'])


def detail_price(html, detail_url, expected_name, sku, category):
    parsed = JSONLDScripts()
    parsed.feed(html)
    candidates = []
    for raw in parsed.scripts:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        for item in _nodes(obj):
            kind = item.get('@type', [])
            if isinstance(kind, str):
                kind = [kind]
            if 'Product' not in kind:
                continue
            name = item.get('name')
            if not isinstance(name, str) or ' '.join(name.split()).casefold() != ' '.join(expected_name.split()).casefold():
                continue
            offer_items = item.get('offers', [])
            if isinstance(offer_items, dict):
                offer_items = [offer_items]
            if not isinstance(offer_items, list):
                continue
            for offer in offer_items:
                if not isinstance(offer, dict) or offer.get('@type') not in ('Offer', ['Offer']):
                    continue
                if offer.get('priceCurrency') != 'BYN':
                    continue
                try:
                    price = Decimal(str(offer.get('price')))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if not price.is_finite() or price <= 0:
                    continue
                candidates.append((price, offer.get('availability')))
    if not candidates:
        return None, 'missing matching Product Offer in JSON-LD'
    prices = {x[0] for x in candidates}
    if len(prices) != 1:
        return None, f'conflicting detail Offer prices: {sorted(prices)}'
    avail = candidates[0][1]
    if not isinstance(avail, str):
        avail = None
    else:
        avail = avail.rsplit('/', 1)[-1]
    return Product('mts', category, sku, expected_name, detail_url, candidates[0][0], 'BYN', avail), None
