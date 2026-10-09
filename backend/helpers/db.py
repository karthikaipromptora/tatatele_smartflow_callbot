import json
import os
from datetime import date, datetime
from decimal import Decimal
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

        # Dashboard accounts. Calls and uploads record who started them.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id             SERIAL PRIMARY KEY,
                email          TEXT NOT NULL UNIQUE,
                password_hash  TEXT NOT NULL,
                role           TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by     INTEGER REFERENCES users(id),
                disabled_at    TIMESTAMPTZ,
                last_login_at  TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash  TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id)")
        await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id)")
        await conn.execute("ALTER TABLE batches ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_created_by_idx ON calls (created_by)")
        # Insights, customer commitments, and callback scheduling
        for column in (
            "customer_quote TEXT NOT NULL DEFAULT ''",
            "commitment_eta TEXT NOT NULL DEFAULT ''",
            "callback_date DATE",
            "sentiment TEXT NOT NULL DEFAULT ''",
            "summary TEXT NOT NULL DEFAULT ''",
            "callback_type VARCHAR(30) NOT NULL DEFAULT 'auto'",
        ):
            await conn.execute(f"ALTER TABLE calls ADD COLUMN IF NOT EXISTS {column}")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_callback_date_idx ON calls (callback_date)")

        # Incidents & Tickets table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                ticket_number VARCHAR(32) UNIQUE NOT NULL,
                call_ref_id TEXT REFERENCES calls(ref_id) ON DELETE SET NULL,
                customer_name TEXT,
                phone_number TEXT,
                service_name TEXT,
                amount TEXT,
                category VARCHAR(50) NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                customer_quote TEXT,
                priority VARCHAR(20) DEFAULT 'medium',
                status VARCHAR(30) DEFAULT 'open',
                assigned_to INT REFERENCES users(id) ON DELETE SET NULL,
                resolved_at TIMESTAMPTZ,
                resolution_notes TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_assigned ON tickets(assigned_to)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")

    await backfill_call_insights()
    logger.info("Database ready with insights and tickets")


async def close_db():
    if _pool:
        await _pool.close()


# ── Post-call emails ───────────────────────────────────────────────────────────

EMAIL_MAX_ATTEMPTS = 5


async def claim_email_jobs(limit: int = 5) -> list[str]:
    """Claim completed calls whose summary email is due, so each is sent by one worker at a time.

    Claiming books the next retry slot up front (2 → 10 → 30 → 120 min), so a crash mid-send is
    retried later rather than immediately. Only calls started by a dashboard user are emailed, and
    only within a day of ending.
    """
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE calls SET email_attempts = email_attempts + 1,
                             email_next_at = NOW() + CASE email_attempts
                                 WHEN 0 THEN INTERVAL '2 minutes' WHEN 1 THEN INTERVAL '10 minutes'
                                 WHEN 2 THEN INTERVAL '30 minutes' ELSE INTERVAL '120 minutes' END
            WHERE ref_id IN (
                SELECT ref_id FROM calls
                WHERE status = 'completed' AND email_sent_at IS NULL
                  AND created_by IS NOT NULL AND initiator_email <> ''
                  AND email_attempts < $2
                  AND (email_next_at IS NULL OR email_next_at <= NOW())
                  AND ended_at > NOW() - INTERVAL '24 hours'
                ORDER BY ended_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING ref_id
            """,
            limit, EMAIL_MAX_ATTEMPTS,
        )
        return [r["ref_id"] for r in rows]


async def mark_call_email_sent(ref_id: str):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE calls SET email_sent_at = NOW(), email_error = NULL WHERE ref_id = $1", ref_id)


async def mark_call_email_failed(ref_id: str, error: str):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE calls SET email_error = $2 WHERE ref_id = $1", ref_id, error[:500])


def _row(r) -> dict | None:
    if r is None:
        return None
    out = {}
    for k, v in dict(r).items():
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        elif k == "source" and isinstance(v, str):
            v = json.loads(v)
        out[k] = v
    return out


_INSERT_CALL = """
    INSERT INTO calls
        (ref_id, phone_number, customer_name, service_name, amount,
         billing_period, language, voice_id, status, batch_id, batch_seq, dialed_at,
         call_type, account_number, invoice_number, due_date, days_overdue, amount_paid, email_domain, source,
         created_by, initiator_email)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            CASE WHEN $9 = 'queued' THEN NULL ELSE NOW() END,
            $12, $13, $14, $15, $16, $17, $18, $19::jsonb, $20, $21)
