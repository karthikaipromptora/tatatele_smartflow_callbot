"""Mock SmartFlow call where the customer never speaks: checks Arjun's silence handling.

Expected: greeting → ~15 s silence → "are you still there?" → ~15 s silence → goodbye → stream closed.
Run from backend/ while the server is up:
    uv run python tests/mock_idle_check.py --url ws://127.0.0.1:8017/ws
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mock_smartflow_client import MockSmartFlow, db, DEFAULT_CONTEXT  # noqa: E402


async def main(args) -> int:
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{' — ' + detail if detail else ''}")

    await db.init_db()
    ref_id = f"mock-idle-{uuid.uuid4().hex[:8]}"
    await db.insert_call(ref_id, "+919999999999", {**DEFAULT_CONTEXT, "customer_name": "Silent Customer"})
    stream_sid, call_sid = f"MZ{uuid.uuid4().hex}", f"CA{uuid.uuid4().hex}"
    closed_at = None
    try:
        async with websockets.connect(args.url) as ws:
            sf = MockSmartFlow(ws, stream_sid)
            await ws.send(json.dumps({"event": "connected"}))
            await sf.send({"event": "start", "streamSid": stream_sid, "start": {
                "accountSid": "ACmock", "streamSid": stream_sid, "callSid": call_sid, "from": "+911234567890",
                "to": "+919999999999", "direction": "outbound",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "bitRate": 64, "bitDepth": 8},
                "customParameters": {"ref_id": ref_id}}})
            sender = asyncio.create_task(sf.sender())       # sends silence only
            receiver = asyncio.create_task(sf.receiver())

            check("greeting received", await sf.wait_bot_turn(after=0))
            greeting_end = time.monotonic()
            print("Customer stays silent…")

            ok = await sf.wait_bot_turn(after=greeting_end + 1, start_timeout=25)
            check_in = sf.media_events[-1] if ok else None
            first_gap = (check_in_start := next(t for t in sf.media_events if t > greeting_end + 1)) - greeting_end if ok else None
            check("bot checks in after ~15 s of silence", ok and 13 <= first_gap <= 20, f"after {first_gap:.1f}s" if first_gap else "no check-in")

            ok2 = await sf.wait_bot_turn(after=check_in + 1, start_timeout=25) if ok else False
            second_gap = (next(t for t in sf.media_events if t > check_in + 1) - check_in) if ok2 else None
            check("bot says goodbye after another ~15 s", ok2 and 13 <= second_gap <= 20, f"after {second_gap:.1f}s" if second_gap else "no goodbye")

            try:
                await asyncio.wait_for(receiver, timeout=20)  # ends when the server closes the stream
                closed_at = time.monotonic()
            except asyncio.TimeoutError:
                pass
            check("server closes the stream after the goodbye", closed_at is not None,
                  f"{closed_at - greeting_end:.1f}s after the greeting" if closed_at else "still open after 20 s")
            sender.cancel()
    except websockets.ConnectionClosed:
        closed_at = closed_at or time.monotonic()

    call = None
    for _ in range(30):
        call = await db.get_call(ref_id)
        if call and call["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.5)
    transcript = await db.get_transcript(ref_id)
    check("call recorded as completed", bool(call) and call["status"] == "completed", call and call["status"])
    print("\nTranscript:")
    for t in transcript:
        print(f"  {t['role']:>9}: {t['text']}")
    if not args.keep:  # this is a test call — don't leave it in the call logs
        async with db._pool.acquire() as conn:
            await conn.execute("DELETE FROM calls WHERE ref_id=$1", ref_id)
    await db.close_db()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=f"ws://127.0.0.1:{os.getenv('PORT', '8011')}/ws")
    parser.add_argument("--keep", action="store_true", help="keep the test call in the call logs")
    sys.exit(asyncio.run(main(parser.parse_args())))
