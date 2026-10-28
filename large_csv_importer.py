"""Streaming, atomic CSV snapshot import for the supplied 27-column Belarusian feed."""
import csv
import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path


REQUIRED = {'Модель', 'Розничная цена'}
MAX_CSV_BYTES = int(os.getenv('MAX_CSV_BYTES', str(1024 * 1024 * 1024)))


def _rows(path):
    # utf-8-sig handles BOM; cp1251 is common in Belarusian supplier exports.
    for encoding in ('utf-8-sig', 'cp1251'):
        try:
            with open(path, 'r', encoding=encoding, newline='') as f:
                sample = f.read(65536)
                f.seek(0)
                reader = csv.DictReader(f, delimiter=';', quotechar='"')
                if not reader.fieldnames or not REQUIRED.issubset(set(reader.fieldnames)):
                    raise ValueError('CSV должен содержать колонки «Модель» и «Розничная цена».')
                yield from reader
            return
        except UnicodeDecodeError:
            if encoding == 'cp1251':
                raise


def import_large_csv(path, supplier_id, expected_tax_status=None):
    """COPY staged offers then atomically replace the chosen supplier's snapshot.

    Other suppliers remain untouched. Incomplete/invalid files roll back entirely.
    A name-based signature matches the existing Excel import semantics.
    """
    from database import _connect
    from product_parser import parse_product
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_CSV_BYTES:
        raise ValueError('CSV не найден или превышен MAX_CSV_BYTES.')
    total = valid = skipped = 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT currency, tax_status FROM suppliers WHERE id=%s FOR UPDATE', (supplier_id,))
            supplier = cur.fetchone()
            if not supplier:
                raise ValueError('Поставщик не найден.')
            if supplier['currency'] != 'BYN':
                raise ValueError('Для этого CSV валюта выбранного поставщика должна быть BYN (настройки /settings).')
            if supplier['tax_status'] not in ('with_vat', 'without_vat'):
                raise ValueError('Для поставщика нужно настроить НДС.')
            if expected_tax_status and supplier['tax_status'] != expected_tax_status:
                raise ValueError('Настройки НДС поставщика изменились в процессе импорта.')
            cur.execute('''CREATE TEMP TABLE csv_stage (
                signature text, original_name text, brand text, model text,
                storage_gb integer, color text, region text, sim text,
                condition_tags jsonb, warnings jsonb, ram_gb integer,
                price numeric(16,2)
            ) ON COMMIT DROP''')
            with cur.copy('''COPY csv_stage (signature, original_name, brand, model,
                storage_gb, color, region, sim, condition_tags, warnings, ram_gb, price)
                FROM STDIN''') as copier:
                for record in _rows(path):
                    total += 1
                    name = (record.get('Модель') or '').strip()
                    raw = (record.get('Розничная цена') or '').strip().replace(' ', '').replace('\xa0', '').replace(',', '.')
                    if not name or not raw:
                        skipped += 1
                        continue
                    try:
                        amount = Decimal(raw)
                        if not amount.is_finite() or amount <= 0 or amount > Decimal('99999999999999.99'):
                            raise ValueError('invalid price')
                    except (ValueError, InvalidOperation):
                        skipped += 1
                        continue
                    item = parse_product(name)
                    valid += 1
                    copier.write_row((hashlib.sha256(name.encode('utf-8')).hexdigest(), name,
                        item.get('brand') or (record.get('Бренд') or '').strip() or None,
                        item.get('model'), item.get('storage_gb'), item.get('color'),
                        item.get('region'), item.get('sim'),
                        json.dumps(item.get('condition') or [], ensure_ascii=False),
                        json.dumps(item.get('warnings') or [], ensure_ascii=False),
                        item.get('ram_gb'), amount))
            if total == 0 or valid == 0 or valid / total < 0.5:
                raise ValueError('CSV пустой или слишком много некорректных строк; старый прайс сохранён.')
            # DISTINCT ON mirrors existing logic: same exact name -> cheapest price.
            cur.execute('''CREATE TEMP TABLE csv_distinct ON COMMIT DROP AS
                SELECT DISTINCT ON (signature) * FROM csv_stage
                ORDER BY signature, price ASC''')
            cur.execute('SELECT count(*) AS n FROM csv_distinct')
            saved = cur.fetchone()['n']
            cur.execute('DELETE FROM offers WHERE supplier_id=%s', (supplier_id,))
            cur.execute('''INSERT INTO offers (supplier_id, signature, original_name, brand, model,
                    storage_gb, color, region, sim, condition_tags, warnings, ram_gb,
                    price, currency, tax_status)
                SELECT %s, signature, original_name, brand, model, storage_gb, color,
                    region, sim, condition_tags, warnings, ram_gb,
                    price, 'BYN', %s FROM csv_distinct''',
                (supplier_id, supplier['tax_status']))
            cur.execute('UPDATE suppliers SET last_price_import_at=now() WHERE id=%s', (supplier_id,))
    return {'total': total, 'saved': saved, 'skipped': skipped, 'duplicates': valid - saved}