"""

# Call context fields stored on the calls row (and read back when the stream connects).
CONTEXT_COLUMNS = ("customer_name", "service_name", "amount", "billing_period", "language", "voice_id",
                   "call_type", "account_number", "invoice_number", "due_date", "days_overdue",
                   "amount_paid", "email_domain")


def _call_args(ref_id, phone_number, ctx, status, batch_id=None, batch_seq=None, source=None, owner=None):
    """owner: the signed-in user placing the call ({"id", "email"}); their email receives the summary."""
    return (ref_id, phone_number, ctx["customer_name"], ctx["service_name"], ctx["amount"],
            ctx["billing_period"], ctx.get("language", "English"), ctx["voice_id"], status, batch_id, batch_seq,
            ctx.get("call_type", "overdue"), ctx.get("account_number", ""), ctx.get("invoice_number", ""),
            ctx.get("due_date", ""), str(ctx.get("days_overdue", "")), ctx.get("amount_paid", ""),
            ctx.get("email_domain", ""), json.dumps(source) if source else None,
            owner["id"] if owner else None, owner["email"] if owner else "")


async def insert_call(ref_id: str, phone_number: str, ctx: dict, status: str = "initiated", owner: dict | None = None):
    async with _pool.acquire() as conn:
        await conn.execute(_INSERT_CALL, *_call_args(ref_id, phone_number, ctx, status, owner=owner))


def context_from_row(call: dict) -> dict:
    return {k: (call.get(k) or "") for k in CONTEXT_COLUMNS}


# ── Batches ────────────────────────────────────────────────────────────────────

_BATCH_SUMMARY = """
    SELECT b.batch_id, b.file_name, b.total, b.created_at, b.cancelled_at, b.created_by,
           u.email AS created_by_email,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'queued')    AS queued,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'initiated') AS dialing,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'active')    AS active,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'completed') AS completed,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'failed')    AS failed,
           COUNT(c.ref_id) FILTER (WHERE c.status = 'cancelled') AS cancelled
    FROM batches b LEFT JOIN calls c ON c.batch_id = b.batch_id
    LEFT JOIN users u ON u.id = b.created_by
"""


async def create_batch(batch_id: str, file_name: str, call_type: str, calls: list[tuple[str, str, dict, dict | None]],
                       owner: dict | None = None):
    """calls: (ref_id, phone_number, ctx, source_row) in dialing order; all are inserted as 'queued'."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO batches (batch_id, file_name, total, call_type, created_by) VALUES ($1, $2, $3, $4, $5)",
                batch_id, file_name, len(calls), call_type, owner["id"] if owner else None,
            )
            await conn.executemany(
                _INSERT_CALL,
                [_call_args(ref, phone, ctx, "queued", batch_id, seq, source, owner)
                 for seq, (ref, phone, ctx, source) in enumerate(calls)],
            )


async def has_queued_calls() -> bool:
    async with _pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM calls WHERE status='queued')"))


async def list_batches(limit: int = 20, owner_id: int | None = None) -> list[dict]:
    """owner_id limits the list to one user's uploads; None lists everyone's (admin)."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _BATCH_SUMMARY + " WHERE ($2::int IS NULL OR b.created_by = $2)"
            " GROUP BY b.batch_id, u.email ORDER BY b.created_at DESC LIMIT $1",
            limit, owner_id,
        )
        return [_row(r) for r in rows]


async def get_batch(batch_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(
            _BATCH_SUMMARY + " WHERE b.batch_id = $1 GROUP BY b.batch_id, u.email", batch_id
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


_CALL_SELECT = """
    SELECT c.*, b.file_name AS batch_file_name, u.email AS created_by_email FROM calls c
    LEFT JOIN batches b ON b.batch_id = c.batch_id
    LEFT JOIN users u ON u.id = c.created_by
