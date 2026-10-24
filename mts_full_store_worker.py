"""Opt-in full-store writer sharing lock with current MTS worker."""
import os
import traceback

def run_full_store(max_pages=100, report=None):
    import psycopg
    from web_worker import LOCK_KEY
    from mts_full_store import collect_full_store
    from mts_full_store_snapshot import replace_full_store
    dsn = os.environ['DATABASE_URL']
    def emit(s):
        print(s, flush=True)
        if report:
            report(s)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,)).fetchone()[0]:
            emit('MTS: другой сбор уже выполняется')
            return 3
        try:
            merged, summary = collect_full_store(max_pages=max_pages, emit=emit)
            if not summary['ready_to_commit']:
                emit('NOT SAVED: full-store validation failed; previous supplier untouched')
                return 2
            # Preview defaults to no-write; only explicit environment gate permits a commit.
            if os.getenv('MTS_FULL_STORE_COMMIT','0') != '1':
                emit('PREVIEW ONLY: set MTS_FULL_STORE_COMMIT=1 to enable supplier replacement')
                return 0
            saved = replace_full_store(merged)
            emit(f'SAVED shop.mts.by: {saved["saved"]} unique offers supplier_id={saved["supplier_id"]}')
            return 0
        except Exception:
            traceback.print_exc()
            emit('NOT SAVED: full-store failure')
            return 2
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))
