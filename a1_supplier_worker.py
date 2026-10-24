"""A1 full seven-section snapshot: preview by default, atomic supplier write opt-in.

Run: python -u a1_supplier_worker.py [--commit] [--max-pages 100]
"""
import argparse
import hashlib
import json
import os
from decimal import Decimal
from urllib.parse import urlsplit

from a1_catalog_dryrun import SECTIONS, crawl

SUPPLIER = 'shop.a1.by'
LOCK_KEY = 77504433


def prepare_snapshot(results):
    errors = []
    selected = {}
    stats = []
    for section in SECTIONS:
        result = results.get(section)
        if result is None:
            errors.append(f'{section}: category missing from crawl')
            continue
        issues = result.get('issues') or []
        found = result.get('found', 0)
        expected = result.get('expected_items')
        if issues:
            errors.append(f'{section}: crawl errors: {issues}')
        if expected is None:
            errors.append(f'{section}: missing expected ItemList count')
        elif found != expected:
            errors.append(f'{section}: expected {expected} products, got {found}')
        if found == 0:
            errors.append(f'{section}: empty catalog')
        priced = 0
        unpriced = 0
        for item in result.get('products', []):
            url = item['url']
            p = item.get('price_byn')
            if p is None:
                unpriced += 1
                continue
            try:
                amount = Decimal(p)
                parsed = urlsplit(url)
                if (not amount.is_finite() or amount <= 0 or
                    parsed.scheme != 'https' or parsed.hostname != SUPPLIER or
                    not parsed.path.startswith('/catalog/p/')):
                    raise ValueError('bad URL or price')
            except Exception as exc:
                errors.append(f'{section}: invalid priced product {url}: {exc}')
                continue
            priced += 1
            if url in selected:
                if selected[url]['price_byn'] != str(amount):
                    errors.append(f'{section}: conflicting price across categories for {url}')
                continue
            selected[url] = {**item, 'price_byn':str(amount), 'section':section}
        stats.append({'section': section, 'found':found, 'expected':expected,
                      'priced':priced, 'without_price':unpriced})
    if not selected:
        errors.append('no priced products')
    return {'ready_to_commit':not errors, 'errors':errors, 'statistics':stats,
            'unique_priced':len(selected), 'selected':selected}


def save_supplier(snapshot):
    """Replace only the managed A1 supplier, inside one transaction."""
    if not snapshot['ready_to_commit']:
        raise ValueError('Refusing incomplete A1 snapshot')
    from database import _connect
    from product_parser import parse_product
    prepared = []
    for url, product in snapshot['selected'].items():
        name = product['text'].strip()
        # Anchor text includes promotion and price; extract product title from tail,
        # preserving original until a reliable title selector is available.
        name = __import__('re').sub(r'^.*?(?=(?:Смартфон|Планшет|Ноутбук|Умные часы|Смарт-часы|Наушники)\s)', '', name, flags=__import__('re').I).strip() or name
        if not name:
            raise ValueError(f'Empty product name: {url}')
        parsed = parse_product(name)
        warnings = list(parsed.get('warnings') or [])
        warnings.append('Публичная цена каталога A1; условия для юрлиц не подтверждены')
        if 'promotional_terms' in product.get('flags', []):
            warnings.append('На странице указаны акционные условия')
        if 'price_sequence_review' in product.get('flags', []):
            warnings.append('В карточке более двух цен: требуется проверка')
        signature = hashlib.sha256(('a1:'+url).encode()).hexdigest()
        prepared.append((signature,name,Decimal(product['price_byn']),url,product['section'],parsed,warnings))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS web_supplier_registry (
                shop TEXT PRIMARY KEY,
                supplier_id BIGINT NOT NULL UNIQUE REFERENCES suppliers(id) ON DELETE RESTRICT)''')
            cur.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS source_url TEXT')
            cur.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS web_category TEXT')
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_KEY,))
            cur.execute('SELECT supplier_id FROM web_supplier_registry WHERE shop=%s FOR UPDATE', ('a1',))
            row=cur.fetchone()
            if row:
                sid=row['supplier_id']
                cur.execute('SELECT name FROM suppliers WHERE id=%s FOR UPDATE', (sid,))
                supplier=cur.fetchone()
                if not supplier or supplier['name'] != SUPPLIER:
                    raise RuntimeError('A1 managed supplier changed: refusing to write')
            else:
                cur.execute('SELECT id FROM suppliers WHERE name=%s', (SUPPLIER,))
                if cur.fetchone():
                    raise RuntimeError('Existing unmanaged A1 supplier: refusing adoption')
                cur.execute("INSERT INTO suppliers(name,validity_hours,currency,tax_status) VALUES (%s,48,'BYN','unknown') RETURNING id", (SUPPLIER,))
                sid=cur.fetchone()['id']
                cur.execute('INSERT INTO web_supplier_registry(shop,supplier_id) VALUES (%s,%s)', ('a1',sid))
            cur.execute('DELETE FROM offers WHERE supplier_id=%s', (sid,))
            for sig,name,price,url,section,p,warnings in prepared:
                cur.execute('''INSERT INTO offers (
                    supplier_id,signature,original_name,brand,model,storage_gb,color,region,sim,
                    condition_tags,warnings,ram_gb,price,currency,tax_status,source_url,web_category
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'BYN','unknown',%s,%s)''',(
                    sid,sig,name,p.get('brand'),p.get('model'),p.get('storage_gb'),
                    p.get('color'),p.get('region'),p.get('sim'),
                    json.dumps(p.get('condition') or [],ensure_ascii=False),
                    json.dumps(warnings,ensure_ascii=False),p.get('ram_gb'),price,url,section))
            cur.execute("UPDATE suppliers SET currency='BYN',tax_status='unknown', validity_hours=48,last_price_import_at=now() WHERE id=%s",(sid,))
    return {'supplier_id':sid,'saved':len(prepared)}


def run(max_pages=100, pause=1, timeout=25, commit=False):
    import time
    results={}
    for section in SECTIONS:
        print('START A1',section,flush=True)
        try:
            result=crawl(section,max_pages,timeout,pause)
        except Exception as exc:
            result={'section':section,'issues':[f'{type(exc).__name__}: {exc}'],
                    'expected_items':None,'found':0,'products':[]}
        print('RESULT A1',json.dumps({k:v for k,v in result.items() if k!='products'},ensure_ascii=False),flush=True)
        results[section]=result
        time.sleep(pause)
    snapshot=prepare_snapshot(results)
    summary={k:v for k,v in snapshot.items() if k!='selected'}
    print('A1_SNAPSHOT',json.dumps(summary,ensure_ascii=False),flush=True)
    if not snapshot['ready_to_commit']:
        print('NOT SAVED: incomplete catalog or conflicting prices; existing supplier untouched',flush=True)
        return 2
    if not commit:
        print('PREVIEW ONLY: database unchanged',flush=True)
        return 0
    if os.getenv('A1_SUPPLIER_COMMIT','0')!='1':
        print('NOT SAVED: set A1_SUPPLIER_COMMIT=1 to explicitly allow writes',flush=True)
        return 2
    result=save_supplier(snapshot)
    print('SAVED shop.a1.by',json.dumps(result),flush=True)
    return 0

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--max-pages',type=int,default=100)
    ap.add_argument('--pause',type=float,default=1.0)
    ap.add_argument('--timeout',type=int,default=25)
    ap.add_argument('--commit',action='store_true')
    args=ap.parse_args()
    if not 1<=args.max_pages<=100: ap.error('max-pages must be 1..100')
    raise SystemExit(run(args.max_pages,args.pause,args.timeout,args.commit))
