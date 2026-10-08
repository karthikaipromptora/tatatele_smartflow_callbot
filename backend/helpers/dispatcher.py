import asyncio
import os
import time

import aiohttp
from loguru import logger

from helpers import db
from helpers.smartflow import ClickToCallError, initiate_click_to_call


class CallRateLimiter:
    """Spaces Click to Call requests so single and batch calls together stay within the account's CPS."""

    def __init__(self, gap_seconds: float):
        self._gap = gap_seconds
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    @property
    def gap(self) -> float:
        return self._gap

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self._gap
        if slot > now:
            await asyncio.sleep(slot - now)


async def place_call(session: aiohttp.ClientSession, limiter: CallRateLimiter, ref_id: str, phone_number: str) -> str:
    """Dial through SmartFlow, recording the outcome on the call row. Raises ClickToCallError on rejection."""
    await limiter.wait()
    try:
        smartflow_ref_id = await initiate_click_to_call(session, phone_number, ref_id)
    except ClickToCallError as e:
        await db.mark_call_failed(ref_id, str(e))
        raise
    except Exception as e:
        await db.mark_call_failed(ref_id, f"Could not reach SmartFlow: {e}")
        raise ClickToCallError(f"Could not reach SmartFlow: {e}") from e
    await db.set_smartflow_ref(ref_id, smartflow_ref_id)
    return smartflow_ref_id


class BatchDispatcher:
    """Single worker that dials queued calls in order. Queue state lives in Postgres, so a restart resumes it."""

    def __init__(self, session: aiohttp.ClientSession, limiter: CallRateLimiter):
        self._session = session
        self._limiter = limiter
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="batch-dispatcher")

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
        logger.info(f"Batch dispatcher started (one call every {self._limiter.gap:g}s)")
        while True:
            try:
                call = await db.claim_next_queued_call()
            except Exception:
                logger.exception("Dispatcher could not read the queue; retrying in 5s")
                await asyncio.sleep(5)
                continue
            if not call:
                # Queued calls may exist but be waiting for an earlier call to the same number to finish.
                try:
                    blocked = await db.has_queued_calls()
                except Exception:
                    blocked = False
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5 if blocked else 30)
                except asyncio.TimeoutError:
                    pass
                continue
            ref_id = call["ref_id"]
            try:
                sf_ref = await place_call(self._session, self._limiter, ref_id, call["phone_number"])
                logger.info(f"[{ref_id}] Batch call queued to={call['phone_number']} batch={call['batch_id']} smartflow_ref_id={sf_ref}")
            except ClickToCallError as e:
                logger.error(f"[{ref_id}] Batch call failed: {e}")


def gap_from_env() -> float:
    try:
        return max(0.2, float(os.getenv("SMARTFLOW_CALL_GAP_SECONDS", "2")))
    except ValueError:
        return 2.0
