# -*- coding: utf-8 -*-
"""Read-only export of your Onliner B2B price list to an Excel .xlsx file.

Requirements: pip install requests
No PUT/PATCH/DELETE is ever sent.
"""
import getpass
import json
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

import requests

SCRIPT_VERSION = '2026-11-19-verified-2'

TOKEN_URL = 'https://b2bapi.onliner.by/oauth/token'
API_BASE = 'https://price.api.onliner.by'
HEADERS = ['Раздел', 'Бренд', 'Модель', 'productId', 'ID предложения',
           'Артикул', 'Цена, BYN', 'Валюта', 'Наличие', 'Гарантия, мес.',
           'Доставка по городу, дни', 'Доставка по стране, дни',
           'Халва, мес.', 'Безналичная оплата', 'Onliner Prime',
           'Промоцена, BYN', 'Производитель', 'Импортер', 'Сервисные центры',
           'Комментарий']


def request_json(session, method, url, **kwargs):
    try:
        r = session.request(method, url, timeout=60, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f'Ошибка сети: {exc}') from exc
    if not r.ok:
        try:
            details = r.json()
            if isinstance(details, dict):
                details = {k: v for k, v in details.items()
                           if k.lower() not in ('access_token', 'refresh_token', 'client_secret')}
        except ValueError:
            details = r.text[:300]
        raise RuntimeError(f'{method} {url} -> HTTP {r.status_code}: {details}')
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError('API вернул не JSON') from exc


def get_all_positions(session):
    # This endpoint was used with the same API in the previous read-only checks.
    url = API_BASE + '/positions'
    print('Чтение полного прайс-листа Mobistore (GET /positions)...', flush=True)
    result = request_json(session, 'GET', url)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ('positions', 'items', 'data'):
            if isinstance(result.get(key), list):
                return result[key]
    raise RuntimeError('Неожиданный формат ответа /positions. Ничего не записано.')


def fmt_text(x):
    if x is None:
        return ''
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False)
    return str(x)


def as_number(x):
    if x is None or x == '':
        return None
    try:
        n = Decimal(str(x).strip().replace(',', '.'))
        if n.is_finite():
            return float(n)
    except (InvalidOperation, ValueError):
        pass
    return None


def clean_xml(s):
    # Remove invalid XML 1.0 chars; prevent Excel formula injection by storing ALL strings as inlineStr.
    s = ''.join(c for c in str(s) if ord(c) in (9, 10, 13) or
                0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                0x10000 <= ord(c) <= 0x10FFFF)
    return escape(s, {'"': '&quot;'})


def col_name(num):
    out = ''
    while num:
        num, r = divmod(num - 1, 26)
        out = chr(65 + r) + out
    return out


