import asyncio
import logging
import os
import sys
import uuid
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.websockets import WebSocketState

load_dotenv(override=True)

from helpers import auth, db
from helpers.batch_file import FILTER_COLUMNS, MAX_BYTES, MAX_ROWS, BatchFileError, parse_rows, template_workbook
from helpers.bot import run_bot
from helpers.call_input import normalize_period, parse_date, validate_call
from helpers.dispatcher import BatchDispatcher, CallRateLimiter, gap_from_env, place_call
from helpers.email_sender import is_email_configured, trigger_call_completed_email
from helpers.prompts import DEFAULT_CONTEXT
from helpers.smartflow import ClickToCallError, ClickToCallNotConfigured, read_stream_start
from helpers.voices import (
    DEFAULT_VOICE,
    LANGUAGE_CODES,
    MAX_PREVIEW_CHARS,
    VOICE_IDS,
    VOICES,
    VoicePreviewError,
    preview_audio,
    sample_text,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# ── Logging ────────────────────────────────────────────────────────────────────

logger.remove()


_VERBOSE = os.getenv("LOG_VERBOSE", "").lower() in ("1", "true", "yes")


def _log_filter(record) -> bool:
    level = record["level"].no
    if _VERBOSE:
        return level >= 10
    if level >= 30:
        return True
    if record["name"].startswith(("helpers", "routers", "__main__")):
        return level >= 10
    msg = record["message"]
    return "TTFB" in msg or "Generating TTS" in msg


logger.add(
    sys.stderr,
    filter=_log_filter,
    format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<8}</level> | {message}",
    colorize=True,
)


class _HidePollingRequests(logging.Filter):
    """The dashboard polls these every few seconds; keep them out of the access log."""

    _POLLED = ('"GET /logs HTTP', '"GET /logs?', '"GET /batches HTTP', '"GET /batches?', '"GET /health HTTP')

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(p in message for p in self._POLLED)


logging.getLogger("uvicorn.access").addFilter(_HidePollingRequests())

# ── App ────────────────────────────────────────────────────────────────────────


async def send_call_email(ref_id: str) -> dict:
    """Email the summary and transcript of a completed call to the user who started it."""
    call = await db.get_call(ref_id)
    if not call:
        return {"success": False, "reason": "Call not found", "ref_id": ref_id}
    res = await trigger_call_completed_email(ref_id, await db.get_transcript(ref_id), call)
    if res.get("success"):
        await db.mark_call_email_sent(ref_id)
        logger.info(f"[{ref_id}] Summary email sent to {res['recipient']}")
    else:
        await db.mark_call_email_failed(ref_id, res.get("error") or res.get("reason") or "Unknown error")
    return res


class EmailWorker:
    """Sends post-call emails from the database queue (see db.claim_email_jobs).

    Woken when a call ends; also checks every minute for retries. Does nothing until email
    credentials are configured, so calls are never re-processed in a tight loop.
    """

    def __init__(self):
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self):
        if not is_email_configured():
            logger.warning("Email is not configured (OUTLOOK_* or SMTP_* in .env) — post-call emails are off")
        self._task = asyncio.create_task(self._run(), name="email-worker")

    def wake(self):
        self._wake.set()

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not is_email_configured():
                continue
            try:
                while ref_ids := await db.claim_email_jobs():
                    for ref_id in ref_ids:
                        try:
                            res = await send_call_email(ref_id)
                            if not res.get("success"):
                                logger.warning(f"[{ref_id}] Summary email not sent: {res.get('error') or res.get('reason')}")
                        except Exception as e:
                            logger.exception(f"[{ref_id}] Summary email failed")
                            await db.mark_call_email_failed(ref_id, str(e))
            except Exception:
                logger.exception("Email worker error; retrying in a minute")


