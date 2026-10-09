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
        await conn.execute(_USER_GUARD_SQL)
        await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        await conn.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id)")
        await conn.execute("ALTER TABLE batches ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS calls_created_by_idx ON calls (created_by)")
        # Post-call email delivery state (retried with backoff).
        for column in ("email_attempts INTEGER NOT NULL DEFAULT 0", "email_next_at TIMESTAMPTZ", "email_error TEXT"):
            await conn.execute(f"ALTER TABLE calls ADD COLUMN IF NOT EXISTS {column}")
    logger.info("Database ready")


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

# Accounts may only be created or changed by this app. The app tags each account change with
# the acting admin (app.actor, set per transaction); a change without it — e.g. a password hash
# edited or copied by hand in a SQL console — is rejected. Every accepted change is audited.
# (Sign-ins only touch last_login_at and pass through untouched.)
_USER_GUARD_SQL = """
CREATE TABLE IF NOT EXISTS user_audit (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id  INTEGER,
    email    TEXT,
    event    TEXT NOT NULL,
    actor    TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION guard_user_changes() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor TEXT := NULLIF(current_setting('app.actor', true), '');
    ev    TEXT;
    uid   INTEGER;
    em    TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        ev := 'created (' || NEW.role || ')'; uid := NEW.id; em := NEW.email;
    ELSIF TG_OP = 'DELETE' THEN
        ev := 'deleted'; uid := OLD.id; em := OLD.email;
    ELSE
        uid := NEW.id; em := NEW.email;
        ev := concat_ws(', ',
            CASE WHEN NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN 'password changed' END,
            CASE WHEN NEW.email IS DISTINCT FROM OLD.email THEN 'email changed from ' || OLD.email END,
            CASE WHEN NEW.role IS DISTINCT FROM OLD.role THEN 'role changed to ' || NEW.role END,
            CASE WHEN NEW.disabled_at IS DISTINCT FROM OLD.disabled_at
                 THEN CASE WHEN NEW.disabled_at IS NULL THEN 'enabled' ELSE 'disabled' END END);
        IF ev = '' THEN
            RETURN NEW;  -- e.g. last_login_at on sign-in
        END IF;
    END IF;
    IF actor IS NULL THEN
        RAISE EXCEPTION 'Blocked: dashboard accounts can only be changed from the dashboard (% for %)', ev, em
            USING HINT = 'Sign in as an admin and use the Users page to reset passwords or disable accounts.';
    END IF;
    INSERT INTO user_audit (user_id, email, event, actor) VALUES (uid, em, ev, actor);
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

CREATE OR REPLACE TRIGGER users_guard
    BEFORE INSERT OR UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION guard_user_changes();
"""


async def _as_actor(conn, actor: str):
    """Tag this transaction's account changes with who made them (see _USER_GUARD_SQL)."""
    await conn.execute("SELECT set_config('app.actor', $1, true)", actor)


class EmailTaken(Exception):
    pass


async def count_users() -> int:
    async with _pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def create_user(email: str, password_hash: str, role: str, created_by: int | None, actor: str) -> dict:
    async with _pool.acquire() as conn:
        try:
            async with conn.transaction():
                await _as_actor(conn, actor)
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


async def set_user_disabled(user_id: int, disabled: bool, actor: str):
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await _as_actor(conn, actor)
            await conn.execute(
                "UPDATE users SET disabled_at = CASE WHEN $2 THEN COALESCE(disabled_at, NOW()) END WHERE id=$1",
                user_id, disabled,
            )
            if disabled:
                await conn.execute("DELETE FROM sessions WHERE user_id=$1", user_id)


async def set_user_password(user_id: int, password_hash: str, actor: str):
    """Changing a password signs the user out everywhere."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await _as_actor(conn, actor)
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


async def list_user_audit(limit: int = 50) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT at, email, event, actor FROM user_audit ORDER BY at DESC LIMIT $1", limit)
        return [_row(r) for r in rows]