def sheet_xml(rows, widths, money_cols=frozenset(), filter_header=True):
    """Create a real Office Open XML worksheet with frozen header, filters and styled columns."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<sheetFormatPr defaultRowHeight="17"/>', '<cols>']
    for i, width in enumerate(widths, 1):
        parts.append(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>')
    parts.append('</cols><sheetData>')
    for ri, row in enumerate(rows, 1):
        parts.append(f'<row r="{ri}"' + (' ht="30" customHeight="1"' if ri == 1 else '') + '>')
        for ci, value in enumerate(row, 1):
            if value is None or value == '':
                continue
            cell = f'{col_name(ci)}{ri}'
            style = 1 if ri == 1 else (2 if ri % 2 == 0 else 0)
            if ci in money_cols and ri > 1:
                style = 3 if ri % 2 else 4
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parts.append(f'<c r="{cell}" s="{style}"><v>{value}</v></c>')
            else:
                parts.append(f'<c r="{cell}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{clean_xml(value)}</t></is></c>')
        parts.append('</row>')
    parts.append('</sheetData>')
    if filter_header and rows:
        parts.append(f'<autoFilter ref="A1:{col_name(len(rows[0]))}{len(rows)}"/>')
    parts.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>')
    parts.append('</worksheet>')
    return ''.join(parts).encode('utf-8')


def create_xlsx(path, sheets):
    styles = '''<?xml version="1.0" encoding="utf-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="10"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Aptos"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF0F5FA"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="1" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/><xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="2" fontId="0" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/></cellXfs>
</styleSheet>'''
    content_types = ['<?xml version="1.0" encoding="utf-8"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(1, len(sheets) + 1):
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append('</Types>')
    workbook = ['<?xml version="1.0" encoding="utf-8"?>',
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<?xml version="1.0" encoding="utf-8"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ''.join(content_types))
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/styles.xml', styles)
        for i, (name, rows, widths, money_cols) in enumerate(sheets, 1):
            workbook.append(f'<sheet name="{clean_xml(name)}" sheetId="{i}" r:id="rId{i}"/>')
            rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            z.writestr(f'xl/worksheets/sheet{i}.xml', sheet_xml(rows, widths, money_cols))
        workbook.append('</sheets></workbook>')
        rels.append('</Relationships>')
        z.writestr('xl/workbook.xml', ''.join(workbook))
        z.writestr('xl/_rels/workbook.xml.rels', ''.join(rels))


def main():
    print('MOBISTORE — экспорт полного прайс-листа в Excel (ТОЛЬКО ЧТЕНИЕ)', flush=True)
    print('Версия скрипта:', SCRIPT_VERSION, flush=True)
    print('Используй ключи нового приложения Onliner Mobistore, НЕ Zoomos.', flush=True)
    client_id = getpass.getpass('Client ID (ввод скрыт): ').strip()
    client_secret = getpass.getpass('Client Secret (ввод скрыт): ').strip()
    if not client_id or not client_secret:
        sys.exit('Ключи не введены.')
    session = requests.Session()
    session.headers.update({'Accept': 'application/json'})
    token = request_json(session, 'POST', TOKEN_URL, auth=(client_id, client_secret),
                         data={'grant_type': 'client_credentials'})
    access_token = token.get('access_token') if isinstance(token, dict) else None
    if not access_token:
        sys.exit('Авторизация не вернула access_token.')
    print('Авторизация успешна. Токен не отображается и не сохраняется.', flush=True)
    session.headers['Authorization'] = 'Bearer ' + access_token
    items = get_all_positions(session)
    if not all(isinstance(x, dict) for x in items):
        sys.exit('В ответе найдены не-объекты. Экспорт остановлен.')
    print(f'Получено предложений: {len(items)}', flush=True)
    if not items:
        sys.exit('API вернул пустой список. Пустой Excel не создаётся.')

    # The control product was previously confirmed against Mobistore's downloaded XML.
    apple = next((x for x in items if str(x.get('productId')) == '5089271'), None)
    if not apple:
        print('ВНИМАНИЕ: контрольный Apple AirPods Max 2 не найден. Убедись, что использованы ключи Mobistore.', flush=True)
    else:
        print('Контрольный Apple AirPods Max 2: цена', apple.get('price'), 'BYN', flush=True)
        if as_number(apple.get('price')) != 1845.0:
            print('ВНИМАНИЕ: ранее цена была 1845 BYN. Возможно, прайс обновился; проверь магазин.', flush=True)

    items.sort(key=lambda p: (fmt_text(p.get('category')).lower(), fmt_text(p.get('vendor')).lower(), fmt_text(p.get('model')).lower()))
    rows = [HEADERS]
    sections = defaultdict(list)
    by_product = defaultdict(list)
    by_offer = defaultdict(list)
    for p in items:
        cat = fmt_text(p.get('category')) or '(не указан)'
        sections[cat].append(p)
        product_id = fmt_text(p.get('productId'))
        offer_id = fmt_text(p.get('id'))
        if product_id:
            by_product[product_id].append(p)
        if offer_id:
            by_offer[offer_id].append(p)
        rows.append([
            cat, fmt_text(p.get('vendor')), fmt_text(p.get('model')),
            product_id, offer_id, fmt_text(p.get('article')),
            as_number(p.get('price')) if as_number(p.get('price')) is not None else fmt_text(p.get('price')),
            fmt_text(p.get('currency')), fmt_text(p.get('inStock')),
            as_number(p.get('warranty')) if as_number(p.get('warranty')) is not None else fmt_text(p.get('warranty')),
            fmt_text(p.get('deliveryTownTime')), fmt_text(p.get('deliveryCountryTime')),
            fmt_text(p.get('termHalva')), fmt_text(p.get('isCashless')),
            fmt_text(p.get('hasOnlinerPrime')),
            as_number(p.get('pricePromo')) if as_number(p.get('pricePromo')) is not None else fmt_text(p.get('pricePromo')),
            fmt_text(p.get('producer')), fmt_text(p.get('importer')),
            fmt_text(p.get('serviceCenters')), fmt_text(p.get('comment'))
        ])

    summary = [['Раздел', 'Всего позиций', 'В наличии (да)', 'Нет в наличии (нет)', 'Остальное']]
    for name, section_items in sorted(sections.items(), key=lambda x: (-len(x[1]), x[0])):
        counts = Counter(fmt_text(x.get('inStock')).lower() for x in section_items)
        yes = counts.get('да', 0)
        no = counts.get('нет', 0)
        summary.append([name, len(section_items), yes, no, len(section_items) - yes - no])
    summary.append(['ВСЕГО', len(items), sum(fmt_text(x.get('inStock')).lower() == 'да' for x in items),
                    sum(fmt_text(x.get('inStock')).lower() == 'нет' for x in items),
                    sum(fmt_text(x.get('inStock')).lower() not in ('да', 'нет') for x in items)])

    duplicates = [['Тип совпадения', 'Значение', 'Количество предложений', 'Примеры товаров']]
    for key, values in sorted(by_product.items()):
        if len(values) > 1:
            duplicates.append(['Одинаковый productId (может быть несколько предложений)', key, len(values),
                               '; '.join(fmt_text(v.get('model')) for v in values[:3])])
    for key, values in sorted(by_offer.items()):
        if len(values) > 1:
            duplicates.append(['Повторный ID предложения', key, len(values),
                               '; '.join(fmt_text(v.get('model')) for v in values[:3])])
    if len(duplicates) == 1:
        duplicates.append(['Совпадений не найдено', '', '', ''])

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    outfile = Path(__file__).resolve().parent / f'mobistore_positions_{timestamp}.xlsx'
    create_xlsx(outfile, [
        ('Все товары', rows, [27, 19, 49, 16, 22, 24, 17, 12, 15, 18, 22, 23, 14, 21, 16, 19, 44, 44, 44, 44], {7, 16}),
        ('Сводка по разделам', summary, [39, 20, 22, 25, 19], set()),
        ('Возможные дубли', duplicates, [49, 29, 24, 65], set()),
    ])
    print('Excel создан:', outfile, flush=True)
    print('Разделов:', len(sections), '| Возможных совпадений:', len(duplicates) - 1, flush=True)
    print('Google Pixel Buds Pro Coral в прайсе:',
          'ДА' if any(str(p.get('productId')) == '3593709' for p in items) else 'НЕТ')
    print('Никаких изменений в магазине не выполнено.')


if __name__ == '__main__':
    if '--check' in sys.argv:
        print('ПРОВЕРКА: скрипт целый, импорты работают, main() доступна.', flush=True)
        print('Версия:', SCRIPT_VERSION, flush=True)
        print('Строк в файле:', len(Path(__file__).read_text(encoding='utf-8-sig').splitlines()), flush=True)
        sys.exit(0)
    try:
        main()
    except Exception as exc:
        print('ОШИБКА:', exc, file=sys.stderr, flush=True)
        sys.exit(1)
