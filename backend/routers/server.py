import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

load_dotenv(override=True)

from helpers import db
from helpers.bot import run_bot
from helpers.prompts import DEFAULT_CONTEXT
from helpers.smartflow import ClickToCallNotConfigured, initiate_click_to_call, read_stream_start

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PHONE_RE = re.compile(r"^\+?\d{10,15}$")

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

# ── App ────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    app.state.session = aiohttp.ClientSession()
    yield
    await app.state.session.close()
    await db.close_db()


app = FastAPI(title="Tata Tele SmartFlow Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── Outbound call trigger ──────────────────────────────────────────────────────


@app.post("/start")
async def start_call(request: Request) -> JSONResponse:
    data = await request.json()

    phone_number = re.sub(r"[\s\-()]", "", str(data.get("phone_number", "")))
    if not PHONE_RE.match(phone_number):
        raise HTTPException(status_code=400, detail="Enter a valid phone number (10–15 digits, optional +)")

    ctx = {
        key: (str(data.get(key) or "").strip() or default)
        for key, default in DEFAULT_CONTEXT.items()
    }

    ref_id = uuid.uuid4().hex
    await db.insert_call(ref_id, phone_number, ctx)

    try:
        await initiate_click_to_call(request.app.state.session, phone_number, ref_id)
    except ClickToCallNotConfigured as e:
        await db.mark_call_failed(ref_id, str(e))
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        logger.exception(f"[{ref_id}] Click to Call failed")
        await db.mark_call_failed(ref_id, str(e))
        raise HTTPException(status_code=502, detail=f"Click to Call failed: {e}")

    logger.info(f"[{ref_id}] Outbound call requested to={phone_number} customer={ctx['customer_name']}")
    return JSONResponse({"ref_id": ref_id, "status": "initiated"})


# ── SmartFlow bi-directional stream ────────────────────────────────────────────


@app.websocket("/ws")
async def smartflow_stream(websocket: WebSocket):
    await websocket.accept()

    try:
        start = await read_stream_start(websocket)
    except Exception as e:
        logger.error(f"SmartFlow handshake failed: {e}")
        await websocket.close()
        return

    logger.info(f"SmartFlow start event: {start.raw}")

    ref_id = start.custom_parameters.get("ref_id") or ""
    call = await db.get_call(ref_id) if ref_id else None
    if call:
        ctx = {key: call[key] for key in DEFAULT_CONTEXT}
    else:
        if ref_id:
            logger.warning(f"ref_id={ref_id} not found — using default call context")
        ref_id = uuid.uuid4().hex
        ctx = dict(DEFAULT_CONTEXT)
        customer_number = start.to_number if start.direction == "outbound" else start.from_number
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
async def get_logs() -> JSONResponse:
    return JSONResponse(await db.list_calls())


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