"""


async def get_call(ref_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(_CALL_SELECT + " WHERE c.ref_id=$1", ref_id))


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


async def list_calls(limit: int = 50, batch_id: str | None = None, owner_id: int | None = None) -> list[dict]:
    """owner_id limits the list to calls one user started; None lists every call (admin)."""
    async with _pool.acquire() as conn:
        if batch_id:
            rows = await conn.fetch(
                _CALL_SELECT + """
                WHERE c.batch_id = $2 AND ($3::int IS NULL OR c.created_by = $3)
                ORDER BY c.batch_seq LIMIT $1
                """,
                limit, batch_id, owner_id,
            )
        else:
            rows = await conn.fetch(
                _CALL_SELECT + """
                WHERE ($2::int IS NULL OR c.created_by = $2)
                ORDER BY COALESCE(c.dialed_at, c.created_at) DESC, c.batch_seq DESC LIMIT $1
                """,
                limit, owner_id,
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


# ── Users & sessions ───────────────────────────────────────────────────────────

_USER_COLUMNS = "id, email, role, created_at, created_by, disabled_at, last_login_at"


class EmailTaken(Exception):
    pass


async def count_users() -> int:
    async with _pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def create_user(email: str, password_hash: str, role: str, created_by: int | None = None) -> dict:
    async with _pool.acquire() as conn:
        try:
            return _row(await conn.fetchrow(
                f"INSERT INTO users (email, password_hash, role, created_by) VALUES ($1, $2, $3, $4) RETURNING {_USER_COLUMNS}",
                email, password_hash, role, created_by,
            ))
        except asyncpg.UniqueViolationError as e:
            raise EmailTaken(email) from e


async def get_user_login(email: str) -> dict | None:
    """The user's row including the password hash — for sign-in only."""
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE email=$1", email))


async def get_user(user_id: int) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(f"SELECT {_USER_COLUMNS} FROM users WHERE id=$1", user_id))


async def list_users() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id, u.email, u.role, u.created_at, u.disabled_at, u.last_login_at, cb.email AS created_by_email,
                   (SELECT COUNT(*) FROM calls c WHERE c.created_by = u.id) AS calls
            FROM users u LEFT JOIN users cb ON cb.id = u.created_by
            ORDER BY u.role, u.created_at
            """
        )
        return [_row(r) for r in rows]


async def set_user_disabled(user_id: int, disabled: bool):
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET disabled_at = CASE WHEN $2 THEN COALESCE(disabled_at, NOW()) END WHERE id=$1",
                user_id, disabled,
            )
            if disabled:
                await conn.execute("DELETE FROM sessions WHERE user_id=$1", user_id)


async def set_user_password(user_id: int, password_hash: str):
    """Changing a password signs the user out everywhere."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET password_hash=$2 WHERE id=$1", user_id, password_hash)
            await conn.execute("DELETE FROM sessions WHERE user_id=$1", user_id)


async def create_session(token_hash: str, user_id: int, days: int):
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ($1, $2, NOW() + make_interval(days => $3))",
                token_hash, user_id, days,
            )
            await conn.execute("UPDATE users SET last_login_at = NOW() WHERE id=$1", user_id)


async def get_session_user(token_hash: str) -> dict | None:
    async with _pool.acquire() as conn:
        return _row(await conn.fetchrow(
            """
            SELECT u.id, u.email, u.role FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash=$1 AND s.expires_at > NOW() AND u.disabled_at IS NULL
            """,
            token_hash,
        ))


async def delete_session(token_hash: str):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token_hash=$1", token_hash)


# ── Call Insights, Follow-up Scheduling & Analytics ───────────────────────────


def format_inr(amount_num: float | int) -> str:
    """Formats a numeric value according to the Indian numbering system (e.g. ₹1,25,000)."""
    try:
        val = int(round(float(amount_num)))
    except (ValueError, TypeError):
        return "₹0"
    if val < 0:
        return f"-{format_inr(-val)}"
    s = str(val)
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    parts.append(last3)
    return "₹" + ",".join(parts)


