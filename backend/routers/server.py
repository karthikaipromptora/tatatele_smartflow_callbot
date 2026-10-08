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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.websockets import WebSocketState

load_dotenv(override=True)

from helpers import db
from helpers.batch_file import FILTER_COLUMNS, MAX_BYTES, MAX_ROWS, BatchFileError, parse_rows, template_workbook
from helpers.bot import run_bot
from helpers.call_input import normalize_period, parse_date, validate_call
from helpers.dispatcher import BatchDispatcher, CallRateLimiter, gap_from_env, place_call
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    app.state.session = aiohttp.ClientSession()
    app.state.limiter = CallRateLimiter(gap_from_env())
    app.state.dispatcher = BatchDispatcher(app.state.session, app.state.limiter)
    app.state.dispatcher.start()
    yield
    await app.state.dispatcher.stop()
    await app.state.session.close()
    await db.close_db()


app = FastAPI(title="Tata Tele SmartFlow Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Dashboard pages share one client-side app.
@app.get("/")
@app.get("/calls")
@app.get("/voices")
@app.get("/settings")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/config")
async def config(request: Request) -> dict:
    return {
        "call_gap_seconds": request.app.state.limiter.gap,
        "batch_max_rows": MAX_ROWS,
        "default_voice": DEFAULT_VOICE,
        "min_amount": MIN_AMOUNT,
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
    await db.insert_call(ref_id, phone_number, ctx)

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
    await db.create_batch(
        batch_id, file_name, call_type,
        [(uuid.uuid4().hex, r["call"]["phone_number"], r["call"]["ctx"], src) for r, src in zip(checked, sources)],
    )
    request.app.state.dispatcher.wake()
    logger.info(f"Batch {batch_id} created from '{file_name}' with {len(checked)} calls")
    return JSONResponse(await db.get_batch(batch_id))


@app.get("/batches")
async def list_batches(limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    return JSONResponse(await db.list_batches(limit))


@app.get("/batches/template.xlsx")
async def batch_template() -> Response:
    return Response(
        template_workbook(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="collection-calls-template.xlsx"', "Cache-Control": "no-store"},
    )


@app.get("/batches/{batch_id}")
async def get_batch(batch_id: str) -> JSONResponse:
    batch = await db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return JSONResponse(batch)


@app.post("/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str) -> JSONResponse:
    if not await db.get_batch(batch_id):
        raise HTTPException(status_code=404, detail="Batch not found")
    cancelled = await db.cancel_batch(batch_id)
    logger.info(f"Batch {batch_id}: cancelled {cancelled} queued calls")
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


# ── Call logs ──────────────────────────────────────────────────────────────────


@app.get("/logs")
async def get_logs(limit: int = Query(50, ge=1, le=1000), batch_id: str | None = None) -> JSONResponse:
    return JSONResponse(await db.list_calls(limit, batch_id))


@app.get("/logs/{ref_id}")
async def get_log_detail(ref_id: str) -> JSONResponse:
    call = await db.get_call(ref_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return JSONResponse({"call": call, "transcript": await db.get_transcript(ref_id)})


@app.get("/recordings/{ref_id}")
async def get_recording(ref_id: str) -> FileResponse:
    call = await db.get_call(ref_id)
    if not call or not call.get("recording_path") or not Path(call["recording_path"]).is_file():
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(call["recording_path"], media_type="audio/wav")
