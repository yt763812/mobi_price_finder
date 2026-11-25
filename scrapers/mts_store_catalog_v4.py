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
from scrapers.mts_modern import discover_cards, detail_offer, _product_link

CRAWLER_VERSION = "mts-v3.9-recheck-coverage-20261125"


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



def discover_catalogue_preorders(html, page_url):
    """Identify product tiles by an explicit PREORDER purchase action.

    MTS currently displays a BYN price on some preorder-only items, so price
    visibility is NOT proof an item is purchasable. Only accept an action in a
    compact, single-product tile; never match a page-wide advertisement.
    """
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main') or soup.select_one('[role=main]') or soup
    found = {}
    for action in main.find_all(['button', 'input', 'a', 'span', 'div']):
        if action.name == 'input':
            label = action.get('value', '')
        else:
            # Divs can encompass entire pages; permit only small leaf labels.
            if action.find(['button', 'input', 'a', 'span', 'div']):
                continue
            label = action.get_text(' ', strip=True)
        if not re.fullmatch(r'\s*предзаказ(?:ать)?\s*', str(label), re.I):
            continue
        for ancestor in list(action.parents)[:10]:
            if ancestor is main or ancestor.name in ('body', 'html'):
                break
            text = ancestor.get_text(' ', strip=True)
            if len(text) > 2500:
                break
            candidates = {}
            for a in ancestor.select('a[href]'):
                name = ' '.join(a.get_text(' ', strip=True).split())
                if not 7 <= len(name) <= 180:
                    continue
                url = _product_link(a.get('href', ''), page_url)
                if url and valid_store_product_url(url, page_url):
                    candidates.setdefault(url, name)
            if len(candidates) == 1:
                found.update(candidates)
                break
    return found