async def save_call_insights(ref_id: str, insights: dict):
    """Persists extracted transcript insights, customer quotes, and follow-up callbacks."""
    cb_date = None
    raw_date = insights.get("callback_date")
    if raw_date:
        if isinstance(raw_date, date):
            cb_date = raw_date
        else:
            try:
                cb_date = date.fromisoformat(str(raw_date).split("T")[0])
            except Exception:
                pass
    async with _pool.acquire() as conn:
        cb_type = str(insights.get("callback_type") or "auto")
        await conn.execute(
            """
            UPDATE calls
            SET customer_quote = $2,
                commitment_eta = $3,
                callback_date = $4,
                sentiment = $5,
                summary = $6,
                callback_type = $7
            WHERE ref_id = $1
            """,
            ref_id,
            (insights.get("customer_quote") or "")[:500],
            (insights.get("commitment_eta") or "")[:100],
            cb_date,
            (insights.get("sentiment") or "")[:50],
            (insights.get("summary") or "")[:1000],
            cb_type,
        )

        # Automatically create or sync incident ticket if needed
        if insights.get("ticket_needed"):
            call_row = await conn.fetchrow("SELECT customer_name, phone_number, service_name, amount, created_by FROM calls WHERE ref_id=$1", ref_id)
            if call_row:
                ticket_no = f"TCK-{ref_id[:8].upper()}"
                cat = insights.get("ticket_category") or "callback_request"
                priority = insights.get("ticket_priority") or "medium"
                title = insights.get("ticket_title") or f"Follow-up for {call_row['customer_name']}"
                desc = insights.get("ticket_description") or ""
                quote = insights.get("customer_quote") or ""
                await conn.execute(
                    """
                    INSERT INTO tickets (
                        ticket_number, call_ref_id, customer_name, phone_number, service_name,
                        amount, category, title, description, customer_quote, priority, status, assigned_to
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'open', $12)
                    ON CONFLICT (ticket_number) DO UPDATE
                    SET category = EXCLUDED.category,
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        customer_quote = EXCLUDED.customer_quote,
                        priority = EXCLUDED.priority,
                        updated_at = NOW()
                    """,
                    ticket_no, ref_id, call_row["customer_name"], call_row["phone_number"],
                    call_row["service_name"], call_row["amount"], cat, title, desc, quote, priority, call_row["created_by"]
                )


