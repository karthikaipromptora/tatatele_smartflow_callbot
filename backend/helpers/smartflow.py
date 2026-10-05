import asyncio
import json
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
        return StreamStart(
            stream_sid=stream_sid,
            call_sid=start.get("callSid", ""),
            account_sid=start.get("accountSid", ""),
            from_number=start.get("from", ""),
            to_number=start.get("to", ""),
            direction=start.get("direction", ""),
            custom_parameters=start.get("customParameters") or {},
            raw=message,
        )


class ClickToCallNotConfigured(RuntimeError):
    pass


async def initiate_click_to_call(session: aiohttp.ClientSession, to_number: str, ref_id: str) -> dict:
    """Ask SmartFlow to dial `to_number`; `ref_id` must be sent as a custom parameter
    so it comes back in the stream's start.customParameters."""
    # TODO: implement once the SmartFlow Click to Call Support API spec is available.
    raise ClickToCallNotConfigured(
        "SmartFlow Click to Call is not integrated yet (API spec pending)"
    )
