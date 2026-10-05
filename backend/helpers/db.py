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
    _pool = await asyncpg.create_pool(_asyncpg_dsn(url), min_size=1, max_size=10)
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


def _row(r) -> dict | None:
    if r is None:
        return None
    return {
        k: v.isoformat() if isinstance(v, (datetime, date)) else v
        for k, v in dict(r).items()
    }


async def insert_call(ref_id: str, phone_number: str, ctx: dict, status: str = "initiated"):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO calls
                (ref_id, phone_number, customer_name, service_name, amount,
                 billing_period, language, voice_id, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            ref_id, phone_number, ctx["customer_name"], ctx["service_name"], ctx["amount"],
            ctx["billing_period"], ctx["language"], ctx["voice_id"], status,
        )


async def get_call(ref_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow("SELECT * FROM calls WHERE ref_id=$1", ref_id))


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
              AND created_at > NOW() - make_interval(mins => $2)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            customer_number, within_minutes,
        )
        return _row(row)


async def list_calls(limit: int = 50) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM calls ORDER BY created_at DESC LIMIT $1", limit)
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
