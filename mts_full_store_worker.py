"""Opt-in atomic FULL MTS supplier snapshot writer.

A successful run replaces the supplier's complete offer set. Missing products
are removed. Incomplete pagination, unresolved detail errors, empty sections,
or a failed category block the replacement so a technical outage cannot erase
an entire category by accident.
"""
import os
import traceback


def run_full_store(max_pages=100, report=None):
    import psycopg
    from web_worker import LOCK_KEY
    from mts_full_store_snapshot import replace_full_store

    dsn = os.environ['DATABASE_URL']

    def emit(message):
        print(message, flush=True)
        if report:
            report(message)

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,)).fetchone()[0]:
            emit('MTS: другой сбор уже выполняется')
            return 3
        try:
            # Diagnostic modes are always read-only, regardless of COMMIT flag.
            if os.getenv('MTS_FAST_PROBE', '0') == '1':
                from mts_fast_probe import run_fast_probe
                return run_fast_probe(emit=emit)
            if os.getenv('MTS_DIAGNOSTIC_ONLY', '0') == '1':
                from mts_diagnostic import run_diagnostic
                return run_diagnostic(emit=emit)

            from mts_full_store import collect_full_store
            merged, summary = collect_full_store(max_pages=max_pages, emit=emit)

            # Destructive replacement is intentionally gated by a complete crawl.
            # MTS_PARTIAL_SAVE is no longer used: this release never merges stale
            # offers from a previous snapshot into today's current-price list.
            if not summary.get('ready_to_commit') or not merged or not merged.get('selected'):
                emit('NOT SAVED: full replacement blocked; one or more parent catalogues had pagination/detail/section errors')
                emit('MTS_REPLACEMENT_BLOCKERS ' + str(summary.get('failed', [])))
                return 2

            if os.getenv('MTS_FULL_STORE_COMMIT', '0') != '1':
                emit('PREVIEW ONLY: full replacement candidate built; set MTS_FULL_STORE_COMMIT=1 after reviewing summary')
                return 0

            saved = replace_full_store(merged)
            emit('MTS_FULL_REPLACED ' + str(saved))
            return 0
        except Exception:
            traceback.print_exc()
            emit('NOT SAVED: full-store failure; previous supplier snapshot remains intact')
            return 2
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))
