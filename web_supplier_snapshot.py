"""Atomic, ownership-checked full snapshot for shop.mts.by in normal suppliers/offers.

Requires existing database.init_db() schema. Never adopts a manually-created supplier.
"""
import hashlib
import json
from decimal import Decimal
from urllib.parse import urlparse

SUPPLIER_NAME = 'shop.mts.by'
EXPECTED = frozenset({'smartphones', 'tablets', 'watches', 'headphones', 'laptops'})


def prepare_snapshot(by_category):
    """Validate full all-category input before touching PostgreSQL."""
    from product_parser import parse_product
    if set(by_category) != EXPECTED:
        raise ValueError('All five MTS categories required for atomic replacement')
    prepared = {}
    per_category = {}
    for category, items in by_category.items():
        if not items:
            raise ValueError(f'Empty MTS category {category}')
        per_category[category] = 0
        for item in items:
            sku = str(item.sku).strip()
            name = str(item.name).strip()
            url = str(item.url).strip()
            p = urlparse(url)
            price = Decimal(str(item.price))
            if (not sku or not name or p.scheme != 'https'
                or p.hostname != 'shop.mts.by' or not price.is_finite() or price <= 0
                or item.currency != 'BYN'):
                raise ValueError(f'Invalid MTS catalogue offer {category}/{sku}')
            # Site analytics SKUs can repeat for different URLs.
            # Use a stable product URL identity across all categories.
            key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            if key in prepared:
                old_url = prepared[key][3]
                raise ValueError(f'Duplicate MTS URL across categories: {old_url}')
            parsed = parse_product(name)
            signature = hashlib.sha256(('mts:' + url).encode('utf-8')).hexdigest()
            warnings = list(parsed.get('warnings') or [])
            warnings.append('Публичная цена каталога МТС; условия закупки для юрлица не подтверждены')
            if getattr(item, 'approximate', False):
                warnings.append('Цена на сайте указана с пометкой «от»')
            prepared[key] = (signature, sku, name, url, parsed, price, warnings)
            per_category[category] += 1
    return prepared, per_category


def replace_shop_supplier(by_category):
    """Create owned supplier once, then atomically replace ONLY its offers."""
    import psycopg
    from database import _connect
    prepared, counts = prepare_snapshot(by_category)
    # Table marks ownership. Existing unmarked suppliers are NEVER adopted.
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS web_supplier_registry (
                shop TEXT PRIMARY KEY,
                supplier_id BIGINT NOT NULL UNIQUE REFERENCES suppliers(id) ON DELETE RESTRICT
            )''')
            cur.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS source_url TEXT')
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (77504432,))
            cur.execute('SELECT supplier_id FROM web_supplier_registry WHERE shop=%s FOR UPDATE', ('mts',))
            registered = cur.fetchone()
            if registered:
                sid = registered['supplier_id']
                cur.execute('SELECT name FROM suppliers WHERE id=%s FOR UPDATE', (sid,))
                supplier = cur.fetchone()
                if not supplier or supplier['name'] != SUPPLIER_NAME:
                    raise RuntimeError('Registered MTS supplier renamed/deleted; aborting')
            else:
                cur.execute('SELECT id FROM suppliers WHERE name=%s', (SUPPLIER_NAME,))
                if cur.fetchone():
                    raise RuntimeError('Supplier shop.mts.by already exists but is not owned by scraper; aborting')
                cur.execute('''INSERT INTO suppliers(name,validity_hours,currency,tax_status)
                    VALUES (%s, 48, 'BYN', 'unknown') RETURNING id''', (SUPPLIER_NAME,))
                sid = cur.fetchone()['id']
                cur.execute('INSERT INTO web_supplier_registry(shop,supplier_id) VALUES (%s,%s)', ('mts',sid))
            cur.execute('DELETE FROM offers WHERE supplier_id=%s', (sid,))
            for signature, sku, name, url, p, price, warnings in prepared.values():
                cur.execute('''INSERT INTO offers (
                    supplier_id,signature,original_name,brand,model,storage_gb,color,region,sim,
                    condition_tags,warnings,ram_gb,price,currency,tax_status,source_url
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'BYN','unknown',%s)''', (
                    sid, signature, name, p.get('brand'), p.get('model'),p.get('storage_gb'),
                    p.get('color'),p.get('region'),p.get('sim'),
                    json.dumps(p.get('condition') or [],ensure_ascii=False),
                    json.dumps(warnings,ensure_ascii=False),p.get('ram_gb'),price,url
                ))
            cur.execute('''UPDATE suppliers SET currency='BYN',tax_status='unknown',
                validity_hours=48,last_price_import_at=now() WHERE id=%s''', (sid,))
    return {'supplier_id': sid, 'saved': len(prepared), 'categories': counts}