def catalogue_page_url(catalogue_url, number):
    """Only construct safe sequential ?page=N links on the same catalogue route."""
    parts = urlsplit(catalogue_url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k != 'page']
    params.append(('page', str(number)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))


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
    seen_preorder_urls = set()
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
    # Do not turn transient detail timeouts into permanent crawl failures until
    # the whole catalogue has been scanned and a delayed recovery was tried.
    transient_failures = {}
    recovery_attempts = max(0, min(2, int(os.getenv('MTS_FAILED_DETAIL_RECHECKS', '1'))))

    def is_transient_detail_error(error):
        if not error or not error.startswith('detail HTTP/parse failure:'):
            return False
        return bool(re.search(
            r'timed out|timeout|HTTP Error (?:408|429|500|502|503|504)|'
            r'connection reset|RemoteDisconnected|temporar|incomplete read|'
            r'network is unreachable|connection aborted|connection refused', error, re.I))

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
            if is_transient_detail_error(error) and recovery_attempts:
                transient_failures[key] = (card, error)
            else:
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
            # A catalogue page can consist entirely of "Предзаказ" cards.
            # The priced-card selector deliberately excludes those cards.
            preorders = discover_catalogue_preorders(html, page_url)
            # CTA status overrides any displayed BYN amount: MTS can show an
            # indicative price even though the only action is "Предзаказ".
            preorder_urls = set(preorders)
            parser.cards = [c for c in parser.cards
                            if urljoin(page_url, c.url or '') not in preorder_urls]
            modern = [c for c in modern if c.url not in preorder_urls]
            if not parser.cards and not modern and not preorders:
                # A terminal blank page is valid only when it follows consecutive,
                # populated pages and there is independent evidence it is the end.
                # An explicit advertised next page inside a known range must NEVER
                # be silently treated as complete.
                terminal_candidate = n > 1 and visited and max(visited) == n - 1
                if terminal_candidate:
                    if (expected_page_count is not None and n <= expected_page_count):
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
            page_urls.update(preorders)
            if page_urls and page_urls.issubset(seen_urls):
                if expected_page_count is not None and n <= expected_page_count:
                    incomplete = True
                    pagination_anomalies.append(
                        f'repeated catalogue page {n} before end {expected_page_count}')
                print(f'MTS catalog-only {category}: terminal repeated page {n}; '
                      f'counting {len(visited)} genuine pages', flush=True)
                break
            visited.add(n)
            unseen_preorders = set(preorders) - seen_preorder_urls
            seen_preorder_urls.update(unseen_preorders)
            preorder_skipped += len(unseen_preorders)
            seen_urls.update(page_urls)
            cards_total += len(parser.cards) + len(modern) + len(unseen_preorders)
            if unseen_preorders:
                print(f'MTS_PREORDER_PAGE section={category} page={n} '
                      f'preorders_on_page={len(unseen_preorders)} '
                      f'preorders_total={preorder_skipped}', flush=True)
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
                    # Legacy cards can explicitly identify preorder status too.
                    if re.search(r'\bпредзаказ\b', card.availability or '', re.I):
                        if url not in seen_preorder_urls:
                            seen_preorder_urls.add(url)
                            preorder_skipped += 1
                        continue
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
            last_page_card_count = len(parser.cards) + len(modern) + len(unseen_preorders)
            for href in links.hrefs:
                next_url = urljoin(page_url, href)
                next_n = page_number(next_url, catalogue_url)
                if next_n and next_n not in visited and next_n not in (v for v, _ in todo):
                    todo.append((next_n, next_url))
            todo.sort(key=lambda x: x[0])
            # Preorder-only pages may omit usable pagination links. Continue
            # through the site's advertised page count rather than interpreting
            # no priced cards as the end of catalogue. If no page count exists,
            # only probe after a genuinely full populated page as before.
            if not todo:
                next_n = n + 1
                advertised_pages_remain = (expected_page_count is not None
                                           and next_n <= expected_page_count)
                full_page_without_count = (expected_page_count is None and
                    (len(parser.cards) >= 18 or len(modern) >= 12))
                if (advertised_pages_remain or full_page_without_count) and next_n not in visited:
                    todo.append((next_n, catalogue_page_url(catalogue_url, next_n)))
                    pagination_probe_count += 1
                    if not advertised_pages_remain:
                        pagination_anomalies.append(f'no pagination after full page {n}; probed page {next_n}')
                    print(f'MTS catalog-only {category}: PAGINATION_PROBE page={next_n} '
                          f'advertised_pages={expected_page_count} '
                          f'preorders={len(unseen_preorders)}', flush=True)
        # Before checking the final result, process every queued detail response.
        while futures:
            drain_details(block=True)
    finally:
        executor.shutdown(wait=True)
    # A later retry is useful when a transient server slowdown affected an
    # otherwise complete catalogue. Retry only network-type failures; malformed
    # prices and title mismatches remain genuine errors and are never hidden.
    if transient_failures:
        print(f'MTS_DETAIL_RECHECK_START section={category} '
              f'failed={len(transient_failures)} rounds={recovery_attempts}', flush=True)
        for round_no in range(1, recovery_attempts + 1):
            if not transient_failures:
                break
            remaining = {}
            for key, (card, previous_error) in transient_failures.items():
                sku, exact, error = get_verified(card)
                if error is None and sku and isinstance(exact, Decimal) and exact.is_finite() and exact > 0:
                    products[key] = CatalogOffer(sku, card.name, card.url, exact,
                                                 'BYN', False, '', '')
                    print(f'MTS_DETAIL_RECOVERED section={category} round={round_no} '
                          f'url={card.url}', flush=True)
                elif error == 'MTS_PREORDER_SKIP':
                    preorder_skipped += 1
                else:
                    error = error or 'detail returned missing/invalid SKU or price'
                    remaining[key] = (card, error)
            transient_failures = remaining
            print(f'MTS_DETAIL_RECHECK_ROUND section={category} round={round_no} '
                  f'unresolved={len(transient_failures)}', flush=True)
        for card, error in transient_failures.values():
            issues.append((card.url, f'unresolved after deferred recheck: {error}'))
    if expected_page_count is not None and len(visited) < expected_page_count:
        incomplete = True
        pagination_anomalies.append(
            f'only {len(visited)} of {expected_page_count} advertised catalogue pages reached')
    # The advertised total includes items without an active selling price.
    # These count toward coverage ONLY when explicitly detected as preorders.
    # Keep a conservative tolerance of less than one normal page for mutable
    # catalogue counters; do not declare a large unexplained gap complete.
    if (advertised_total is not None and first_page_size is not None
            and cards_total + first_page_size < advertised_total):
        incomplete = True
        pagination_anomalies.append(
            f'advertised {advertised_total} cards, only {cards_total} accounted for '
            f'(including {preorder_skipped} confirmed preorders)')
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
