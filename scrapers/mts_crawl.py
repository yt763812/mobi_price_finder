"""Public MTS catalogue crawler. Does not write to DB.

Follow only explicitly linked `?page=N` catalogue pages. If a page fails,
abort the crawl instead of pretending that the result is complete.
"""
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse
from scrapers.mts import CardsParser
from scrapers.mts_detail import detail_price


class PaginationLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.hrefs.append(href)


def page_number(url, catalogue_url):
    parsed, root = urlparse(url), urlparse(catalogue_url)
    if parsed.scheme != 'https' or parsed.netloc != root.netloc or parsed.path != root.path:
        return None
    query = parse_qs(parsed.query)
    if set(query) != {'page'} or len(query['page']) != 1:
        return None
    value = query['page'][0]
    if not value.isdecimal() or not (2 <= int(value) <= 1000):
        return None
    return int(value)


@dataclass
class CrawlResult:
    products: list
    pages: int
    cards: int
    issues: list
    incomplete: bool


def crawl_mts(fetcher, catalogue_url, category, max_pages=50, max_products=3000):
    if not (1 <= max_pages <= 100 and 1 <= max_products <= 10000):
        raise ValueError('Invalid crawl limits')
    todo = [(1, catalogue_url)]
    visited = set()
    products = {}
    issues = []
    total_cards = 0
    incomplete = False
    while todo:
        number, page_url = todo.pop(0)
        if number in visited:
            continue
        if len(visited) >= max_pages:
            incomplete = True
            break
        # Exceptions propagate: never mark a failed page as successfully crawled.
        html = fetcher.get(page_url)
        visited.add(number)
        parser = CardsParser()
        parser.feed(html)
        if not parser.cards:
            raise RuntimeError(f'No product cards on catalogue page {page_url}; aborting')
        total_cards += len(parser.cards)
        if total_cards > max_products:
            incomplete = True
            break
        for card in parser.cards:
            url = urljoin(page_url, card.url)
            parsed = urlparse(url)
            if not (card.sku and card.name and card.url and parsed.scheme == 'https'
                    and parsed.netloc == urlparse(catalogue_url).netloc):
                issues.append((card.sku or '?', 'invalid card data'))
                continue
            key = (card.sku, url)
            if key in products:
                continue
            try:
                document = fetcher.get(url)
                product, error = detail_price(document, url, card.name, card.sku, category)
                if product is None:
                    issues.append((card.sku, error))
                else:
                    products[key] = product
            except Exception as exc:
                issues.append((card.sku, f'{type(exc).__name__}: {exc}'))
        links = PaginationLinks()
        links.feed(html)
        for href in links.hrefs:
            next_url = urljoin(page_url, href)
            n = page_number(next_url, catalogue_url)
            if n and n not in visited and n not in (p for p, _ in todo):
                todo.append((n, next_url))
        todo.sort(key=lambda pair: pair[0])
    return CrawlResult(list(products.values()), len(visited), total_cards, issues, incomplete)
