"""Fast, catalogue-only MTS PRICE OBSERVATIONS, without visiting product details.

This is a diagnostic mode, not proof of B2B eligibility or absence of conditions.
"""
from dataclasses import dataclass
from bs4 import BeautifulSoup
import os
import time
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock
from html.parser import HTMLParser
from decimal import Decimal
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from scrapers.mts import CardsParser, read_price
from scrapers.mts_crawl import PaginationLinks, page_number
from scrapers.mts_modern import discover_cards, detail_offer

CRAWLER_VERSION = "mts-v3.6-streaming-20261124"


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
    preorder_skipped: int = 0


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
    if not 1 <= detail_workers <= 8:
        raise ValueError('detail_workers must be 1..8')
    print(f'MTS_CRAWLER_ENTER {CRAWLER_VERSION} section={category}', flush=True)
    # Shared across sections in one run; bounded concurrency, no unbounded fanout.
    detail_cache = detail_cache if detail_cache is not None else {}
    cache_lock = Lock()
    todo = [(1, catalogue_url)]
    visited = set()
    seen_urls = set()
    products = {}
    issues = []
    conflict_prices = {}
    conflicted = set()
    conflicts = []
    cards_total = 0
    preorder_skipped = 0
    incomplete = False
    pagination_probe_count = 0
    pagination_anomalies = []
    expected_page_count = None
    last_page_links = set()
    last_page_card_count = 0
    first_page_size = None
    advertised_total = None
    # One persistent pool per category; overlap catalogue pagination with detail requests.
    # A bounded queue prevents uncontrolled fan-out and preserves shared rate limiting.
    queued_detail_keys = set()
    futures = {}

    def get_verified(card):
        url = card.url
        key = (url, card.name)
        with cache_lock:
            cached = detail_cache.get(key)
        if cached is not None:
            return cached
        try:
            detail_html = fetcher.get(url)
            answer = detail_offer(detail_html, url, card.name)
            # Only skip a confirmed pre-order that has no verified single-payment
            # price. This is never used to mask timeouts or parser failures.
            if isinstance(answer, tuple) and len(answer) == 3:
                sku, price, error = answer
                if price is None and error and 'expected one explicit single-payment price' in error:
                    soup = BeautifulSoup(detail_html, 'lxml')
                    main = soup.select_one('main') or soup.select_one('[role=main]') or soup
                    if re.search(r'\bпредзаказ\b', main.get_text(' ', strip=True), re.I):
                        answer = (sku, None, 'MTS_PREORDER_SKIP')
            if not isinstance(answer, tuple) or len(answer) != 3:
                return (None, None, f'detail parser returned unexpected result: {type(answer).__name__}')
        except Exception as exc:
            # Do not cache failures; a transient error may be recoverable.
            return (None, None, f'detail HTTP/parse failure: {type(exc).__name__}: {exc}')
        with cache_lock:
            detail_cache[key] = answer
        return answer


    def consume_detail(future):
        nonlocal preorder_skipped
        key, card = futures.pop(future)
        sku, exact, error = future.result()  # get_verified always returns a 3-tuple
        if error is None and (not sku or not isinstance(exact, Decimal) or exact <= 0):
            error = 'detail returned missing/invalid verified SKU or price'
        if error == 'MTS_PREORDER_SKIP':
            preorder_skipped += 1
        elif error is not None:
            issues.append((sku or card.url, error))
        else:
            products[key] = CatalogOffer(sku, card.name, card.url, exact,
                                         'BYN', False, '', '')

    def drain_details(*, block):
        if not futures:
            return
        if block:
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
        else:
            done = {f for f in futures if f.done()}
        for future in done:
            consume_detail(future)

    executor = ThreadPoolExecutor(max_workers=detail_workers)
    try:
        while todo:
            started_page = time.monotonic()
            n, page_url = todo.pop(0)
            if n in visited:
                continue
            if len(visited) >= max_pages:
                incomplete = True
                break
            html = fetcher.get(page_url)
            # The site exposes a count such as 'Показано 1–12 из 546'.
            # If pagination silently truncates, never commit a partial snapshot.
            if n == 1:
                match = re.search(r'Показано\s*1\s*[–—-]\s*(\d+)\s*из\s*(\d+)',
                                  re.sub(r'<[^>]*>', ' ', html), re.I)
                if match:
                    page_size, total = map(int, match.groups())
                    if page_size and total >= page_size:
                        expected_page_count = (total + page_size - 1) // page_size
                        first_page_size = page_size
                        advertised_total = total
            parser = CardsParser()
            parser.feed(html)
            modern = []
            if not parser.cards:
                modern = discover_cards(html, page_url)
            if not parser.cards and not modern:
                # A terminal blank page is valid only when it follows consecutive,
                # populated pages and there is independent evidence it is the end.
                # An explicit advertised next page inside a known range must NEVER
                # be silently treated as complete.
                terminal_candidate = n > 1 and visited and max(visited) == n - 1
                if terminal_candidate:
                    if (advertised_total is not None and first_page_size is not None
                        and cards_total + first_page_size < advertised_total):
                        incomplete = True
                        pagination_anomalies.append(
                            f'catalogue stopped at page {n-1}, but advertised '
                            f'{advertised_total} vs {cards_total} observed cards')
                    # Explicit next-page links can be stale; retain a warning.
                    if n in last_page_links:
                        pagination_anomalies.append(f'empty page {n} was linked from page {n-1}')
                    print(f'MTS catalog-only {category}: terminal empty page {n}; '
                          f'counting {len(visited)} genuine pages; '
                          f'incomplete={incomplete}', flush=True)
                    break
                raise RuntimeError(f'No MTS product cards (legacy or modern) on {page_url}')
            # Out-of-range MTS page numbers may return the first page again.
            # A page containing NO unseen product URLs is a terminal duplicate,
            # not a real extra catalogue page. Never count its cards.
            page_urls = {urljoin(page_url, c.url or '') for c in parser.cards if c.url}
            page_urls.update(c.url for c in modern)
            if page_urls and page_urls.issubset(seen_urls):
                if expected_page_count is not None and n < expected_page_count:
                    raise RuntimeError(f'Repeated page {n} before advertised last page {expected_page_count}')
                print(f'MTS catalog-only {category}: terminal repeated page {n}; '
                      f'counting {len(visited)} genuine pages', flush=True)
                break
            visited.add(n)
            seen_urls.update(page_urls)
            cards_total += len(parser.cards) + len(modern)
            if cards_total > max_products:
                incomplete = True
                break
            for card in parser.cards:
                url = urljoin(page_url, card.url or '')
                dest = urlparse(url)
                if not (card.sku and card.name and card.url and dest.scheme == 'https'
                        and valid_store_product_url(url, catalogue_url)):
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
            pending = []
            for card in modern:
                if not valid_store_product_url(card.url, catalogue_url):
                    issues.append(('?', f'invalid modern product URL: {card.url}'))
                    continue
                key = hashlib.sha256(card.url.encode('utf-8')).hexdigest()
                if key not in products and key not in conflicted:
                    pending.append((key, card))
            for key, card in pending:
                # No duplicate requests for the same URL, even when pages overlap.
                if key in queued_detail_keys:
                    continue
                queued_detail_keys.add(key)
                futures[executor.submit(get_verified, card)] = (key, card)
                # Bound outstanding work: avoid loading the full catalogue into memory.
                if len(futures) >= detail_workers * 4:
                    drain_details(block=True)
            # Opportunistically reap completed items; do NOT wait for the slowest
            # product on this page before discovering the next catalogue page.
            drain_details(block=False)
            print(f'MTS catalog-only {category}: pages={len(visited)}, cards={cards_total}, '
                  f'priced={len(products)}, preorders_skipped={preorder_skipped}, '
                  f'issues={len(issues)}, pending={len(futures)}, page_seconds={time.monotonic() - started_page:.1f}', flush=True)
            links = PaginationLinks()
            links.feed(html)
            last_page_links = {p for href in links.hrefs
                               if (p := page_number(urljoin(page_url, href), catalogue_url))}
            last_page_card_count = len(parser.cards) + len(modern)
            for href in links.hrefs:
                next_url = urljoin(page_url, href)
                next_n = page_number(next_url, catalogue_url)
                if next_n and next_n not in visited and next_n not in (v for v, _ in todo):
                    todo.append((next_n, next_url))
            todo.sort(key=lambda x: x[0])
            # Some responses omit pagination links despite showing a full page.
            # Probe the next sequential page rather than declaring completion.
            # A repeated response is handled at the top of the loop, while an
            # empty/invalid probe fails closed (no supplier write based on it).
            if not todo and (len(parser.cards) >= 18 or len(modern) >= 12):
                next_n = n + 1
                parts = urlsplit(catalogue_url)
                params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                          if k != 'page']
                params.append(('page', str(next_n)))
                candidate = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                        urlencode(params), ''))
                if next_n not in visited:
                    todo.append((next_n, candidate))
                    pagination_probe_count += 1
                    pagination_anomalies.append(f'no pagination after full page {n}; probed page {next_n}')
                    print(f'MTS catalog-only {category}: PAGINATION_PROBE page={next_n} '
                          f'(page {n} had {len(parser.cards) + len(modern)} cards)', flush=True)
        # Before checking the final result, process every queued detail response.
        while futures:
            drain_details(block=True)
    finally:
        executor.shutdown(wait=True)
    if (expected_page_count and len(visited) < expected_page_count
            and advertised_total is not None and cards_total + first_page_size < advertised_total):
        incomplete = True
        pagination_anomalies.append(
            f'only {len(visited)} of {expected_page_count} advertised catalogue pages reached')
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
    result = CatalogResult(list(products.values()), len(visited), cards_total, issues, incomplete, conflicts, pagination_probe_count, pagination_anomalies, preorder_skipped)
    print(f'MTS_CRAWLER_RETURN {CRAWLER_VERSION} section={category} pages={result.pages} cards={result.cards} priced={len(result.products)} preorders_skipped={result.preorder_skipped} issues={len(result.issues)} incomplete={result.incomplete}', flush=True)
    return result