async def backfill_call_insights():
    """Populates insights, callback types, and tickets for completed calls."""
    from helpers.insights import extract_call_insights
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.* FROM calls c
            WHERE c.status = 'completed'
            ORDER BY c.created_at DESC
            LIMIT 100
            """
        )
        if not rows:
            return
        logger.info(f"Backfilling insights and tickets for {len(rows)} completed calls...")
        for r in rows:
            ref_id = r["ref_id"]
            trans = await conn.fetch("SELECT role, text FROM transcripts WHERE ref_id=$1 ORDER BY turn_index", ref_id)
            if trans:
                ins = extract_call_insights(dict(r), [dict(t) for t in trans])
                await save_call_insights(ref_id, ins)


async def get_agent_stats(owner_id: int | None = None) -> dict:
    """Aggregates user-level metrics: calls handled, amounts, services distribution, commitments, and quotes."""
    async with _pool.acquire() as conn:
        summary_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_calls,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed_calls,
                COUNT(*) FILTER (WHERE status IN ('active', 'initiated', 'queued')) AS active_calls,
                COUNT(*) FILTER (WHERE status IN ('failed', 'cancelled')) AS failed_calls,
                COALESCE(SUM(NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount,
                COUNT(*) FILTER (WHERE (commitment_eta <> '' OR callback_date IS NOT NULL)) AS commitments_count,
                COALESCE(SUM(CASE WHEN (commitment_eta <> '' OR callback_date IS NOT NULL) THEN NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric ELSE 0 END), 0) AS commitments_amount
            FROM calls
            WHERE ($1::int IS NULL OR created_by = $1)
            """,
            owner_id,
        )

        service_rows = await conn.fetch(
            """
            SELECT
                service_name,
                COUNT(*) AS call_count,
                COALESCE(SUM(NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount
            FROM calls
            WHERE ($1::int IS NULL OR created_by = $1) AND service_name <> ''
            GROUP BY service_name
            ORDER BY call_count DESC, total_amount DESC
            LIMIT 10
            """,
            owner_id,
        )

        commitment_rows = await conn.fetch(
            """
            SELECT
                c.ref_id, c.customer_name, c.phone_number, c.service_name, c.amount,
                c.customer_quote, c.commitment_eta, c.callback_date, c.sentiment, c.status,
                c.created_at, c.created_by, u.email AS created_by_email
            FROM calls c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE ($1::int IS NULL OR c.created_by = $1)
              AND (c.commitment_eta <> '' OR c.callback_date IS NOT NULL OR c.customer_quote <> '')
            ORDER BY c.callback_date ASC NULLS LAST, c.created_at DESC
            LIMIT 50
            """,
            owner_id,
        )

        user_breakdown_rows = await conn.fetch(
            """
            SELECT
                u.id, u.email, u.role,
                COUNT(c.ref_id) AS total_calls,
                COUNT(c.ref_id) FILTER (WHERE c.status = 'completed') AS completed_calls,
                COALESCE(SUM(NULLIF(regexp_replace(c.amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount,
                COUNT(c.ref_id) FILTER (WHERE (c.commitment_eta <> '' OR c.callback_date IS NOT NULL)) AS commitments_count
            FROM users u
            LEFT JOIN calls c ON c.created_by = u.id
            GROUP BY u.id, u.email, u.role
            ORDER BY total_calls DESC, u.id ASC
            """
        )

    tot_amt = float(summary_row["total_amount"]) if summary_row else 0.0
    com_amt = float(summary_row["commitments_amount"]) if summary_row else 0.0

    services = [
        {
            "name": r["service_name"],
            "count": int(r["call_count"]),
            "amount": float(r["total_amount"]),
            "amount_formatted": format_inr(r["total_amount"]),
        }
        for r in service_rows
    ]

    commitments = []
    for r in commitment_rows:
        row_dict = _row(r)
        raw_amt = str(r["amount"] or "0")
        try:
            amt_num = float("".join(c for c in raw_amt if c.isdigit() or c == "."))
        except ValueError:
            amt_num = 0.0
        row_dict["amount_formatted"] = format_inr(amt_num) if amt_num else f"₹{raw_amt}"
        commitments.append(row_dict)

    users_breakdown = [
        {
            "id": r["id"],
            "email": r["email"],
            "role": r["role"],
            "total_calls": int(r["total_calls"]),
            "completed_calls": int(r["completed_calls"]),
            "amount": float(r["total_amount"]),
            "amount_formatted": format_inr(r["total_amount"]),
            "commitments_count": int(r["commitments_count"]),
        }
        for r in user_breakdown_rows
    ]

    selected_user = None
    if owner_id:
        u_row = await get_user(owner_id)
        if u_row:
            selected_user = {"id": u_row["id"], "email": u_row["email"], "role": u_row["role"]}

    return {
        "total_calls": int(summary_row["total_calls"]) if summary_row else 0,
        "completed_calls": int(summary_row["completed_calls"]) if summary_row else 0,
        "active_calls": int(summary_row["active_calls"]) if summary_row else 0,
        "failed_calls": int(summary_row["failed_calls"]) if summary_row else 0,
        "total_amount_num": tot_amt,
        "total_amount_formatted": format_inr(tot_amt),
        "commitments_count": int(summary_row["commitments_count"]) if summary_row else 0,
        "commitments_amount_num": com_amt,
        "commitments_amount_formatted": format_inr(com_amt),
        "services": services,
        "commitments": commitments,
        "users_breakdown": users_breakdown,
        "selected_user": selected_user,
    }


async def get_calendar_events(owner_id: int | None = None, year: int | None = None, month: int | None = None) -> list[dict]:
    """Retrieves all scheduled callbacks with valid business-day callback_dates."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.ref_id, c.customer_name, c.phone_number, c.service_name, c.amount,
                c.customer_quote, c.commitment_eta, c.callback_date, c.sentiment, c.status,
                c.created_at, u.email AS created_by_email
            FROM calls c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE ($1::int IS NULL OR c.created_by = $1)
              AND c.callback_date IS NOT NULL
              AND ($2::int IS NULL OR EXTRACT(YEAR FROM c.callback_date) = $2)
              AND ($3::int IS NULL OR EXTRACT(MONTH FROM c.callback_date) = $3)
            ORDER BY c.callback_date ASC, c.created_at DESC
            """,
            owner_id, year, month,
        )

    events = []
    for r in rows:
        d = _row(r)
        raw_amt = str(r["amount"] or "0")
        try:
            amt_num = float("".join(c for c in raw_amt if c.isdigit() or c == "."))
        except ValueError:
            amt_num = 0.0
        d["amount_formatted"] = format_inr(amt_num) if amt_num else f"₹{raw_amt}"
        events.append(d)
    return events


