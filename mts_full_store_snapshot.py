"""Transactionally replace only scraper-owned shop.mts.by supplier offers."""
import hashlib
import json
from decimal import Decimal
from urllib.parse import urlsplit


def replace_full_store(merged):
    from database import _connect
    from product_parser import parse_product
    from web_supplier_snapshot import SUPPLIER_NAME
    entries = list(merged['selected'].values())
    if not entries:
        raise ValueError('Refusing empty full-store snapshot')
    prepared = []
    seen = set()
    for record in entries:
        item, category, url = record['item'], record['section'], record['url']
        price = Decimal(str(item.price))
        if (not item.sku or not item.name or item.currency != 'BYN' or
            urlsplit(url).scheme != 'https' or urlsplit(url).hostname != SUPPLIER_NAME or
            not price.is_finite() or price <= 0 or not category):
            raise ValueError(f'Invalid offer {url}')
        sig = hashlib.sha256(('mts:' + url).encode()).hexdigest()
        if sig in seen:
            raise ValueError(f'Duplicate URL: {url}')
        seen.add(sig)
        parsed = parse_product(item.name)
        warnings = list(parsed.get('warnings') or [])
        warnings.append('Публичная цена МТС; условия покупки юридическим лицом не подтверждены')
        if getattr(item,'approximate',False):
            warnings.append('Цена указана «от»')
        prepared.append((sig,item,category,url,price,parsed,warnings))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS web_supplier_registry (
                shop TEXT PRIMARY KEY,
                supplier_id BIGINT NOT NULL UNIQUE REFERENCES suppliers(id) ON DELETE RESTRICT)''')
            cur.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS source_url TEXT')
            cur.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS web_category TEXT')
            # Same advisory lock as original snapshot writer.
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (77504432,))
            cur.execute('SELECT supplier_id FROM web_supplier_registry WHERE shop=%s FOR UPDATE', ('mts',))
            record = cur.fetchone()
            if record:
                sid = record['supplier_id']
                cur.execute('SELECT name FROM suppliers WHERE id=%s FOR UPDATE', (sid,))
                supplier = cur.fetchone()
                if not supplier or supplier['name'] != SUPPLIER_NAME:
                    raise RuntimeError('Registered MTS supplier changed; refusing write')
            else:
                cur.execute('SELECT id FROM suppliers WHERE name=%s', (SUPPLIER_NAME,))
                if cur.fetchone():
                    raise RuntimeError('Existing unmanaged shop.mts.by supplier; refusing adoption')
                cur.execute("INSERT INTO suppliers(name,validity_hours,currency,tax_status) VALUES (%s,48,'BYN','unknown') RETURNING id", (SUPPLIER_NAME,))
                sid = cur.fetchone()['id']
                cur.execute('INSERT INTO web_supplier_registry(shop,supplier_id) VALUES (%s,%s)', ('mts',sid))
            cur.execute('DELETE FROM offers WHERE supplier_id=%s', (sid,))
            for sig,item,category,url,price,p,warnings in prepared:
                cur.execute('''INSERT INTO offers (
                    supplier_id,signature,original_name,brand,model,storage_gb,color,region,sim,
                    condition_tags,warnings,ram_gb,price,currency,tax_status,source_url,web_category
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'BYN','unknown',%s,%s)''', (
                    sid,sig,item.name,p.get('brand'),p.get('model'),p.get('storage_gb'),
                    p.get('color'),p.get('region'),p.get('sim'),
                    json.dumps(p.get('condition') or [],ensure_ascii=False),
                    json.dumps(warnings,ensure_ascii=False),p.get('ram_gb'),price,url,category))
            cur.execute("UPDATE suppliers SET currency='BYN',tax_status='unknown', validity_hours=48,last_price_import_at=now() WHERE id=%s", (sid,))
    return {'supplier_id':sid,'saved':len(prepared)}
