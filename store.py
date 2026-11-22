"""Dedicated Onliner job and approval storage; no writes to supplier tables."""
import hashlib
import json
import os
import re
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=10)


def ids(key):
    return {int(x) for x in re.findall(r'\d+', os.getenv(key, ''))}


def launch_ids():
    return ids('ONLINER_LAUNCH_ADMIN_IDS')


def approval_ids():
    return ids('ONLINER_APPROVAL_ADMIN_IDS')


def init():
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS onliner_jobs (
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','completed','failed')),
          stage TEXT NOT NULL DEFAULT 'queued',
          started_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
          heartbeat_at TIMESTAMPTZ,
          summary JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT)''')
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS onliner_one_active_job ON onliner_jobs ((1)) WHERE state IN ('queued','running')")
        conn.execute('''CREATE TABLE IF NOT EXISTS onliner_candidates (
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          fingerprint TEXT NOT NULL UNIQUE,
          first_job_id BIGINT REFERENCES onliner_jobs(id),
          last_job_id BIGINT REFERENCES onliner_jobs(id),
          category TEXT, model TEXT NOT NULL, color TEXT,
          price_byn NUMERIC(16,2), reason TEXT,
          candidate_product_ids TEXT,
          review_kind TEXT NOT NULL CHECK (review_kind IN ('candidate','uncertain')),
          status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','needs_review')),
          decision_by BIGINT, decided_at TIMESTAMPTZ,
          last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS onliner_decisions (
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          candidate_id BIGINT NOT NULL REFERENCES onliner_candidates(id),
          actor_id BIGINT NOT NULL,
          previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
          changed_at TIMESTAMPTZ NOT NULL DEFAULT now())''')


def enqueue(user_id):
    try:
        with connect() as conn:
            row = conn.execute('INSERT INTO onliner_jobs(started_by) VALUES (%s) RETURNING id', (user_id,)).fetchone()
            return row['id'], True
    except psycopg.errors.UniqueViolation:
        with connect() as conn:
            row = conn.execute("SELECT id FROM onliner_jobs WHERE state IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            return (row['id'] if row else None), False


def current_job():
    with connect() as conn:
        return conn.execute('SELECT * FROM onliner_jobs ORDER BY id DESC LIMIT 1').fetchone()


def claim():
    with connect() as conn:
        row = conn.execute("""UPDATE onliner_jobs SET state='running',stage='starting',started_at=now(),heartbeat_at=now()
            WHERE id=(SELECT id FROM onliner_jobs WHERE state='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""").fetchone()
        return row


def stage(job_id, label):
    with connect() as conn:
        conn.execute("UPDATE onliner_jobs SET stage=%s,heartbeat_at=now() WHERE id=%s AND state='running'", (label, job_id))


def fail(job_id, error):
    with connect() as conn:
        conn.execute("UPDATE onliner_jobs SET state='failed',stage='error',finished_at=now(),error=%s WHERE id=%s", (str(error)[:1800], job_id))


def complete(job_id, sheets, stats, parser_stats):
    """Atomic result commit; never publish; never reset prior administrator decisions."""
    pending = 0
    review = 0
    with connect() as conn:
        for title, kind in (('Можно добавить','candidate'),('Проверить','uncertain')):
            rows = next((r for name,r in sheets if name == title), [])
            if not rows: continue
            header = [str(v) for v in rows[0]]
            for values in rows[1:]:
                data = dict(zip(header, values))
                model = str(data.get('Модель магазина') or '').strip()
                if not model: continue
                category = str(data.get('Категория магазина') or '')
                color = str(data.get('Цвет магазина') or '')
                fingerprint = hashlib.sha256(json.dumps([category.casefold(),model.casefold(),color.casefold()], ensure_ascii=False).encode()).hexdigest()
                price = data.get('Цена, BYN')
                if not isinstance(price,(float,int)) or price <= 0: continue
                reason = str(data.get('Причина') or '')
                candidates = str(data.get('productId Onliner') or '')
                conn.execute('''INSERT INTO onliner_candidates
                    (fingerprint,first_job_id,last_job_id,category,model,color,price_byn,reason,candidate_product_ids,review_kind,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (fingerprint) DO UPDATE SET last_job_id=EXCLUDED.last_job_id,
                    price_byn=EXCLUDED.price_byn,reason=EXCLUDED.reason,
                    candidate_product_ids=EXCLUDED.candidate_product_ids,
                    review_kind=EXCLUDED.review_kind,last_seen_at=now()''',
                    (fingerprint,job_id,job_id,category,model,color,price,reason,candidates,kind,
                     'pending' if kind == 'candidate' else 'needs_review'))
                if kind == 'candidate': pending += 1
                else: review += 1
        summary = dict(stats)
        summary.update({'candidate_rows': pending, 'uncertain_rows':review, 'parser':parser_stats})
        conn.execute("UPDATE onliner_jobs SET state='completed',stage='done',finished_at=now(),summary=%s WHERE id=%s",(Jsonb(summary),job_id))
    return summary


def counts():
    with connect() as conn:
        return conn.execute("SELECT status,count(*) AS n FROM onliner_candidates GROUP BY status").fetchall()


def list_candidates(offset=0, limit=5):
    with connect() as conn:
        return conn.execute("""SELECT * FROM onliner_candidates WHERE status IN ('pending','needs_review')
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,id LIMIT %s OFFSET %s""",(limit,offset)).fetchall()


def get_candidate(candidate_id):
    with connect() as conn:
        return conn.execute('SELECT * FROM onliner_candidates WHERE id=%s',(candidate_id,)).fetchone()


def decide(candidate_id, actor_id, new_status):
    if new_status not in ('approved','rejected'): raise ValueError('invalid decision')
    with connect() as conn:
        with conn.transaction():
            row = conn.execute('SELECT * FROM onliner_candidates WHERE id=%s FOR UPDATE',(candidate_id,)).fetchone()
            if not row: return 'missing'
            if row['status'] not in ('pending','needs_review'): return 'already'
            conn.execute('''UPDATE onliner_candidates SET status=%s,decision_by=%s,decided_at=now() WHERE id=%s''',(new_status,actor_id,candidate_id))
            conn.execute('''INSERT INTO onliner_decisions(candidate_id,actor_id,previous_status,new_status)
                VALUES (%s,%s,%s,%s)''',(candidate_id,actor_id,row['status'],new_status))
            return 'ok'
