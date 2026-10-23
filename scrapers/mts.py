"""MTS catalogue card diagnostics. No database writes in this module.

Only unambiguous *full* prices are converted to Product objects.
'от' pricing and conflicting analytics/display values are logged, not imported.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import re
from scrapers.core import Product

@dataclass
class Card:
    sku: str = ''
    name: str = ''
    url: str = ''
    ga_price: str = ''
    displayed_price: str = ''
    approximate: bool = False
    installment_seen: bool = False
    availability: str = ''

class CardsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.depth = 0
        self.title_depth = None
        self.price_depth = None
        self.price_classes = ''
        self.price_text = []
        self.title_text = []
        self.button_depth = None
        self.button_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set(a.get('class', '').split())
        if self.card is None:
            if tag == 'article' and 'card-product' in classes:
                self.card = Card(sku=a.get('data-ga-item-id', ''), name=a.get('data-ga-item-name', ''), ga_price=a.get('data-ga-item-price', ''))
                self.depth = 1
            return
        if tag in {'img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'area', 'wbr'}:
            return
        self.depth += 1
        if tag == 'a' and 'card-product__link' in classes:
            self.card.url = a.get('href', '')
        if tag == 'p' and 'card-product__title' in classes:
            self.title_depth = self.depth
            self.title_text = []
        if tag == 'p' and 'card-product__price' in classes:
            self.price_depth = self.depth
            self.price_classes = a.get('class', '')
            self.price_text = []
        if tag == 'button' and ('card-product__button' in classes):
            self.button_depth = self.depth
            self.button_text = []

    def handle_data(self, data):
        if self.card is None:
            return
        if self.title_depth is not None:
            self.title_text.append(data)
        if self.price_depth is not None:
            self.price_text.append(data)
        if self.button_depth is not None:
            self.button_text.append(data)

    def handle_endtag(self, tag):
        if tag in {'img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'area', 'wbr'}:
            return
        if self.card is None:
            return
        if tag == 'p' and self.title_depth == self.depth:
            value = ' '.join(''.join(self.title_text).split())
            if value:
                self.card.name = value
            self.title_depth = None
        if tag == 'p' and self.price_depth == self.depth:
            value = ' '.join(''.join(self.price_text).split())
            if 'мес' in value.lower() or 'month' in value.lower():
                self.card.installment_seen = True
            elif 'руб' in value.lower() and 'card-product__price--red' not in self.price_classes:
                self.card.displayed_price = value
                self.card.approximate = bool(re.search(r'(^|\s)от\s', value, flags=re.I))
            self.price_depth = None
        if tag == 'button' and self.button_depth == self.depth:
            self.card.availability = ' '.join(''.join(self.button_text).split())
            self.button_depth = None
        if tag == 'article' and self.depth == 1:
            self.cards.append(self.card)
            self.card = None
            self.depth = 0
            return
        self.depth -= 1

def read_price(value):
    # Price label like '3099 руб.' or '3 099,50 руб.'
    m = re.search(r'([\d\s\u00a0]+(?:[,.]\d{1,2})?)\s*руб', value)
    if not m:
        return None
    try:
        price = Decimal(m.group(1).replace(' ', '').replace('\xa0','').replace(',', '.'))
        return price if price.is_finite() and price > 0 else None
    except InvalidOperation:
        return None

def inspect_mts(html, page_url, category):
    parser = CardsParser()
    parser.feed(html)
    accepted = []
    issues = []
    for c in parser.cards:
        link = urljoin(page_url, c.url)
        if not c.sku or not c.name or not c.url or urlparse(link).netloc != urlparse(page_url).netloc:
            issues.append((c.sku or '?', 'missing valid SKU, name or product URL'))
            continue
        visible = read_price(c.displayed_price)
        if visible is None:
            issues.append((c.sku, f'no full price: {c.displayed_price!r}'))
            continue
        if c.approximate:
            issues.append((c.sku, f'FROM price {visible} BYN; analytics={c.ga_price or "?"}'))
            continue
        try:
            ga = Decimal(c.ga_price)
        except (InvalidOperation, ValueError):
            ga = None
        if ga is not None and ga != visible:
            issues.append((c.sku, f'conflicting prices: visible={visible} BYN, analytics={ga} BYN'))
            continue
        accepted.append(Product('mts', category, c.sku, c.name, link, visible, 'BYN', c.availability or None))
    return accepted, issues, len(parser.cards)
