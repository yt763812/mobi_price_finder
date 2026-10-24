"""On-demand USD selling rate from BSB Bank on Myfin Minsk.

No background polling. Fail closed if the bank quote cannot be identified safely.
"""
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from urllib.request import Request, urlopen

MYFIN_URL = 'https://myfin.by/currency/minsk'


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        elif tag in ('tr', 'section', 'article'):
            self.parts.append('\n')
        elif tag in ('td', 'th', 'div', 'span', 'a', 'p', 'br'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('tr', 'section', 'article'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def parse_bsb_usd_sale(html: str) -> Decimal:
    parser = _VisibleText()
    parser.feed(html)
    text = re.sub(r'[ \t\xa0]+', ' ', ''.join(parser.parts))
    # Bank summary row: name, USD buy/sell, EUR buy/sell, RUB buy/sell.
    # Locate the bank name precisely; NEVER use another bank or a best market rate.
    names = list(re.finditer(r'(?<!\w)БСБ\s*Банк(?!\w)', text, re.IGNORECASE))
    for match in names:
        segment = text[match.end(): match.end() + 320].split('\n', 1)[0]
        # Ignore navigation/filter entries, which do not have a quote.
        # Only decimals between plausible bounds for BYN per 1 USD qualify.
        nums = []
        for item in re.finditer(r'(?<![\d.,])(\d+[.,]\d{2,4})(?![\d.,])', segment):
            try:
                price = Decimal(item.group(1).replace(',', '.'))
            except InvalidOperation:
                continue
            if Decimal('1') < price < Decimal('20'):
                nums.append(price)
            if len(nums) >= 2:
                break
        if len(nums) == 2 and nums[0] <= nums[1] and nums[1] - nums[0] < Decimal('1'):
            return nums[1]  # USD: bank sells (BYN for buying one dollar)
    raise ValueError('Не удалось надёжно найти курс продажи USD БСБ Банка на Myfin.')


def fetch_bsb_usd_sale() -> Decimal:
    request = Request(MYFIN_URL, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; PriceFinder/1.0)',
        'Accept': 'text/html',
        'Accept-Language': 'ru-RU,ru;q=0.9',
    })
    with urlopen(request, timeout=12) as response:
        if response.status != 200:
            raise ValueError(f'Myfin ответил HTTP {response.status}')
        html = response.read(3_000_000).decode('utf-8', errors='replace')
    return parse_bsb_usd_sale(html)
