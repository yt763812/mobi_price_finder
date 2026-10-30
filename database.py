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
            cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS last_price_import_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS delivery_workdays INTEGER CHECK (delivery_workdays >= 0)")
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
            cur.execute("""CREATE TABLE IF NOT EXISTS supplier_excel_profiles (
                supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                sheet_name TEXT NOT NULL,
                header_row INTEGER NOT NULL,
                mapping JSONB NOT NULL,
                column_fingerprint JSONB NOT NULL,
                PRIMARY KEY(supplier_id, sheet_name)
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
            cur.execute("""INSERT INTO app_settings (key, value)
                VALUES ('usd_byn_rate', '3.01') ON CONFLICT (key) DO NOTHING""")
            cur.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS ram_gb INTEGER")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_offers_model
                ON offers(model, storage_gb, color)
            """)

    # Accelerates substring prefilter without changing stored offers.
    # Optional: some managed PostgreSQL roles cannot install extensions.
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_offers_original_trgm ON offers USING gin (original_name gin_trgm_ops)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_offers_model_trgm ON offers USING gin (model gin_trgm_ops)")
    except psycopg.Error:
        # The SQL prefilter still works without the optional acceleration index.
        pass


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
        "ram_gb": product.get("ram_gb"),
        "color": product.get("color"),
        "region": product.get("region"),
        "sim": product.get("sim"),
        "condition": sorted(set(product.get("condition") or [])),
    }
    if not identity["model"]:
        identity["unrecognized_name"] = " ".join(
            product.get("original", "").casefold().split()
        )
    if identity["ram_gb"] is None:
        del identity["ram_gb"]  # Backward-compatible signatures for old offers
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
                    storage_gb, color, region, sim, condition_tags, warnings, ram_gb,
                    price, currency, tax_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s
                )
                ON CONFLICT (supplier_id, signature) DO UPDATE SET
                    original_name = EXCLUDED.original_name,
                    warnings = EXCLUDED.warnings,
                    ram_gb = EXCLUDED.ram_gb,
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
                product.get("ram_gb"), amount, currency, tax_status,
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


def export_supplier_offers(supplier_id):
    """Return the entire persisted snapshot for a supplier, without search limits.

    Includes even stale and unrecognized offers; used for operator audit only.
    """
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError('Invalid supplier ID')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.id, o.original_name, o.brand, o.model, o.storage_gb,
                       o.ram_gb, o.color, o.region, o.sim, o.condition_tags,
                       o.warnings, o.price, o.currency, o.tax_status,
                       o.created_at, o.updated_at,
                       (o.updated_at >= now() - s.validity_hours * INTERVAL '1 hour') AS is_fresh
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.supplier_id = %s
                ORDER BY o.original_name, o.id
            """, (supplier_id,))
            return cur.fetchall()



def iter_supplier_offers(supplier_id, batch_size=2000):
    """Stream the audit snapshot using a server-side cursor, bounded in RAM."""
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError('Invalid supplier ID')
    with _connect() as conn:
        # A named cursor keeps the result on PostgreSQL instead of fetchall().
        with conn.cursor(name='supplier_audit_export') as cur:
            cur.execute("""
                SELECT o.id, o.original_name, o.brand, o.model, o.storage_gb,
                       o.ram_gb, o.color, o.region, o.sim, o.condition_tags,
                       o.warnings, o.price, o.currency, o.tax_status,
                       o.created_at, o.updated_at,
                       (o.updated_at >= now() - s.validity_hours * INTERVAL '1 hour') AS is_fresh
                FROM offers o
                JOIN suppliers s ON s.id = o.supplier_id
                WHERE o.supplier_id = %s
                ORDER BY o.original_name, o.id
            """, (supplier_id,))
            while True:
                chunk = cur.fetchmany(batch_size)
                if not chunk:
                    break
                yield from chunk


def list_suppliers():
    """Show registered suppliers without exposing database credentials."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, validity_hours, currency, tax_status, last_price_import_at, delivery_workdays FROM suppliers ORDER BY name")
            return cur.fetchall()


