"""PostgreSQL storage for Mobi Price Finder.

Importing this module does not connect to the database or create tables.
Call init_db() explicitly after DATABASE_URL is configured.
"""

import hashlib
import json
import os
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)


def init_db():
    """Create the first schema. Safe to call again on startup."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS suppliers (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    telegram_user_id BIGINT UNIQUE,
                    validity_hours INTEGER NOT NULL DEFAULT 72
                        CHECK (validity_hours > 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'UNKNOWN'")
            cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS tax_status TEXT NOT NULL DEFAULT 'unknown'")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS offers (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
                    signature TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    brand TEXT,
                    model TEXT,
                    storage_gb INTEGER,
                    color TEXT,
                    region TEXT,
                    sim TEXT,
                    condition_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
                    price NUMERIC(16, 2) CHECK (price IS NULL OR price >= 0),
                    currency TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
                    tax_status TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (tax_status IN ('without_vat', 'with_vat', 'unknown')),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(supplier_id, signature)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_offers_model
                ON offers(model, storage_gb, color)
            """)


def add_supplier(name, telegram_user_id=None, validity_hours=72):
    """Return an existing or new supplier ID; don't silently reassign Telegram IDs."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Supplier name is required")
    if validity_hours <= 0:
        raise ValueError("validity_hours must be positive")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO suppliers(name, telegram_user_id, validity_hours, currency)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                RETURNING id
            """, (name.strip(), telegram_user_id, validity_hours, 'UNKNOWN'))
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute("SELECT id FROM suppliers WHERE name = %s", (name.strip(),))
            return cur.fetchone()["id"]


def _signature(product):
    """Unknown characteristics stay distinct from known ones.

    Unknown-model items use the full source name, to avoid merging unrelated
    products that happen to have the same memory/color.
    """
    identity = {
        "brand": product.get("brand"),
        "model": product.get("model"),
        "storage_gb": product.get("storage_gb"),
        "color": product.get("color"),
        "region": product.get("region"),
        "sim": product.get("sim"),
        "condition": sorted(set(product.get("condition") or [])),
    }
    if not identity["model"]:
        identity["unrecognized_name"] = " ".join(
            product.get("original", "").casefold().split()
        )
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_offer(supplier_id, product, price, currency=None, tax_status="unknown"):
    """Insert/update one specific offer; never delete other supplier positions.

    price: number/string or None for 'price on request'.
    currency: explicit ISO code or None (never assume USD).
    product: dictionary returned by product_parser.parse_product().
    """
    if tax_status not in ("without_vat", "with_vat", "unknown"):
        raise ValueError("Invalid tax_status")
    amount = None if price is None else Decimal(str(price).replace(",", "."))
    if amount is not None and (not amount.is_finite() or amount < 0):
        raise ValueError("Price must be a finite nonnegative number")
    if currency is not None:
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("currency must be an ISO three-letter code")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO offers (
                    supplier_id, signature, original_name, brand, model,
                    storage_gb, color, region, sim, condition_tags, warnings,
                    price, currency, tax_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s
                )
                ON CONFLICT (supplier_id, signature) DO UPDATE SET
                    original_name = EXCLUDED.original_name,
                    warnings = EXCLUDED.warnings,
                    price = EXCLUDED.price,
                    currency = EXCLUDED.currency,
                    tax_status = EXCLUDED.tax_status,
                    updated_at = now()
                RETURNING id
            """, (
                supplier_id,
                _signature(product),
                product["original"],
                product.get("brand"), product.get("model"),
                product.get("storage_gb"), product.get("color"),
                product.get("region"), product.get("sim"),
                json.dumps(product.get("condition") or [], ensure_ascii=False),
                json.dumps(product.get("warnings") or [], ensure_ascii=False),
                amount, currency, tax_status,
            ))
            return cur.fetchone()["id"]


def get_offers(model=None):
    """Read stored offers with per-supplier freshness metadata."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.*, s.name AS supplier_name,
                       (o.updated_at >= now() - s.validity_hours * INTERVAL '1 hour')
                           AS is_fresh
                FROM offers AS o
                JOIN suppliers AS s ON s.id = o.supplier_id
                WHERE (%s::text IS NULL OR o.model = %s::text)
                ORDER BY o.updated_at DESC
            """, (model, model))
            return cur.fetchall()



def get_supplier_offers(supplier_id, limit=20):
    """Read the latest stored offers of one supplier, newest first."""
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError("Invalid supplier ID")
    limit = max(1, min(int(limit), 50))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.original_name, o.model, o.storage_gb, o.color,
                       o.region, o.sim, o.price, o.currency, o.tax_status,
                       o.updated_at,
                       (o.updated_at >= now() - s.validity_hours * INTERVAL '1 hour')
                           AS is_fresh
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.supplier_id = %s
                ORDER BY o.updated_at DESC, o.id DESC
                LIMIT %s
            """, (supplier_id, limit))
            return cur.fetchall()