async def get_admin_overview() -> dict:
    """Aggregates executive-level cross-team metrics, user matrix, and company-wide collection insights."""
    async with _pool.acquire() as conn:
        kpi_row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                COUNT(*) AS total_calls,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed_calls,
                COUNT(*) FILTER (WHERE status IN ('failed', 'cancelled')) AS failed_calls,
                COUNT(*) FILTER (WHERE status IN ('active', 'initiated', 'queued')) AS active_calls,
                COALESCE(SUM(NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount_managed,
                COUNT(*) FILTER (WHERE (commitment_eta <> '' OR callback_date IS NOT NULL)) AS total_commitments,
                COALESCE(SUM(CASE WHEN (commitment_eta <> '' OR callback_date IS NOT NULL) THEN NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric ELSE 0 END), 0) AS commitments_amount
            FROM calls
            """
        )

        user_rows = await conn.fetch(
            """
            SELECT
                u.id, u.email, u.role, u.created_at, u.last_login_at,
                COUNT(c.ref_id) AS total_calls,
                COUNT(c.ref_id) FILTER (WHERE c.status = 'completed') AS completed_calls,
                COUNT(c.ref_id) FILTER (WHERE c.status IN ('failed', 'cancelled')) AS failed_calls,
                COALESCE(SUM(NULLIF(regexp_replace(c.amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount,
                COUNT(c.ref_id) FILTER (WHERE (c.commitment_eta <> '' OR c.callback_date IS NOT NULL)) AS commitments_count,
                COALESCE(SUM(CASE WHEN (c.commitment_eta <> '' OR c.callback_date IS NOT NULL) THEN NULLIF(regexp_replace(c.amount, '[^0-9.]', '', 'g'), '')::numeric ELSE 0 END), 0) AS commitments_amount,
                MIN(c.callback_date) FILTER (WHERE c.callback_date >= CURRENT_DATE) AS next_callback_date,
                COUNT(DISTINCT c.service_name) FILTER (WHERE c.service_name <> '') AS services_count
            FROM users u
            LEFT JOIN calls c ON c.created_by = u.id
            GROUP BY u.id, u.email, u.role, u.created_at, u.last_login_at
            ORDER BY total_calls DESC, u.created_at ASC
            """
        )

        # Check for unassigned calls (placed before users table or system-placed)
        unassigned_row = await conn.fetchrow(
            """
            SELECT
                COUNT(c.ref_id) AS total_calls,
                COUNT(c.ref_id) FILTER (WHERE c.status = 'completed') AS completed_calls,
                COUNT(c.ref_id) FILTER (WHERE c.status IN ('failed', 'cancelled')) AS failed_calls,
                COALESCE(SUM(NULLIF(regexp_replace(c.amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount,
                COUNT(c.ref_id) FILTER (WHERE (c.commitment_eta <> '' OR c.callback_date IS NOT NULL)) AS commitments_count,
                COALESCE(SUM(CASE WHEN (c.commitment_eta <> '' OR c.callback_date IS NOT NULL) THEN NULLIF(regexp_replace(c.amount, '[^0-9.]', '', 'g'), '')::numeric ELSE 0 END), 0) AS commitments_amount,
                MIN(c.callback_date) FILTER (WHERE c.callback_date >= CURRENT_DATE) AS next_callback_date,
                COUNT(DISTINCT c.service_name) FILTER (WHERE c.service_name <> '') AS services_count
            FROM calls c
            WHERE c.created_by IS NULL
            """
        )

        services_rows = await conn.fetch(
            """
            SELECT
                service_name,
                COUNT(*) AS call_count,
                COALESCE(SUM(NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS total_amount
            FROM calls
            WHERE service_name <> ''
            GROUP BY service_name
            ORDER BY call_count DESC, total_amount DESC
            LIMIT 10
            """
        )

        recent_commitments = await conn.fetch(
            """
            SELECT
                c.ref_id, c.customer_name, c.phone_number, c.service_name, c.amount,
                c.customer_quote, c.commitment_eta, c.callback_date, c.sentiment, c.status,
                c.created_at, u.email AS created_by_email
            FROM calls c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE c.commitment_eta <> '' OR c.callback_date IS NOT NULL OR c.customer_quote <> ''
            ORDER BY c.callback_date ASC NULLS LAST, c.created_at DESC
            LIMIT 20
            """
        )

    tot_amt = float(kpi_row["total_amount_managed"]) if kpi_row else 0.0
    com_amt = float(kpi_row["commitments_amount"]) if kpi_row else 0.0

    team_members = []
    for u in user_rows:
        ud = _row(u)
        amt = float(u["total_amount"])
        camt = float(u["commitments_amount"])
        ud["amount_formatted"] = format_inr(amt)
        ud["commitments_amount_formatted"] = format_inr(camt)
        team_members.append(ud)

    if unassigned_row and unassigned_row["total_calls"] > 0:
        u_amt = float(unassigned_row["total_amount"])
        u_camt = float(unassigned_row["commitments_amount"])
        team_members.append({
            "id": None,
            "email": "system@tatatele.com (Direct / System)",
            "role": "system",
            "created_at": None,
            "last_login_at": None,
            "total_calls": int(unassigned_row["total_calls"]),
            "completed_calls": int(unassigned_row["completed_calls"]),
            "failed_calls": int(unassigned_row["failed_calls"]),
            "total_amount": u_amt,
            "amount_formatted": format_inr(u_amt),
            "commitments_count": int(unassigned_row["commitments_count"]),
            "commitments_amount": u_camt,
            "commitments_amount_formatted": format_inr(u_camt),
            "next_callback_date": unassigned_row["next_callback_date"].isoformat() if unassigned_row["next_callback_date"] else None,
            "services_count": int(unassigned_row["services_count"]),
        })

    services = [
        {
            "name": r["service_name"],
            "count": int(r["call_count"]),
            "amount": float(r["total_amount"]),
            "amount_formatted": format_inr(r["total_amount"]),
        }
        for r in services_rows
    ]

    commitments = []
    for r in recent_commitments:
        cd = _row(r)
        raw_amt = str(r["amount"] or "0")
        try:
            amt_num = float("".join(c for c in raw_amt if c.isdigit() or c == "."))
        except ValueError:
            amt_num = 0.0
        cd["amount_formatted"] = format_inr(amt_num) if amt_num else f"₹{raw_amt}"
        commitments.append(cd)

    return {
        "kpis": {
            "total_users": int(kpi_row["total_users"]) if kpi_row else 0,
            "total_calls": int(kpi_row["total_calls"]) if kpi_row else 0,
            "completed_calls": int(kpi_row["completed_calls"]) if kpi_row else 0,
            "failed_calls": int(kpi_row["failed_calls"]) if kpi_row else 0,
            "active_calls": int(kpi_row["active_calls"]) if kpi_row else 0,
            "total_amount_num": tot_amt,
            "total_amount_formatted": format_inr(tot_amt),
            "total_commitments": int(kpi_row["total_commitments"]) if kpi_row else 0,
            "commitments_amount_num": com_amt,
            "commitments_amount_formatted": format_inr(com_amt),
        },
        "team": team_members,
        "services": services,
        "recent_commitments": commitments,
    }


# ── Incidents & Tickets ────────────────────────────────────────────────────────

async def create_ticket(
    call_ref_id: str | None,
    customer_name: str,
    phone_number: str,
    service_name: str,
    amount: str,
    category: str,
    title: str,
    description: str,
    customer_quote: str,
    priority: str = "medium",
    assigned_to: int | None = None,
) -> dict:
    ticket_num = f"TCK-{secrets.token_hex(4).upper()}"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tickets (
                ticket_number, call_ref_id, customer_name, phone_number, service_name,
                amount, category, title, description, customer_quote, priority, status, assigned_to
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'open', $12)
            RETURNING *
            """,
            ticket_num, call_ref_id, customer_name, phone_number, service_name,
            amount, category, title, description, customer_quote, priority, assigned_to,
        )
    return _row(row)


async def get_tickets(
    assigned_to: int | None = None,
    status: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """Retrieves tickets. Non-admins only receive tickets assigned to them; admins see all."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.*,
                u.email AS assigned_to_email,
                u.role AS assigned_to_role
            FROM tickets t
            LEFT JOIN users u ON u.id = t.assigned_to
            WHERE ($1::int IS NULL OR t.assigned_to = $1)
              AND ($2::text IS NULL OR t.status = $2)
              AND ($3::text IS NULL OR t.category = $3)
            ORDER BY
                CASE WHEN t.status = 'open' THEN 1 WHEN t.status = 'in_progress' THEN 2 ELSE 3 END,
                CASE WHEN t.priority = 'urgent' THEN 1 WHEN t.priority = 'high' THEN 2 WHEN t.priority = 'medium' THEN 3 ELSE 4 END,
                t.created_at DESC
            """,
            assigned_to, status, category,
        )
    result = []
    for r in rows:
        d = _row(r)
        raw_amt = str(r["amount"] or "0")
        try:
            amt_num = float("".join(c for c in raw_amt if c.isdigit() or c == "."))
        except ValueError:
            amt_num = 0.0
        d["amount_formatted"] = format_inr(amt_num) if amt_num else f"₹{raw_amt}"
        result.append(d)
    return result


async def get_ticket_stats(assigned_to: int | None = None) -> dict:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress_count,
                COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_count,
                COUNT(*) FILTER (WHERE priority IN ('high', 'urgent') AND status <> 'resolved') AS urgent_count
            FROM tickets
            WHERE ($1::int IS NULL OR assigned_to = $1)
            """,
            assigned_to,
        )
    return {
        "total": int(row["total"]) if row else 0,
        "open": int(row["open_count"]) if row else 0,
        "in_progress": int(row["in_progress_count"]) if row else 0,
        "resolved": int(row["resolved_count"]) if row else 0,
        "urgent": int(row["urgent_count"]) if row else 0,
    }


async def resolve_ticket(
    ticket_id: int,
    resolution_notes: str,
    user_id: int,
    is_admin: bool = False,
) -> dict | None:
    async with _pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        if not t:
            return None
        if not is_admin and t["assigned_to"] != user_id:
            raise PermissionError("You can only resolve tickets assigned to you.")
        updated = await conn.fetchrow(
            """
            UPDATE tickets
            SET status = 'resolved',
                resolved_at = NOW(),
                resolution_notes = $2,
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            ticket_id, resolution_notes,
        )
        return _row(updated) if updated else None


# ── Callbacks Requested ────────────────────────────────────────────────────────

async def get_callbacks(owner_id: int | None = None, cb_type: str | None = None) -> dict:
    """Aggregates requested callbacks, separating human/manual vs automated reminders."""
    async with _pool.acquire() as conn:
        summary_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_callbacks,
                COUNT(*) FILTER (WHERE callback_type = 'human') AS human_callbacks,
                COUNT(*) FILTER (WHERE callback_type = 'auto' OR callback_type IS NULL OR callback_type = '') AS auto_callbacks,
                COUNT(*) FILTER (WHERE callback_date = CURRENT_DATE) AS scheduled_today,
                COUNT(*) FILTER (WHERE callback_date < CURRENT_DATE) AS overdue_count
            FROM calls
            WHERE callback_date IS NOT NULL
              AND ($1::int IS NULL OR created_by = $1)
            """,
            owner_id,
        )

        rows = await conn.fetch(
            """
            SELECT
                c.ref_id, c.customer_name, c.phone_number, c.service_name, c.amount,
                c.customer_quote, c.commitment_eta, c.callback_date,
                COALESCE(NULLIF(c.callback_type, ''), 'auto') AS callback_type,
                c.status, c.created_at, c.created_by,
                u.email AS assigned_to_email, u.role AS assigned_to_role
            FROM calls c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE c.callback_date IS NOT NULL
              AND ($1::int IS NULL OR c.created_by = $1)
              AND ($2::text IS NULL OR c.callback_type = $2)
            ORDER BY c.callback_date ASC, c.created_at DESC
            LIMIT 100
            """,
            owner_id, cb_type,
        )

    callbacks = []
    for r in rows:
        d = _row(r)
        raw_amt = str(r["amount"] or "0")
        try:
            amt_num = float("".join(c for c in raw_amt if c.isdigit() or c == "."))
        except ValueError:
            amt_num = 0.0
        d["amount_formatted"] = format_inr(amt_num) if amt_num else f"₹{raw_amt}"
        callbacks.append(d)

    return {
        "kpis": {
            "total_callbacks": int(summary_row["total_callbacks"]) if summary_row else 0,
            "human_callbacks": int(summary_row["human_callbacks"]) if summary_row else 0,
            "auto_callbacks": int(summary_row["auto_callbacks"]) if summary_row else 0,
            "scheduled_today": int(summary_row["scheduled_today"]) if summary_row else 0,
            "overdue_count": int(summary_row["overdue_count"]) if summary_row else 0,
        },
        "callbacks": callbacks,
    }
