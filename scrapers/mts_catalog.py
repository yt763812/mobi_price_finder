"""Fast, catalogue-only MTS PRICE OBSERVATIONS, without visiting product details.

This is a diagnostic mode, not proof of B2B eligibility or absence of conditions.
"""
from dataclasses import dataclass
import hashlib
import re
from html.parser import HTMLParser
from decimal import Decimal
from urllib.parse import urljoin, urlparse
from scrapers.mts import CardsParser, read_price
from scrapers.mts_crawl import PaginationLinks, page_number


@dataclass(frozen=True)
class CatalogOffer:
    sku: str
    name: str
    url: str
    price: Decimal
    currency: str
    approximate: bool
    availability: str
    analytics_price: str


@dataclass
class CatalogResult:
    products: list
    pages: int
    cards: int
    issues: list
    incomplete: bool
    conflicts: list = None


class _VisiblePrices(HTMLParser):
    """Conservatively extract advertised prices from explicitly marked price elements."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.active = None
        self.parts = []
        self.found = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").lower().split())
        if self.active is not None:
            self.depth += 1
            return
        # Only explicitly price-marked product UI, not arbitrary body text,
        # recommendations, analytics, or JSON-LD. Prefer no confirmation
        # over importing the wrong price.
        is_price = any("price" in cls for cls in classes)
        excluded = any(any(x in cls for x in ("old", "cross", "strike", "install", "month", "credit")) for cls in classes)
        if is_price and not excluded and tag in ("span", "div", "p", "strong"):
            self.active = tag
            self.depth = 1
            self.parts = []

    def handle_data(self, value):
        if self.active is not None:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if self.active is None:
            return
        self.depth -= 1
        if self.depth == 0:
            value = " ".join("".join(self.parts).split())
            if "руб" in value.lower() and "мес" not in value.lower():
                parsed = read_price(value)
                if parsed is not None:
                    self.found.append(parsed)
            self.active = None


def _verify_conflict(fetcher, offer, alternatives):
    """Resolve only if the product page has one unambiguous price.

    If extraction is absent or ambiguous, exclude rather than guessing.
    """
    try:
        html = fetcher.get(offer.url)
        parser = _VisiblePrices()
        parser.feed(html)
        prices = set(parser.found)
        if len(prices) == 1:
            price = next(iter(prices))
            if price in alternatives:
                return price, "confirmed from single displayed detail price"
        return None, f"detail has ambiguous/nonmatching visible prices: {sorted(prices)}"
    except Exception as exc:
        return None, f"detail verification failed: {type(exc).__name__}: {exc}"


def crawl_mts_catalog(fetcher, catalogue_url, category, max_pages=50, max_products=3000):
    if not (1 <= max_pages <= 100 and 1 <= max_products <= 10000):
        raise ValueError('Invalid crawl limits')
    todo = [(1, catalogue_url)]
    visited = set()
    seen_urls = set()
    products = {}
    issues = []
    conflict_prices = {}
    conflicted = set()
    conflicts = []
    cards_total = 0
    incomplete = False
    root = urlparse(catalogue_url)
    while todo:
        n, page_url = todo.pop(0)
        if n in visited:
            continue
        if len(visited) >= max_pages:
            incomplete = True
            break
        html = fetcher.get(page_url)
        parser = CardsParser()
        parser.feed(html)
        if not parser.cards:
            raise RuntimeError(f'No MTS catalogue cards on {page_url}')
        # Out-of-range MTS page numbers may return the first page again.
        # A page containing NO unseen product URLs is a terminal duplicate,
        # not a real extra catalogue page. Never count its cards.
        page_urls = {urljoin(page_url, c.url or '') for c in parser.cards if c.url}
        if page_urls and page_urls.issubset(seen_urls):
            print(f'MTS catalog-only {category}: terminal repeated page {n}; '
                  f'counting {len(visited)} genuine pages', flush=True)
            break
        visited.add(n)
        seen_urls.update(page_urls)
        cards_total += len(parser.cards)
        if cards_total > max_products:
            incomplete = True
            break
        for card in parser.cards:
            url = urljoin(page_url, card.url or '')
            dest = urlparse(url)
            if not (card.sku and card.name and card.url and dest.scheme == 'https'
                    and dest.netloc == root.netloc and dest.path.startswith(root.path)):
                issues.append((card.sku or '?', 'invalid SKU/name/link'))
                continue
            price = read_price(card.displayed_price)
            if price is None:
                issues.append((card.sku, f'no full advertised price: {card.displayed_price!r}'))
                continue
            offer = CatalogOffer(card.sku, card.name, url, price, 'BYN',
                                 card.approximate, card.availability, card.ga_price)
            # The site's GA SKU is not necessarily unique: several different
            # links may share it. Disambiguate with stable URL-derived keys,
            # while rejecting contradictory prices for the SAME URL.
            # All keys are stable across page order and daily runs.
            key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            if key in conflicted:
                conflict_prices[key].add(offer.price)
                continue
            old = products.get(key)
            if old is not None and old.price != offer.price:
                # Withhold this URL until the crawl completes. Other errors
                # still block the atomic snapshot as before.
                conflict_prices[key] = {old.price, offer.price}
                conflicted.add(key)
                del products[key]
                conflicts.append((card.sku, url, old))
                continue
            products[key] = offer
        print(f'MTS catalog-only {category}: pages={len(visited)}, cards={cards_total}, '
              f'priced={len(products)}, issues={len(issues)}', flush=True)
        links = PaginationLinks()
        links.feed(html)
        for href in links.hrefs:
            next_url = urljoin(page_url, href)
            next_n = page_number(next_url, catalogue_url)
            if next_n and next_n not in visited and next_n not in (v for v, _ in todo):
                todo.append((next_n, next_url))
        todo.sort(key=lambda x: x[0])
    # One detail request per conflicting URL, not one per product.
    for sku, url, original in conflicts:
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        choices = conflict_prices[key]
        resolved, reason = _verify_conflict(fetcher, original, choices)
        if resolved is None:
            print(f'MTS catalog-only {category}: CONFLICT EXCLUDED sku={sku} url={url} '
                  f'prices={sorted(choices)}; {reason}', flush=True)
        else:
            from dataclasses import replace
            products[key] = replace(original, price=resolved)
            print(f'MTS catalog-only {category}: CONFLICT RESOLVED sku={sku} url={url} '
                  f'price={resolved}; {reason}', flush=True)
    return CatalogResult(list(products.values()), len(visited), cards_total, issues, incomplete, conflicts)