async def _ensure_admin():
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when no accounts exist yet."""
    if await db.count_users():
        return
    email = auth.normalize_email(os.getenv("ADMIN_EMAIL"))
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        logger.warning("No dashboard accounts exist — set ADMIN_EMAIL and ADMIN_PASSWORD in .env and restart to create the admin")
        return
    problem = auth.email_error(email) or auth.password_error(password)
    if problem:
        logger.error(f"Admin account not created: {problem}")
        return
    await db.create_user(email, await auth.hash_password(password), "admin")
    logger.info(f"Created admin account {email}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await _ensure_admin()
    app.state.session = aiohttp.ClientSession()
    app.state.limiter = CallRateLimiter(gap_from_env())
    app.state.dispatcher = BatchDispatcher(app.state.session, app.state.limiter)
    app.state.dispatcher.start()
    app.state.emailer = EmailWorker()
    app.state.emailer.start()
    yield
    await app.state.emailer.stop()
    await app.state.dispatcher.stop()
    await app.state.session.close()
    await db.close_db()


app = FastAPI(title="Tata Tele SmartFlow Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Sign-in ────────────────────────────────────────────────────────────────────
# Every route needs a signed-in user except these. The SmartFlow media stream (/ws) is a
# WebSocket, which this HTTP middleware never sees, so Smartflo can always connect.

_PUBLIC_PATHS = {"/health", "/login", "/auth/login"}
_PAGES = {"/", "/calls", "/voices", "/settings", "/users"}
# 5 wrong passwords per account, and 30 per client IP, per 15 minutes. The IP limit is higher
# because behind a reverse proxy every user can share one address.
_email_throttle = auth.LoginThrottle(limit=5)
_ip_throttle = auth.LoginThrottle(limit=30)


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    user = await db.get_session_user(auth.token_hash(token)) if token else None
    if not user:
        if request.method == "GET" and path in _PAGES:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Please sign in"}, status_code=401)
    request.state.user = user
    return await call_next(request)


def current_user(request: Request) -> dict:
    return request.state.user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can do this")
    return user


def owner_scope(request: Request) -> int | None:
    """Users see only what they started; admins see everything (None = no filter)."""
    user = current_user(request)
    return None if user["role"] == "admin" else user["id"]


def can_see(request: Request, created_by: int | None) -> bool:
    scope = owner_scope(request)
    return scope is None or created_by == scope


def _secure_cookie(request: Request) -> bool:
    forced = os.getenv("COOKIE_SECURE", "").lower()
    if forced in ("1", "true", "yes", "0", "false", "no"):
        return forced in ("1", "true", "yes")
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


@app.get("/login")
async def login_page(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token and await db.get_session_user(auth.token_hash(token)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/auth/login")
async def login(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except ValueError:
        data = {}
    email = auth.normalize_email(data.get("email"))
    password = str(data.get("password") or "")
    ip = request.client.host if request.client else "?"
    wait = max(_email_throttle.retry_after(email), _ip_throttle.retry_after(ip))
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {max(1, wait // 60)} min.")

    user = await db.get_user_login(email) if email else None
    ok = await auth.verify_password(password, user["password_hash"]) if user else await auth.verify_unknown_user(password)
    if not ok or user["disabled_at"]:
        _email_throttle.failed(email)
        _ip_throttle.failed(ip)
        logger.warning(f"Failed sign-in for {email or '(blank)'} from {ip}")
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    _email_throttle.succeeded(email)
    token = auth.new_session_token()
    await db.create_session(auth.token_hash(token), user["id"], auth.SESSION_DAYS)
    logger.info(f"{email} signed in")
    resp = JSONResponse({"id": user["id"], "email": user["email"], "role": user["role"]})
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=_secure_cookie(request), path="/")
    return resp


@app.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        await db.delete_session(auth.token_hash(token))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/me")
async def me(request: Request) -> dict:
    return current_user(request)


# ── Users (admin) ──────────────────────────────────────────────────────────────


@app.get("/users/list")
async def list_users(request: Request) -> JSONResponse:
    require_admin(request)
    return JSONResponse(await db.list_users())


@app.post("/users")
async def create_user(request: Request) -> JSONResponse:
    admin = require_admin(request)
    data = await request.json()
    email = auth.normalize_email(data.get("email"))
    password = str(data.get("password") or "")
    role = data.get("role") if data.get("role") in auth.ROLES else "user"
    problem = auth.email_error(email) or auth.password_error(password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    try:
        user = await db.create_user(email, await auth.hash_password(password), role, admin["id"])
    except db.EmailTaken:
        raise HTTPException(status_code=409, detail=f"An account for {email} already exists")
    logger.info(f"{admin['email']} created {role} account {email}")
    return JSONResponse(user, status_code=201)


async def _target_user(request: Request, user_id: int) -> tuple[dict, dict]:
    admin = require_admin(request)
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return admin, user


@app.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: int) -> JSONResponse:
    admin, user = await _target_user(request, user_id)
    if user["id"] == admin["id"]:
        raise HTTPException(status_code=400, detail="You can't disable your own account")
    await db.set_user_disabled(user_id, True)
    logger.info(f"{admin['email']} disabled {user['email']}")
    return JSONResponse(await db.get_user(user_id))


@app.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: int) -> JSONResponse:
    admin, user = await _target_user(request, user_id)
    await db.set_user_disabled(user_id, False)
    logger.info(f"{admin['email']} enabled {user['email']}")
    return JSONResponse(await db.get_user(user_id))


@app.post("/users/{user_id}/password")
async def reset_password(request: Request, user_id: int) -> JSONResponse:
    admin, user = await _target_user(request, user_id)
    password = str((await request.json()).get("password") or "")
    problem = auth.password_error(password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    await db.set_user_password(user_id, await auth.hash_password(password))
    logger.info(f"{admin['email']} reset the password for {user['email']}")
    return JSONResponse({"ok": True})


# ── Dashboard ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Dashboard pages share one client-side app.
@app.get("/")
@app.get("/calls")
@app.get("/voices")
@app.get("/settings")
@app.get("/users")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/config")
async def config(request: Request) -> dict:
    return {
        "call_gap_seconds": request.app.state.limiter.gap,
        "batch_max_rows": MAX_ROWS,
        "default_voice": DEFAULT_VOICE,
        "min_amount": MIN_AMOUNT,
        "email_enabled": is_email_configured(),
        "filter_labels": {k: label for k, (label, _) in FILTER_COLUMNS.items()},
    }


# ── Single outbound call ───────────────────────────────────────────────────────


@app.post("/start")
async def start_call(request: Request) -> JSONResponse:
    data = await request.json()
    call, errors = validate_call(data)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    phone_number, ctx = call["phone_number"], call["ctx"]
    ref_id = uuid.uuid4().hex
    await db.insert_call(ref_id, phone_number, ctx, owner=current_user(request))

    try:
        smartflow_ref_id = await place_call(request.app.state.session, request.app.state.limiter, ref_id, phone_number)
    except ClickToCallNotConfigured as e:
        raise HTTPException(status_code=501, detail=str(e))
    except ClickToCallError as e:
        logger.error(f"[{ref_id}] {e}")
        raise HTTPException(status_code=502, detail=str(e))

    logger.info(
        f"[{ref_id}] Outbound call queued to={phone_number} customer={ctx['customer_name']} "
        f"smartflow_ref_id={smartflow_ref_id}"
    )
    return JSONResponse({"ref_id": ref_id, "smartflow_ref_id": smartflow_ref_id, "status": "initiated"})


# ── Bulk calls from Excel / CSV ────────────────────────────────────────────────


MIN_AMOUNT = 100
_EDITABLE = ("phone_number", "customer_name", "amount", "billing_period", "due_date", "invoice_number",
             "service_name", "voice_id", "account_number", "amount_paid", "email_domain")


def _input_text(field: str, value) -> str:
    if value is None:
        return ""
    if field == "due_date":
        d = parse_date(value)
        return d.isoformat() if d else str(value)
    if field == "billing_period":
        return normalize_period(value) or ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _check_rows(rows: list[dict], default_voice: str, call_type: str = "overdue") -> list[dict]:
    """Validate rows, flagging the same invoice (or the same number + amount) appearing twice."""
    first_seen: dict[tuple, int] = {}
    checked = []
    for i, raw in enumerate(rows):
        row_no = raw.get("row", i + 1)
        call, errors = validate_call({**raw, "call_type": call_type}, default_voice=default_voice)
        if call:
            ctx = call["ctx"]
            key = (call["phone_number"], ctx["invoice_number"] or f"amount:{ctx['amount']}")
            if key in first_seen:
                errors, call = [f"Duplicate of row {first_seen[key]}"], None
            else:
                first_seen[key] = row_no
        checked.append({
            "row": row_no,
            "input": {f: _input_text(f, raw.get(f)) for f in _EDITABLE},
            "call": call,
            "errors": errors,
            "skip_reason": raw.get("_skip"),
            "filters": raw.get("_filters") or {},
            "source": raw.get("_source") or {},
        })
    return checked


@app.post("/batches/preview")
async def preview_batch(file: UploadFile = File(...), default_voice: str = Query(DEFAULT_VOICE)) -> JSONResponse:
    if default_voice not in VOICE_IDS:
        default_voice = DEFAULT_VOICE
    data = await file.read(MAX_BYTES + 1)
    try:
        fmt, rows = parse_rows(file.filename or "upload", data)
    except BatchFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    checked = _check_rows(rows, default_voice)
    return JSONResponse({"file_name": file.filename, "format": fmt, "rows": checked})


@app.post("/batches")
async def create_batch(request: Request) -> JSONResponse:
    data = await request.json()
    file_name = str(data.get("file_name") or "Upload").strip()[:120]
    default_voice = data.get("default_voice") if data.get("default_voice") in VOICE_IDS else DEFAULT_VOICE
    call_type = "predue" if data.get("call_type") == "predue" else "overdue"
    rows = data.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="No rows to call")
    if len(rows) > MAX_ROWS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_ROWS} calls per batch")

    checked = _check_rows(rows, default_voice, call_type)
    bad = [r for r in checked if not r["call"]]
    if bad:
        first = bad[0]
        raise HTTPException(status_code=400, detail=f"Row {first['row']}: {'; '.join(first['errors'])}")

    batch_id = uuid.uuid4().hex
    sources = [r.get("source") if isinstance(r.get("source"), dict) else None for r in rows]
    user = current_user(request)
    await db.create_batch(
        batch_id, file_name, call_type,
        [(uuid.uuid4().hex, r["call"]["phone_number"], r["call"]["ctx"], src) for r, src in zip(checked, sources)],
        owner=user,
    )
    request.app.state.dispatcher.wake()
    logger.info(f"Batch {batch_id} created by {user['email']} from '{file_name}' with {len(checked)} calls")
    return JSONResponse(await db.get_batch(batch_id))


@app.get("/batches")
async def list_batches(request: Request, limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    return JSONResponse(await db.list_batches(limit, owner_scope(request)))


@app.get("/batches/template.xlsx")
async def batch_template() -> Response:
    return Response(
        template_workbook(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="collection-calls-template.xlsx"', "Cache-Control": "no-store"},
    )


async def _visible_batch(request: Request, batch_id: str) -> dict:
    batch = await db.get_batch(batch_id)
    if not batch or not can_see(request, batch["created_by"]):
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@app.get("/batches/{batch_id}")
async def get_batch(request: Request, batch_id: str) -> JSONResponse:
    return JSONResponse(await _visible_batch(request, batch_id))


@app.post("/batches/{batch_id}/cancel")
async def cancel_batch(request: Request, batch_id: str) -> JSONResponse:
    await _visible_batch(request, batch_id)
    cancelled = await db.cancel_batch(batch_id)
    logger.info(f"Batch {batch_id}: {current_user(request)['email']} cancelled {cancelled} queued calls")
    return JSONResponse({"cancelled": cancelled, **(await db.get_batch(batch_id))})


# ── Voice library ──────────────────────────────────────────────────────────────


@app.get("/voices/catalog")
async def voice_catalog() -> dict:
    return {
        "voices": VOICES,
        "default_voice": DEFAULT_VOICE,
        "languages": list(LANGUAGE_CODES),
        "samples": {lang: sample_text(lang) for lang in LANGUAGE_CODES},
        "max_chars": MAX_PREVIEW_CHARS,
    }


@app.get("/voices/{voice}/preview")
async def voice_preview(request: Request, voice: str, language: str = "English", text: str = "") -> FileResponse:
    if voice not in VOICE_IDS:
        raise HTTPException(status_code=404, detail="Unknown voice")
    if language not in LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail="Language must be English or Hindi")
    text = text.strip() or sample_text(language)
    if len(text) > MAX_PREVIEW_CHARS:
        raise HTTPException(status_code=400, detail=f"Sample text is limited to {MAX_PREVIEW_CHARS} characters")
    try:
        path = await preview_audio(request.app.state.session, voice, language, text)
    except VoicePreviewError as e:
        logger.error(f"Voice preview failed for {voice}/{language}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "public, max-age=86400"})


# ── SmartFlow bi-directional stream ────────────────────────────────────────────


@app.websocket("/ws")
async def smartflow_stream(websocket: WebSocket):
    await websocket.accept()

    try:
        start = await read_stream_start(websocket)
    except Exception as e:
        logger.warning(f"SmartFlow handshake ended before start event: {e!r}")
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()
        return

    logger.info(f"SmartFlow start event: {start.raw}")

    customer_number = start.to_number if start.direction == "outbound" else start.from_number
    ref_id = str(start.custom_parameters.get("ref_id") or "")
    call = await db.get_call(ref_id) if ref_id else None
    if not call and start.direction == "outbound":
        call = await db.find_recent_initiated_call(customer_number)
        if call:
            logger.warning(f"No ref_id in start event — matched call {call['ref_id']} by customer number")

    if call:
        ref_id = call["ref_id"]
        ctx = {**DEFAULT_CONTEXT, **db.context_from_row(call)}
        due = parse_date(ctx.get("due_date"))
        if due:  # recompute on the day of the call; the queue may be older
            ctx["days_overdue"] = str((date.today() - due).days)
    else:
        if ref_id:
            logger.warning(f"ref_id={ref_id} not found — using default call context")
        ref_id = uuid.uuid4().hex
        ctx = dict(DEFAULT_CONTEXT)
        await db.insert_call(ref_id, customer_number, ctx, status="active")

    await db.mark_call_started(ref_id, start.call_sid, start.stream_sid, start.direction)

    try:
        result = await run_bot(websocket, start, ctx, ref_id)
    except Exception as e:
        logger.exception(f"[{ref_id}] Bot error")
        await db.mark_call_ended(ref_id, "failed", None, str(e))
        return

    await db.insert_transcript(ref_id, result.transcript)
    await db.mark_call_ended(ref_id, "completed", result.recording_path)
    logger.info(f"[{ref_id}] Stored transcript — {len(result.transcript)} turns")

    websocket.app.state.emailer.wake()  # emails the summary to the user who started the call


# ── Call logs ──────────────────────────────────────────────────────────────────


@app.get("/logs")
async def get_logs(request: Request, limit: int = Query(50, ge=1, le=1000), batch_id: str | None = None,
                   user_id: int | None = None) -> JSONResponse:
    """user_id (admin only) narrows the list to calls one user started."""
    scope = owner_scope(request)
    return JSONResponse(await db.list_calls(limit, batch_id, scope if scope is not None else user_id))


async def _visible_call(request: Request, ref_id: str) -> dict:
    call = await db.get_call(ref_id)
    if not call or not can_see(request, call["created_by"]):
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.get("/logs/{ref_id}")
async def get_log_detail(request: Request, ref_id: str) -> JSONResponse:
    call = await _visible_call(request, ref_id)
    return JSONResponse({"call": call, "transcript": await db.get_transcript(ref_id)})


@app.post("/logs/{ref_id}/send-email")
async def resend_call_email(request: Request, ref_id: str) -> JSONResponse:
    """Send (or re-send) the summary email to the user who started the call — never to another address."""
    call = await _visible_call(request, ref_id)
    if call["status"] != "completed":
        raise HTTPException(status_code=400, detail="Only completed calls have a summary to email")
    if not is_email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured on the server yet")
    res = await send_call_email(ref_id)
    if not res.get("success"):
        raise HTTPException(status_code=502, detail=res.get("error") or res.get("reason") or "The email could not be sent")
    return JSONResponse(res)


@app.post("/email/test")
async def test_email_endpoint(request: Request) -> JSONResponse:
    """Admin only: send a sample summary to your own address to check the email settings."""
    recipient = require_admin(request)["email"]
    if not is_email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured on the server yet")
    sample_call = {
        "customer_name": "Test Customer",
        "phone_number": "9876543210",
        "amount": "10,000",
        "service_name": "Monthly Telecom Services (Voice, Internet & Business Connectivity)",
        "billing_period": "September 2026",
        "initiator_email": recipient,
    }
    sample_transcript = [
        {"role": "assistant", "text": "Hi, this is Arjun from Tata Tele services regarding a pending payment for your telecom services. Would you like to continue in English or Hindi?"},
        {"role": "user", "text": "Can you continue in English?"},
        {"role": "assistant", "text": "Sure. Regarding the ten thousand rupees due for your September 2026 services, could you please let me know when we can expect the payment?"},
        {"role": "user", "text": "I will make the payment in two days."},
        {"role": "assistant", "text": "Thank you for confirming. I have noted that you will make the payment within the next two days. Have a great day!"},
    ]
    test_ref = "test-" + uuid.uuid4().hex[:8]
    res = await trigger_call_completed_email(ref_id=test_ref, transcript=sample_transcript, call_details=sample_call)
    return JSONResponse(res)


@app.get("/recordings/{ref_id}")
async def get_recording(request: Request, ref_id: str) -> FileResponse:
    call = await _visible_call(request, ref_id)
    if not call.get("recording_path") or not Path(call["recording_path"]).is_file():
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(call["recording_path"], media_type="audio/wav")
