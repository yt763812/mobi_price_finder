"""Fast, catalogue-only MTS PRICE OBSERVATIONS, without visiting product details.

This is a diagnostic mode, not proof of B2B eligibility or absence of conditions.
"""
from dataclasses import dataclass
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
        visited.add(n)
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
            old = products.get(card.sku)
            if old is not None and (old.url != offer.url or old.price != offer.price):
                issues.append((card.sku, 'same SKU has different URL or price across catalogue pages'))
                continue
            products[card.sku] = offer
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
