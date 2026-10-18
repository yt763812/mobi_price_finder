"""Safe, preview-only XLSX inspection. No database writes."""
import io
import re
from decimal import Decimal, InvalidOperation
from openpyxl import load_workbook

NAME_HEADERS = ('модель', 'наименование', 'название товара', 'товар', 'product', 'model', 'item', 'description')
PRICE_HEADERS = ('цена', 'закупочная цена', 'опт', 'оптовая цена', 'price', 'wholesale', 'dealer price')
BRAND_HEADERS = ('бренд', 'brand', 'производитель')
CATEGORY_HEADERS = ('категория', 'category')
BAD_PRICE = ('итого', 'total', 'сумма', 'розница', 'retail', 'ррц')
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_ROWS = 30000


def _clean(value):
    return str(value).strip() if value is not None else ''


def _header(value):
    return re.sub(r'\s+', ' ', _clean(value).casefold())


def _indices(row, aliases):
    return [i for i, v in enumerate(row) if _header(v) in aliases]


def _price(value):
    if isinstance(value, bool) or value is None:
        return None
    raw = _clean(value).replace('\u00a0', '').replace(' ', '').replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', raw):
        return None
    try:
        result = Decimal(raw)
        return result if result > 0 else None
    except InvalidOperation:
        return None


def inspect_xlsx(data):
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('Файл больше 12 МБ. Пришли файл поменьше.')
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            header_row = None
            mapping = None
            ambiguous = False
            for row_num, row in enumerate(ws.iter_rows(min_row=1, max_row=25, values_only=True), 1):
                names = _indices(row, NAME_HEADERS)
                prices = _indices(row, PRICE_HEADERS)
                brands = _indices(row, BRAND_HEADERS)
                categories = _indices(row, CATEGORY_HEADERS)
                if names and prices:
                    header_row = row_num
                    mapping = {'name': names[0], 'price': prices[0],
                               'brand': brands[0] if brands else None,
                               'category': categories[0] if categories else None}
                    ambiguous = len(names) > 1 or len(prices) > 1
                    break
            if mapping is None:
                sheets.append({'sheet': ws.title, 'status': 'unknown'})
                continue
            count = valid = no_price = 0
            samples = []
            for row in ws.iter_rows(min_row=header_row + 1, max_row=min(ws.max_row or MAX_ROWS, MAX_ROWS), values_only=True):
                def val(idx):
                    return row[idx] if idx is not None and idx < len(row) else None
                name = _clean(val(mapping['name']))
                if not name:
                    continue
                count += 1
                price = _price(val(mapping['price']))
                if price is None:
                    no_price += 1
                    continue
                valid += 1
                brand = _clean(val(mapping['brand']))
                if len(samples) < 4:
                    samples.append(f'{brand + " " if brand else ""}{name[:85]} — {price}')
            sheets.append({'sheet': ws.title, 'status': 'preview', 'header_row': header_row,
                           'mapping': mapping, 'ambiguous': ambiguous,
                           'named': count, 'valid': valid, 'no_price': no_price,
                           'samples': samples,
                           'truncated': bool(ws.max_row and ws.max_row > MAX_ROWS)})
        return sheets
    finally:
        wb.close()
