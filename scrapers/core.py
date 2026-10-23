"""Conservative JSON-LD product collection from public catalogue pages.

No authentication, browser automation, protection bypass, or speculative APIs.
Incomplete crawls are never treated as complete snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import json
import time
import urllib.request

USER_AGENT = 'MobiPriceFinder/0.1 (public catalog; contact: configure-before-use)'
CATEGORIES = ('smartphones', 'tablets', 'watches', 'headphones')

@dataclass(frozen=True)
class Product:
    shop: str
    category: str
    sku: str
    name: str
    url: str
    price: Decimal
    currency: str
    availability: str | None = None

class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.links = []
        self._script = False
        self._buffer = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self._script = True
            self._buffer = []
        if tag == 'a' and attrs.get('href'):
            self.links.append((attrs['href'], attrs.get('rel', '')))
        if tag == 'link' and attrs.get('rel') == 'next' and attrs.get('href'):
            self.links.append((attrs['href'], 'next'))
    def handle_data(self, data):
        if self._script:
            self._buffer.append(data)
    def handle_endtag(self, tag):
        if tag == 'script' and self._script:
            self.scripts.append(''.join(self._buffer))
            self._script = False

def _nodes(obj):
    if isinstance(obj, list):
        for part in obj:
            yield from _nodes(part)
    elif isinstance(obj, dict):
        yield obj
        for prop in ('@graph', 'itemListElement', 'item', 'mainEntity'):
            if prop in obj:
                yield from _nodes(obj[prop])

def parse_products(html: str, page_url: str, shop: str, category: str) -> list[Product]:
    p = DocumentParser()
    p.feed(html)
    out = {}
    for script in p.scripts:
        try:
            document = json.loads(script)
        except json.JSONDecodeError:
            continue
        for node in _nodes(document):
            kinds = node.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if not any(str(t).lower().endswith('product') for t in kinds):
                continue
            offers = node.get('offers', {})
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                raw_price = offer.get('price')
                spec = offer.get('priceSpecification')
                if raw_price is None and isinstance(spec, dict):
                    raw_price = spec.get('price')
                try:
                    price = Decimal(str(raw_price).replace(' ', '').replace(',', '.'))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if not price.is_finite() or price <= 0:
                    continue
                currency = str(offer.get('priceCurrency') or (spec.get('priceCurrency') if isinstance(spec, dict) else '') or '').upper()
                if currency != 'BYN':
                    continue
                name = node.get('name')
                raw_url = node.get('url') or offer.get('url')
                if not isinstance(name, str) or not name.strip() or not isinstance(raw_url, str):
                    continue
                url = urljoin(page_url, raw_url)
                if urlparse(url).netloc != urlparse(page_url).netloc:
                    continue
                sku = str(node.get('sku') or node.get('productID') or url).strip()
                availability = offer.get('availability')
                if isinstance(availability, str):
                    availability = availability.rsplit('/', 1)[-1]
                else:
                    availability = None
                product = Product(shop, category, sku, name.strip(), url, price, currency, availability)
                out[(shop, sku, url)] = product
    return list(out.values())

class PublicFetcher:
    def __init__(self, domain: str, delay: float = 2.0, user_agent: str = USER_AGENT):
        self.domain = domain
        self.delay = max(1.0, delay)
        self.agent = user_agent
        self.last_request = 0.0
        self.robot = RobotFileParser()
        self.robot.set_url(f'https://{domain}/robots.txt')
        # Fail closed when robots cannot be fetched.
        self.robot.read()
        if self.robot.disallow_all or self.robot.default_entry is None and not self.robot.entries and not self.robot.allow_all:
            raise RuntimeError(f'robots.txt unavailable or restrictive: {domain}')
    def get(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != self.domain:
            raise ValueError('URL outside allowed domain')
        if not self.robot.can_fetch(self.agent, url):
            raise PermissionError(f'robots.txt disallows {url}')
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={'User-Agent': self.agent, 'Accept': 'text/html'})
        self.last_request = time.monotonic()
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type:
                raise ValueError('Unexpected content type')
            payload = response.read(4_000_001)
            if len(payload) > 4_000_000:
                raise ValueError('Response too large')
            return payload.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
