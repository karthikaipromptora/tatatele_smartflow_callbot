import asyncio
import json
import os
import re
from dataclasses import dataclass

import aiohttp
from fastapi import WebSocket


@dataclass
class StreamStart:
    stream_sid: str
    call_sid: str
    account_sid: str
    from_number: str
    to_number: str
    direction: str
    custom_parameters: dict
    raw: dict


async def read_stream_start(websocket: WebSocket, timeout: float = 10.0) -> StreamStart:
    """Consume SmartFlow's `connected` handshake and return the `start` event metadata."""
    while True:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout)
        message = json.loads(raw)
        event = message.get("event")
        if event == "connected":
            continue
        if event != "start":
            raise ValueError(f"Expected 'start' event, got {event!r}")

        start = message.get("start") or {}
        stream_sid = start.get("streamSid") or message.get("streamSid")
        if not stream_sid:
            raise ValueError("start event is missing streamSid")
        custom = start.get("customParameters") or {}
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except json.JSONDecodeError:
                custom = {}
        return StreamStart(
            stream_sid=stream_sid,
            call_sid=start.get("callSid", ""),
            account_sid=start.get("accountSid", ""),
            from_number=start.get("from", ""),
            to_number=start.get("to", ""),
            direction=start.get("direction", ""),
            custom_parameters=custom if isinstance(custom, dict) else {},
            raw=message,
        )


CLICK_TO_CALL_URL = "https://api-smartflo.tatateleservices.com/v1/click_to_call_support"


class ClickToCallError(RuntimeError):
    pass


class ClickToCallNotConfigured(ClickToCallError):
    pass


def normalize_customer_number(raw: str) -> str | None:
    """SmartFlow accepts 10–12 digit customer numbers (e.g. 9876543210 or 919876543210)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if 10 <= len(digits) <= 12 else None


async def initiate_click_to_call(session: aiohttp.ClientSession, customer_number: str, ref_id: str) -> str:
    """Ask SmartFlow to dial the customer; on answer it streams the call to the VOICE Bot
    bound to the API key. Returns SmartFlow's ref_id for the request."""
    token = os.getenv("SMARTFLOW_API_TOKEN", "").strip()
    api_key = os.getenv("SMARTFLOW_C2C_API_KEY", "").strip()
    if not token or not api_key:
        raise ClickToCallNotConfigured("Set SMARTFLOW_API_TOKEN and SMARTFLOW_C2C_API_KEY in .env")

    payload = {
        "customer_number": customer_number,
        "api_key": api_key,
        "async": 1,
        "custom_identifier": {"ref_id": ref_id},
    }
    caller_id = os.getenv("SMARTFLOW_CALLER_ID", "").strip()
    if caller_id:
        payload["caller_id"] = caller_id

    async with session.post(
        CLICK_TO_CALL_URL,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        text = await resp.text()
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"message": text[:300]}

    if resp.status != 200 or not (isinstance(body, dict) and body.get("success")):
        message = body.get("message") if isinstance(body, dict) else None
        raise ClickToCallError(f"SmartFlow rejected the call (HTTP {resp.status}): {message or body}")
    return str(body.get("ref_id", ""))
