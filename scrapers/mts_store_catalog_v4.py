"""Fast, catalogue-only MTS PRICE OBSERVATIONS, without visiting product details.

This is a diagnostic mode, not proof of B2B eligibility or absence of conditions.
"""
from dataclasses import dataclass
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from html.parser import HTMLParser
from decimal import Decimal
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from scrapers.mts import CardsParser, read_price
from scrapers.mts_crawl import PaginationLinks, page_number
from scrapers.mts_modern import discover_cards, detail_offer


def valid_store_product_url(url, catalogue_url):
    """Permit cross-subsection product links on MTS, never external/navigational links."""
    parsed = urlsplit(url)
    host = urlsplit(catalogue_url)
    if parsed.scheme != 'https' or parsed.hostname != 'shop.mts.by' or parsed.hostname != host.hostname:
        return False
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    # Product pages have one or more category segments followed by a product slug.
    segments = [x for x in parsed.path.split('/') if x]
    if len(segments) < 2 or not all(segments) or parsed.query or parsed.fragment:
        return False
    if segments[-1].lower() in ('index.php', 'catalog', 'page'):
        return False
    return True


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
    pagination_probe_count: int = 0
    pagination_anomalies: list = None


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


def crawl_mts_catalog(fetcher, catalogue_url, category, max_pages=50, max_products=3000,
                      detail_workers=4, detail_cache=None):
    if not (1 <= max_pages <= 100 and 1 <= max_products <= 10000):
        raise ValueError('Invalid crawl limits')
    if not 1 <= detail_workers <= 6:
        raise ValueError('detail_workers must be 1..6')
    # Shared across sections in one run; bounded concurrency, no unbounded fanout.
    detail_cache = detail_cache if detail_cache is not None else {}
    cache_lock = Lock()
