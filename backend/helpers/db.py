import json
import os
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from loguru import logger

_pool: asyncpg.Pool | None = None


def _asyncpg_dsn(url: str) -> str:
    # Neon connection strings include channel_binding, a libpq-only option asyncpg rejects.
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def init_db():
    global _pool
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Neon's pooled endpoint is PgBouncer (transaction mode): asyncpg's prepared-statement cache must be off.
    _pool = await asyncpg.create_pool(_asyncpg_dsn(url), min_size=1, max_size=10, statement_cache_size=0)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                ref_id          TEXT PRIMARY KEY,
                call_sid        TEXT,
                stream_sid      TEXT,
                direction       TEXT,
                phone_number    TEXT NOT NULL DEFAULT '',
                customer_name   TEXT NOT NULL,
                service_name    TEXT NOT NULL,
                amount          TEXT NOT NULL,
                billing_period  TEXT NOT NULL,
                language        TEXT NOT NULL,
                voice_id        TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'initiated',
                error           TEXT,
                recording_path  TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at      TIMESTAMPTZ,
                ended_at        TIMESTAMPTZ
            )
        """)
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS smartflow_ref_id TEXT")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                batch_id      TEXT PRIMARY KEY,
                file_name     TEXT NOT NULL,
                total         INTEGER NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cancelled_at  TIMESTAMPTZ
            )
        """)
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS batch_id TEXT REFERENCES batches(batch_id)")
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS batch_seq INTEGER")
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS dialed_at TIMESTAMPTZ")
        for column in ("call_type TEXT NOT NULL DEFAULT 'overdue'", "account_number TEXT NOT NULL DEFAULT ''",
                       "invoice_number TEXT NOT NULL DEFAULT ''", "due_date TEXT NOT NULL DEFAULT ''",
                       "days_overdue TEXT NOT NULL DEFAULT ''", "amount_paid TEXT NOT NULL DEFAULT ''",
                       "email_domain TEXT NOT NULL DEFAULT ''", "initiator_email TEXT NOT NULL DEFAULT ''",
                       "email_sent_at TIMESTAMPTZ", "source JSONB"):
            await conn.execute(f"ALTER TABLE calls ADD COLUMN IF NOT EXISTS {column}")
        await conn.execute("ALTER TABLE batches ADD COLUMN IF NOT EXISTS call_type TEXT NOT NULL DEFAULT 'overdue'")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_status_created_idx ON calls (status, created_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_phone_status_idx ON calls (phone_number, status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_batch_idx ON calls (batch_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_status_email_idx ON calls (status, email_sent_at)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id          SERIAL PRIMARY KEY,
                ref_id      TEXT NOT NULL REFERENCES calls(ref_id) ON DELETE CASCADE,
                role        TEXT NOT NULL,
                text        TEXT NOT NULL,
                turn_index  INTEGER NOT NULL
            )
        """)
    logger.info("Database ready")


async def close_db():
    if _pool:
        await _pool.close()


async def mark_call_email_sent(ref_id: str):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE calls SET email_sent_at = NOW() WHERE ref_id = $1", ref_id)


async def get_pending_email_calls(limit: int = 10) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.*, b.file_name AS batch_file_name
            FROM calls c
            LEFT JOIN batches b ON b.batch_id = c.batch_id
            WHERE c.status = 'completed'
              AND c.email_sent_at IS NULL
              AND COALESCE(c.ended_at, c.created_at) > NOW() - INTERVAL '24 hours'
            ORDER BY COALESCE(c.ended_at, c.created_at) DESC
            LIMIT $1
            """,
            limit,
        )
        return [_row(r) for r in rows]


def _row(r) -> dict | None:
    if r is None:
        return None
    out = {}
    for k, v in dict(r).items():
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif k == "source" and isinstance(v, str):
            v = json.loads(v)
        out[k] = v
    return out


_INSERT_CALL = """
    INSERT INTO calls
        (ref_id, phone_number, customer_name, service_name, amount,
         billing_period, language, voice_id, status, batch_id, batch_seq, dialed_at,
         call_type, account_number, invoice_number, due_date, days_overdue, amount_paid, email_domain, initiator_email, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            CASE WHEN $9 = 'queued' THEN NULL ELSE NOW() END,
            $12, $13, $14, $15, $16, $17, $18, $19, $20::jsonb)
"""

# Call context fields stored on the calls row (and read back when the stream connects).
CONTEXT_COLUMNS = ("customer_name", "service_name", "amount", "billing_period", "language", "voice_id",
                   "call_type", "account_number", "invoice_number", "due_date", "days_overdue",
                   "amount_paid", "email_domain", "initiator_email")


def _call_args(ref_id, phone_number, ctx, status, batch_id=None, batch_seq=None, source=None):
    return (ref_id, phone_number, ctx["customer_name"], ctx["service_name"], ctx["amount"],
            ctx["billing_period"], ctx.get("language", "English"), ctx["voice_id"], status, batch_id, batch_seq,
            ctx.get("call_type", "overdue"), ctx.get("account_number", ""), ctx.get("invoice_number", ""),
            ctx.get("due_date", ""), str(ctx.get("days_overdue", "")), ctx.get("amount_paid", ""),
            ctx.get("email_domain", ""), str(ctx.get("initiator_email", "")),
            json.dumps(source) if source else None)


async def insert_call(ref_id: str, phone_number: str, ctx: dict, status: str = "initiated"):
    async with _pool.acquire() as conn:
        await conn.execute(_INSERT_CALL, *_call_args(ref_id, phone_number, ctx, status))


def context_from_row(call: dict) -> dict:
    return {k: (call.get(k) or "") for k in CONTEXT_COLUMNS}


# ── Batches ────────────────────────────────────────────────────────────────────

_BATCH_SUMMARY = """
    SELECT b.batch_id, b.file_name, b.total, b.created_at, b.cancelled_at,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'queued')    AS queued,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'initiated') AS dialing,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'active')    AS active,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'completed') AS completed,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'failed')    AS failed,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'cancelled') AS cancelled
    FROM batches b LEFT JOIN calls c ON c.batch_id = b.batch_id
"""


async def create_batch(batch_id: str, file_name: str, call_type: str, calls: list[tuple[str, str, dict, dict | None]]):
    """calls: (ref_id, phone_number, ctx, source_row) in dialing order; all are inserted as 'queued'."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO batches (batch_id, file_name, total, call_type) VALUES ($1, $2, $3, $4)",
                batch_id, file_name, len(calls), call_type,
            )
            await conn.executemany(
                _INSERT_CALL,
                [_call_args(ref, phone, ctx, "queued", batch_id, seq, source)
                 for seq, (ref, phone, ctx, source) in enumerate(calls)],
            )


async def has_queued_calls() -> bool:
    async with _pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM calls WHERE status='queued')"))


async def list_batches(limit: int = 20) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _BATCH_SUMMARY + " GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT $1", limit
        )
        return [_row(r) for r in rows]


async def get_batch(batch_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(
            _BATCH_SUMMARY + " WHERE b.batch_id = $1 GROUP BY b.batch_id", batch_id
        ))


async def cancel_batch(batch_id: str) -> int:
    """Cancel calls in the batch that have not been dialed yet. Returns how many were cancelled."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE calls SET status='cancelled', error='Cancelled before dialing', ended_at=NOW()
                WHERE batch_id=$1 AND status='queued'
                """,
                batch_id,
            )
            cancelled = int(result.split()[-1])
            if cancelled:
                await conn.execute(
                    "UPDATE batches SET cancelled_at=COALESCE(cancelled_at, NOW()) WHERE batch_id=$1", batch_id
                )
    return cancelled


async def claim_next_queued_call() -> dict | None:
    """Atomically move the oldest queued call to 'initiated' so it is dialed exactly once.

    Calls to a number that already has a live call (ringing in the last 3 minutes, or connected)
    wait their turn, so a customer with several invoices is never rung twice at once.
    """
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(
            """
            UPDATE calls SET status='initiated', dialed_at=NOW()
            WHERE ref_id = (
                SELECT q.ref_id FROM calls q
                WHERE q.status='queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM calls live
                      WHERE live.phone_number = q.phone_number
                        AND (live.status = 'active'
                             OR (live.status = 'initiated' AND live.dialed_at > NOW() - INTERVAL '3 minutes'))
                  )
                ORDER BY q.created_at, q.batch_seq
                LIMIT 1
                FOR UPDATE OF q SKIP LOCKED
            )
            RETURNING *
            """
        ))


async def get_call(ref_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(
            """
            SELECT c.*, b.file_name AS batch_file_name FROM calls c
            LEFT JOIN batches b ON b.batch_id = c.batch_id
            WHERE c.ref_id=$1
            """,
            ref_id,
        ))


async def set_smartflow_ref(ref_id: str, smartflow_ref_id: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE calls SET smartflow_ref_id=$2 WHERE ref_id=$1", ref_id, smartflow_ref_id
        )


async def find_recent_initiated_call(customer_number: str, within_minutes: int = 5) -> dict | None:
    """Match an outbound stream to the call we requested, by the customer's last 10 digits."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM calls
            WHERE status='initiated'
              AND right(regexp_replace(phone_number, '\\D', '', 'g'), 10) = right(regexp_replace($1, '\\D', '', 'g'), 10)
              AND COALESCE(dialed_at, created_at) > NOW() - make_interval(mins => $2)
            ORDER BY COALESCE(dialed_at, created_at) DESC
            LIMIT 1
            """,
            customer_number, within_minutes,
        )
        return _row(row)


async def list_calls(limit: int = 50, batch_id: str | None = None) -> list[dict]:
    async with _pool.acquire() as conn:
        if batch_id:
            rows = await conn.fetch(
                """
                SELECT c.*, b.file_name AS batch_file_name FROM calls c
                LEFT JOIN batches b ON b.batch_id = c.batch_id
                WHERE c.batch_id = $2 ORDER BY c.batch_seq LIMIT $1
                """,
                limit, batch_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT c.*, b.file_name AS batch_file_name FROM calls c
                LEFT JOIN batches b ON b.batch_id = c.batch_id
                ORDER BY COALESCE(c.dialed_at, c.created_at) DESC, c.batch_seq DESC LIMIT $1
                """,
                limit,
            )
        return [_row(r) for r in rows]


async def mark_call_started(ref_id: str, call_sid: str, stream_sid: str, direction: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE calls SET call_sid=$2, stream_sid=$3, direction=$4,
                             status='active', started_at=NOW()
            WHERE ref_id=$1
            """,
            ref_id, call_sid, stream_sid, direction,
        )


async def mark_call_ended(ref_id: str, status: str, recording_path: str | None, error: str | None = None):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE calls SET status=$2, recording_path=$3, error=$4, ended_at=NOW()
            WHERE ref_id=$1
            """,
            ref_id, status, recording_path, error,
        )


async def mark_call_failed(ref_id: str, error: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE calls SET status='failed', error=$2, ended_at=NOW() WHERE ref_id=$1",
            ref_id, error,
        )


async def insert_transcript(ref_id: str, turns: list[dict]):
    if not turns:
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO transcripts (ref_id, role, text, turn_index) VALUES ($1, $2, $3, $4)",
            [(ref_id, t["role"], t["text"], i) for i, t in enumerate(turns)],
        )


async def get_transcript(ref_id: str) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, text, turn_index FROM transcripts WHERE ref_id=$1 ORDER BY turn_index",
            ref_id,
        )
        return [_row(r) for r in rows]
