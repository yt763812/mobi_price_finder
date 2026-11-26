"""Opt-in full-store writer sharing lock with current MTS worker."""
import os
import traceback

def run_full_store(max_pages=100, report=None):
    import psycopg
    from web_worker import LOCK_KEY
    from mts_full_store_snapshot import replace_full_store, merge_partial_full_store
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
            # Short diagnostic mode: no supplier snapshot can be written, even
            # when MTS_FULL_STORE_COMMIT=1 by mistake.
            if os.getenv('MTS_FAST_PROBE', '0') == '1':
                from mts_fast_probe import run_fast_probe
                return run_fast_probe(emit=emit)
            if os.getenv('MTS_DIAGNOSTIC_ONLY', '0') == '1':
                from mts_diagnostic import run_diagnostic
                return run_diagnostic(emit=emit)
            from mts_full_store import collect_full_store
            merged, summary = collect_full_store(max_pages=max_pages, emit=emit)
            allow_partial = os.getenv('MTS_PARTIAL_SAVE', '0') == '1'
            if not summary['ready_to_commit'] and not (allow_partial and summary.get('partial_ready')):
                emit('NOT SAVED: full-store validation failed; partial mode not enabled or no usable prices')
                return 2
            # Preview defaults to no-write; only explicit environment gate permits a commit.
            if os.getenv('MTS_FULL_STORE_COMMIT','0') != '1':
                emit('PREVIEW ONLY: set MTS_FULL_STORE_COMMIT=1 to enable supplier replacement')
                return 0
            if allow_partial:
                saved = merge_partial_full_store(merged)
                emit('MTS_PARTIAL_SAVED ' + str(saved))
            elif summary['ready_to_commit']:
                saved = replace_full_store(merged)
                emit(f'SAVED shop.mts.by: {saved["saved"]} unique offers supplier_id={saved["supplier_id"]}')
            else:
                emit('NOT SAVED: incomplete; MTS_PARTIAL_SAVE=1 required for safe merge')
                return 2
            return 0
        except Exception:
            traceback.print_exc()
            emit('NOT SAVED: full-store failure')
            return 2
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))