def get_supplier(supplier_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, validity_hours, currency, tax_status, last_price_import_at, delivery_workdays FROM suppliers WHERE id=%s", (supplier_id,))
            return cur.fetchone()


def rename_supplier(supplier_id, new_name):
    """Rename a supplier without changing its ID or its saved offers."""
    name = new_name.strip() if isinstance(new_name, str) else ''
    if not name or len(name) > 120 or '\n' in name:
        raise ValueError('Название должно быть одной строкой (до 120 символов).')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM suppliers WHERE lower(name) = lower(%s) AND id <> %s', (name, supplier_id))
            if cur.fetchone():
                raise ValueError('Поставщик с таким названием уже существует.')
            try:
                cur.execute('UPDATE suppliers SET name = %s WHERE id = %s RETURNING id', (name, supplier_id))
            except psycopg.errors.UniqueViolation:
                raise ValueError('Поставщик с таким названием уже существует.') from None
            if not cur.fetchone():
                return None
    return get_supplier(supplier_id)


def delete_supplier(supplier_id):
    """Atomically remove supplier offers and the supplier itself."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM suppliers WHERE id = %s FOR UPDATE', (supplier_id,))
            if not cur.fetchone():
                return False
            cur.execute('DELETE FROM offers WHERE supplier_id = %s', (supplier_id,))
            cur.execute('DELETE FROM suppliers WHERE id = %s', (supplier_id,))
    return True


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


def set_supplier_delivery_days(supplier_id, days):
    """Set delivery lead time; None means unknown."""
    if days is not None and (not isinstance(days, int) or days < 0 or days > 365):
        raise ValueError('Срок доставки должен быть от 0 до 365 рабочих дней.')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE suppliers SET delivery_workdays=%s WHERE id=%s', (days, supplier_id))
            if cur.rowcount != 1:
                raise ValueError('Поставщик не найден.')
    return get_supplier(supplier_id)


def _prepare_import(rows):
    """Keep distinct original supplier lines; collapse only exact repeated names."""
    unique = {}
    rejected = []
    for product, raw_price in rows:
        try:
            amount = Decimal(str(raw_price).replace(',', '.'))
            if not amount.is_finite() or amount <= 0:
                raise ValueError('Invalid price')
        except (ValueError, ArithmeticError, __import__('decimal').InvalidOperation):
            rejected.append((product.get('original', ''), 'некорректная цена'))
            continue
        # Deduplicate only byte-for-byte identical source names (ignoring
        # padding around the cell). Normalising internal whitespace/case here
        # silently collapses distinct catalogue rows, e.g. two-space vs
        # one-space supplier names or Maunfeld vs MAUNFELD.
        original = str(product.get('original') or '').strip()
        if not original:
            rejected.append(('', 'отсутствует название'))
            continue
        signature = hashlib.sha256(original.encode('utf-8')).hexdigest()
        if signature not in unique or amount < unique[signature][1]:
            unique[signature] = (product, amount)
    return unique, rejected


def _replace_supplier_offers(supplier_id, rows, currency=None, tax_status=None):
    """All-or-nothing snapshot replacement. Failed/empty imports keep old data."""
    unique, rejected = _prepare_import(rows)
    if not unique:
        raise ValueError('В прайсе нет ни одной строки с корректной ценой; старые данные сохранены.')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT currency, tax_status FROM suppliers WHERE id=%s FOR UPDATE', (supplier_id,))
            supplier = cur.fetchone()
            if not supplier:
                raise ValueError('Поставщик не найден')
            if currency is not None and supplier['currency'] != currency:
                raise ValueError('Настройки валюты изменились; загрузи файл снова.')
            if tax_status is not None and supplier['tax_status'] != tax_status:
                raise ValueError('Настройки НДС изменились; загрузи файл снова.')
            currency = supplier['currency']
            tax_status = supplier['tax_status']
            if currency not in ('USD', 'BYN', 'RUB', 'EUR') or tax_status not in ('with_vat', 'without_vat'):
                raise ValueError('Сначала настрой валюту и НДС поставщика.')
            cur.execute('DELETE FROM offers WHERE supplier_id=%s', (supplier_id,))
            # Batch inserts reduce database round trips, especially on Railway.
            insert_sql = """
                INSERT INTO offers (supplier_id, signature, original_name, brand, model,
                    storage_gb, color, region, sim, condition_tags, warnings, ram_gb,
                    price, currency, tax_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
            """
            def insert_parameters():
                for signature, (product, amount) in unique.items():
                    yield (supplier_id, signature, product['original'], product.get('brand'),
                           product.get('model'), product.get('storage_gb'), product.get('color'),
                           product.get('region'), product.get('sim'),
                           json.dumps(product.get('condition') or [], ensure_ascii=False),
                           json.dumps(product.get('warnings') or [], ensure_ascii=False),
                           product.get('ram_gb'), amount, currency, tax_status)
            batch = []
            for params in insert_parameters():
                batch.append(params)
                if len(batch) >= 1000:
                    cur.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                cur.executemany(insert_sql, batch)
            # Same transaction as the snapshot: a failed import cannot move the timestamp.
            cur.execute('UPDATE suppliers SET last_price_import_at = now() WHERE id = %s', (supplier_id,))
    return {'saved': len(unique), 'skipped': len(rejected),
            'duplicates': len(rows) - len(unique) - len(rejected),
            'rejected': rejected[:5],
            'unrecognized_rows': sum(not p.get('model') for p, _ in unique.values())}


def save_price_list(supplier_id, rows):
    """Replace supplier's full text price list, keeping unrecognized products."""
    return _replace_supplier_offers(supplier_id, rows)


def save_excel_rows(supplier_id, rows, currency, tax_status):
    """Replace supplier's full Excel price list within one DB transaction."""
    return _replace_supplier_offers(supplier_id, rows, currency, tax_status)


def search_offers(anchor=None, *, model=None, name_terms=None):
    """Search fresh offers using index-friendly SQL before Python validation.

    For recognized iPhones, exact structured model matches are retained along
    with supplier names carrying generation and distinguishing tokens. The final
    Python product parser remains the authority on model, capacity and color.
    Other searches retain the original conservative anchor behavior.
    """
    def escape_like(value):
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    with _connect() as conn:
        with conn.cursor() as cur:
            conditions = []
            params = []
            if model and name_terms:
                # Indexed equality on model, plus a trigram-indexable fallback
                # for descriptions that the importer could not categorize.
                name_checks = []
                for term in name_terms:
                    name_checks.append("o.original_name ILIKE %s ESCAPE '\\'")
                conditions.append('(' + ' AND '.join(name_checks) + ')')
                params.extend('%' + escape_like(term) + '%' for term in name_terms)
                filter_sql = '(o.model = %s OR ' + conditions[0] + ')'
                params = [model, *params]
            elif anchor:
                pattern = '%' + escape_like(anchor) + '%'
                filter_sql = "(o.original_name ILIKE %s ESCAPE '\\' OR o.model ILIKE %s ESCAPE '\\' OR o.brand ILIKE %s ESCAPE '\\')"
                params = [pattern, pattern, pattern]
            else:
                filter_sql = 'TRUE'
            cur.execute(f"""
                SELECT o.*, s.name AS supplier_name, s.delivery_workdays,
                    TRUE AS is_fresh
                FROM offers o JOIN suppliers s ON s.id=o.supplier_id
                WHERE o.updated_at >= now() - s.validity_hours * INTERVAL '1 hour'
                  AND {filter_sql}
            """, params)
            return cur.fetchall()


def get_excel_profiles(supplier_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT sheet_name, header_row, mapping, column_fingerprint FROM supplier_excel_profiles WHERE supplier_id=%s', (supplier_id,))
            return {row['sheet_name']: row for row in cur.fetchall()}


def save_excel_profiles(supplier_id, profiles):
    """Save mappings only after successful offer import."""
    with _connect() as conn:
        with conn.cursor() as cur:
            for item in profiles:
                cur.execute("""INSERT INTO supplier_excel_profiles(supplier_id,sheet_name,header_row,mapping,column_fingerprint)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(supplier_id,sheet_name) DO UPDATE SET
                    header_row=excluded.header_row, mapping=excluded.mapping,
                    column_fingerprint=excluded.column_fingerprint""",
                    (supplier_id, item['sheet'], item['header_row'],
                     psycopg.types.json.Jsonb(item['mapping']),
                     psycopg.types.json.Jsonb(item['fingerprint'])))


def get_usd_byn_rate():
    """BYN per 1 USD; persisted across Railway restarts."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key='usd_byn_rate'")
            row = cur.fetchone()
    return Decimal(row['value']) if row else Decimal('3.01')


def set_usd_byn_rate(value):
    rate = Decimal(str(value))
    if not rate.is_finite() or rate <= 0 or rate > 1000:
        raise ValueError('Курс должен быть положительным и не более 1000')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO app_settings(key, value) VALUES ('usd_byn_rate', %s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""", (str(rate),))
    return rate