def list_suppliers():
    """Show registered suppliers without exposing database credentials."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, validity_hours, currency, tax_status FROM suppliers ORDER BY name")
            return cur.fetchall()


def get_supplier(supplier_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, validity_hours, currency, tax_status FROM suppliers WHERE id=%s", (supplier_id,))
            return cur.fetchone()


def update_supplier_settings(supplier_id, currency=None, tax_status=None):
    if currency is not None and currency not in ('USD', 'BYN', 'RUB', 'EUR'):
        raise ValueError('Unsupported currency')
    if tax_status is not None and tax_status not in ('without_vat', 'with_vat', 'unknown'):
        raise ValueError('Invalid tax status')
    with _connect() as conn:
        with conn.cursor() as cur:
            if currency is not None:
                cur.execute('UPDATE suppliers SET currency=%s WHERE id=%s', (currency, supplier_id))
            if tax_status is not None:
                cur.execute('UPDATE suppliers SET tax_status=%s WHERE id=%s', (tax_status, supplier_id))
    return get_supplier(supplier_id)


def save_price_list(supplier_id, rows):
    """Save eligible offers atomically; lowest duplicate wins *within this batch*.

    Rows are [(parsed_product, raw_price), ...]. Prices and VAT status are
    captured from the supplier at import time and never silently recomputed.
    """
    supplier = get_supplier(supplier_id)
    if not supplier:
        raise ValueError('Supplier not found')
    if supplier['tax_status'] == 'unknown':
        raise ValueError('Set supplier VAT status first')
    if supplier['currency'] not in ('USD', 'BYN', 'RUB', 'EUR'):
        raise ValueError('Set supplier currency first')
    by_signature = {}
    rejected = []
    for product, raw_price in rows:
        if not product.get('model') or not product.get('storage_gb'):
            rejected.append((product['original'], 'модель или память не распознана'))
            continue
        if product.get('warnings'):
            rejected.append((product['original'], 'необходима проверка характеристик'))
            continue
        try:
            amount = Decimal(str(raw_price).replace(',', '.'))
            if not amount.is_finite() or amount <= 0:
                raise ValueError('Invalid price')
        except (ValueError, ArithmeticError, __import__('decimal').InvalidOperation):
            rejected.append((product['original'], 'некорректная цена'))
            continue
        signature = _signature(product)
        if signature not in by_signature or amount < by_signature[signature][1]:
            by_signature[signature] = (product, amount)

    with _connect() as conn:
        with conn.cursor() as cur:
            for signature, (product, amount) in by_signature.items():
                cur.execute('''
                    INSERT INTO offers (
                        supplier_id, signature, original_name, brand, model,
                        storage_gb, color, region, sim, condition_tags, warnings,
                        price, currency, tax_status
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s, %s
                    )
                    ON CONFLICT (supplier_id, signature) DO UPDATE SET
                        original_name = EXCLUDED.original_name,
                        warnings = EXCLUDED.warnings,
                        price = EXCLUDED.price,
                        currency = EXCLUDED.currency,
                        tax_status = EXCLUDED.tax_status,
                        updated_at = now()
                ''', (
                    supplier_id, signature, product['original'],
                    product.get('brand'), product.get('model'),
                    product.get('storage_gb'), product.get('color'),
                    product.get('region'), product.get('sim'),
                    json.dumps(product.get('condition') or [], ensure_ascii=False),
                    json.dumps(product.get('warnings') or [], ensure_ascii=False),
                    amount, supplier['currency'], supplier['tax_status'],
                ))
    return {'saved': len(by_signature), 'skipped': len(rejected),
            'duplicates': len(rows) - len(by_signature) - len(rejected),
            'rejected': rejected[:5]}
