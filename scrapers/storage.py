"""Separate web-offer storage. Does not modify supplier/offers tables."""
import json
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_offers (
 shop TEXT NOT NULL, sku TEXT NOT NULL, url TEXT NOT NULL,
 category TEXT NOT NULL, name TEXT NOT NULL,
 price NUMERIC(16,2) NOT NULL CHECK (price > 0),
 currency TEXT NOT NULL DEFAULT 'BYN', availability TEXT,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(shop, sku, url)
);
CREATE TABLE IF NOT EXISTS web_price_history (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 shop TEXT NOT NULL, sku TEXT NOT NULL, url TEXT NOT NULL,
 old_price NUMERIC(16,2), new_price NUMERIC(16,2) NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_web_price_history_lookup
 ON web_price_history(shop, sku, observed_at DESC);
CREATE TABLE IF NOT EXISTS web_scrape_runs (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 shop TEXT NOT NULL, category TEXT NOT NULL,
 status TEXT NOT NULL, found INTEGER NOT NULL,
 details TEXT, finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
'''

def init(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()

def save_observations(conn, shop, category, products):
    """Upsert observed products; never delete unseen products (possibly partial crawl)."""
    if not products:
        raise ValueError('No trustworthy products: database unchanged')
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            for product in products:
                if product.shop != shop or product.category != category:
                    raise ValueError('Mixed shop/category')
                cur.execute('SELECT price FROM web_offers WHERE shop=%s AND sku=%s AND url=%s FOR UPDATE',
                            (shop, product.sku, product.url))
                previous = cur.fetchone()
                if previous is None or previous['price'] != product.price:
                    cur.execute('''INSERT INTO web_price_history(shop,sku,url,old_price,new_price)
                                   VALUES (%s,%s,%s,%s,%s)''',
                                (shop, product.sku, product.url,
                                 previous['price'] if previous else None, product.price))
                cur.execute('''INSERT INTO web_offers(shop,sku,url,category,name,price,currency,availability)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT(shop,sku,url) DO UPDATE SET
                               category=excluded.category, name=excluded.name, price=excluded.price,
                               currency=excluded.currency, availability=excluded.availability,
                               observed_at=now()''',
                            (shop, product.sku, product.url, category, product.name,
                             product.price, product.currency, product.availability))
            cur.execute('INSERT INTO web_scrape_runs(shop,category,status,found) VALUES (%s,%s,%s,%s)',
                        (shop, category, 'partial_observations', len(products)))
