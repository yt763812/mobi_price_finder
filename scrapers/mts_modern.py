"""MTS HTML 2026 adapter. Only read-only public pages, never infer installment prices.

Conservative: product discovery != verified purchase price. The latter is read
from an explicit single-payment block on the matching product detail page.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

MONEY = re.compile(r"(?<![\d.,])([1-9]\d{0,5}(?:[ \u00a0]\d{3})*(?:[,.]\d{1,2})?)\s*(?:BYN|руб\.?)(?![\w/])", re.I)
SKU = re.compile(r"Код\s+товара\s*:\s*([\w-]+)", re.I)
SINGLE = re.compile(r"^(?:Единый\s+плат[её]ж|Оплата\s+сразу|Полная\s+стоимость)$", re.I)
EXCLUDE = re.compile(r"мес\.?|рассроч|кредит|в\s+месяц|плат[её]ж\s+в\s+месяц", re.I)

@dataclass(frozen=True)
class ModernCard:
    name: str
    url: str


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split())


def _price(text):
    hits = list(MONEY.finditer(text.replace('\u202f',' ')))
    if len(hits) != 1 or EXCLUDE.search(text):
        return None
    try:
        value = Decimal(hits[0].group(1).replace(' ', '').replace('\xa0', '').replace(',', '.'))
        return value if value.is_finite() and value > 0 else None
    except InvalidOperation:
        return None


def _product_link(href, base):
    url = urljoin(base, href)
    u = urlsplit(url)
    root = urlsplit(base)
    if u.scheme != 'https' or u.hostname != 'shop.mts.by' or u.hostname != root.hostname:
        return None
    if u.username or u.password or u.query or u.fragment:
        return None
    # News/marketing pages and category-navigation tiles are not SKU pages.
    # Keep real game-console SKUs: only exclude the category landing URL.
    if (u.path.startswith('/news-actions/') or
        u.path.rstrip('/').split('/')[-1] in {
            'game-consoles', 'consoles-gamepades', 'smart-watch',
            'headphones', 'laptops', 'tablet', 'phones', 'audio',
            'computer-equipment', 'accessories', 'tv', 'for-home',
        }):
        return None
    parts = [part for part in u.path.split('/') if part]
    # A product is nested below its category (not a category itself).
    return url if len(parts) >= 3 and parts[-1].lower() not in ('catalog','index.php') else None


def discover_cards(html: str, page_url: str) -> list[ModernCard]:
    """Find linked products only within card-like sections containing purchase UI.

    This works with class changes, but deliberately excludes navigation,
    recommendations, and category links lacking local purchase controls.
    """
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main') or soup.select_one('[role=main]') or soup
    found = {}
    for anchor in main.select('a[href]'):
        name = _text(anchor)
        if len(name) < 7 or len(name) > 180:
            continue
        url = _product_link(anchor.get('href',''), page_url)
        if not url:
            continue
        for ancestor in list(anchor.parents)[:7]:
            if ancestor is main or ancestor.name in ('body','html'):
                break
            text = _text(ancestor)
            if len(text) > 1400:
                break
            has_purchase = bool(ancestor.select('button')) and ('Купить' in text or 'В корзину' in text)
            has_money = bool(re.search(r'\bBYN\b|\bруб\.', text, re.I))
            if has_purchase and has_money:
                found.setdefault(url, ModernCard(name, url))
                break
    return list(found.values())


def detail_offer(html: str, detail_url: str, expected_name: str):
    """Return (SKU, price, error). Prefer precision over collection coverage.

    Exact details must have a unique single-payment price and matching title;
    no catalogue 'from' amount, discount old price, or monthly payment is used.
    """
    soup = BeautifulSoup(html, 'lxml')
    h1 = soup.find('h1')
    if h1 is None:
        return None, None, 'detail missing H1'
    actual_name = _text(h1)
    normal = lambda s: ' '.join(s.casefold().split())
    if normal(actual_name) != normal(expected_name):
        return None, None, f'detail title mismatch: {actual_name[:90]}'
    main = soup.select_one('main') or soup.select_one('[role=main]') or soup
    text = _text(main)
    matched = SKU.search(text)
    if not matched:
        return None, None, 'detail missing product code'
    sku = matched.group(1)
    # The live 2026 storefront often renders the payment label, current price,
    # and crossed-out old price in different nested DOM branches. Looking at
    # parent.get_text() loses the boundary and rejects the valid current price.
    # Read the *first* monetary value following the explicit payment label in
    # document order, before any payment-plan / old-price labels.
    visible = [node for node in main.find_all(string=True)
               if node.parent and node.parent.name not in ('script', 'style', 'noscript')
               and str(node).strip()]
    prices = set()
    for i, node in enumerate(visible):
        label = ' '.join(str(node).split())
        m = re.search(r'Единый\s+плат[её]ж|Оплата\s+сразу|Полная\s+стоимость', label, re.I)
        if not m:
            continue
        # Ignore label text outside the purchase selector and unrelated copies.
        # Limit scanning to the nearest short textual region; never search the
        # entire page for a price, nor fall back to a catalogue "от" amount.
        pieces = [label[m.end():].strip()] if label[m.end():].strip() else []
        for following in visible[i+1:i+9]:
            part = ' '.join(str(following).split())
            if re.search(r'настроить\s+оплату|оплата\s+частями|рассроч|кредит|купить|в\s+корзину|самовывоз|доставка', part, re.I):
                break
            pieces.append(part)
            combined = ' '.join(pieces)
            if len(combined) > 145:
                break
            # First amount, explicitly denominated in BYN/rubles, wins.
            # Following values may be crossed-out prices, so do not require
            # the surrounding container to contain only one amount.
            hit = MONEY.search(combined.replace('\u202f', ' '))
            if hit:
                prefix = combined[:hit.start()]
                if re.search(r'\bот\s*$', prefix, re.I) or EXCLUDE.search(prefix):
                    break
                try:
                    value = Decimal(hit.group(1).replace(' ', '').replace('\xa0', '').replace(',', '.'))
                except InvalidOperation:
                    break
                if value.is_finite() and value > 0:
                    prices.add(value)
                break
    if len(prices) != 1:
        return sku, None, f'expected one explicit single-payment price, found {sorted(prices)}'
    return sku, next(iter(prices)), None
