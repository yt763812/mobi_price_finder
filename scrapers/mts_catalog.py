"""Fast, catalogue-only MTS PRICE OBSERVATIONS, without visiting product details.

This is a diagnostic mode, not proof of B2B eligibility or absence of conditions.
"""
from dataclasses import dataclass
import hashlib
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


def crawl_mts_catalog(fetcher, catalogue_url, category, max_pages=50, max_products=3000):
    if not (1 <= max_pages <= 100 and 1 <= max_products <= 10000):
        raise ValueError('Invalid crawl limits')
    todo = [(1, catalogue_url)]
    visited = set()
    seen_urls = set()
    products = {}
    issues = []
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
            old = products.get(key)
            if old is not None and old.price != offer.price:
                issues.append((card.sku, f'conflicting prices for identical URL {url}: '
                                         f'{old.price} vs {offer.price}'))
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
    return CatalogResult(list(products.values()), len(visited), cards_total, issues, incomplete)
