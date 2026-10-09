"""Receiver-specific fast-path response cache.

Generates deterministic, low-latency responses for predictable turns
(such as Turn 1 language selection and explicit detail inquiries like amount,
due date, and invoice number) based on the receiver's known call context.

If the user utterance does not cleanly match a known intent, it falls back
to the remote LLM so there is zero confusion or broken conversation flow.
"""

import re
from typing import Optional
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ReceiverResponseCache:
    """Pre-computes and matches cached responses for a specific call receiver."""

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.call_type = ctx.get("call_type", "overdue")
        self.is_predue = self.call_type == "predue"
        self.customer_name = (ctx.get("customer_name") or "").strip()
        self.service_name = (ctx.get("service_name") or "").strip() or "Monthly Telecom Services"
        self.amount = (ctx.get("amount") or "").strip()
        self.billing_period = (ctx.get("billing_period") or "").strip()
        self.due_date = (ctx.get("due_date") or "").strip()
        self.invoice_number = (ctx.get("invoice_number") or "").strip()

        # Build cached responses for Turn 1
        self.turn1_english = self._build_turn1_english()
        self.turn1_hindi = self._build_turn1_hindi()

        # Detail query responses
        self.amount_reply_en = f"The amount due is {self.amount} rupees." if self.amount else ""
        self.amount_reply_hi = f"कुल बकाया राशि {self.amount} rupees है।" if self.amount else ""

        self.due_reply_en = (
            f"The payment due date is {self.due_date}."
            if self.is_predue
            else f"The payment was due on {self.due_date}."
        ) if self.due_date else ""
        self.due_reply_hi = f"Payment की due date {self.due_date} थी।" if self.due_date else ""

        self.invoice_reply_en = f"Your invoice number is {self.invoice_number}." if self.invoice_number else ""
        self.invoice_reply_hi = f"आपका invoice number {self.invoice_number} है।" if self.invoice_number else ""

        # State tracking for this call session
        self.turn_count = 0
        self.active_language = ctx.get("language", "English")

    def _build_turn1_english(self) -> str:
        due_str = f" which is due on {self.due_date}" if (self.is_predue and self.due_date) else ""
        if self.is_predue:
            return (
                f"Thank you. This is a friendly reminder regarding an upcoming payment of {self.amount} rupees "
                f"for your {self.service_name} for the billing period of {self.billing_period}{due_str}. "
                f"Could you please confirm if you will be able to make the payment by the due date?"
            )
        return (
            f"Thank you. Regarding the pending payment of {self.amount} rupees for your {self.service_name} "
            f"for the {self.billing_period} billing period, could you please let me know when we can expect the payment?"
        )

    def _build_turn1_hindi(self) -> str:
        if self.is_predue:
            return (
                f"धन्यवाद। आपके {self.service_name} के {self.billing_period} के {self.amount} rupees के "
                f"upcoming payment का एक reminder देने के लिए call किया है। क्या आप due date तक payment कर पाएंगे?"
            )
        return (
            f"धन्यवाद। आपके {self.service_name} के {self.billing_period} के {self.amount} rupees के "
            f"pending payment के बारे में, क्या आप बता सकते हैं कि payment कब तक हो पाएगा?"
        )

    def match_intent(self, text: str) -> Optional[str]:
        """Matches user text to a cached response. Returns None if ambiguous or unhandled."""
        clean = re.sub(r"[^\w\s\u0900-\u097F]", "", text.strip().lower())
        words = clean.split()
        if not words:
            return None

        self.turn_count += 1

        # Check for Turn 1 Language Selection
        if self.turn_count <= 2:
            lang_match = self._match_language_choice(clean, words)
            if lang_match == "English":
                self.active_language = "English"
                return self.turn1_english
            elif lang_match == "Hindi":
                self.active_language = "Hindi"
                return self.turn1_hindi

        # Check for explicit Detail Queries (Amount, Invoice Number, Due Date)
        detail_reply = self._match_detail_query(clean)
        if detail_reply:
            return detail_reply

        # Fall back to LLM for all open-ended / complex / custom messages
        return None

    def _match_language_choice(self, clean: str, words: list[str]) -> Optional[str]:
        disqualifiers = {
            "who", "kaun", "why", "kyun", "kisse", "what", "kya", "wrong", "busy",
            "paid", "already", "baad", "later", "not", "nahi", "stop", "mat", "driver",
            "driving", "hospital", "meeting"
        }
        if any(w in disqualifiers for w in words):
            return None

        english_exact = {
            "english", "english please", "in english", "english is fine", "yes english",
            "yeah english", "continue in english", "speak in english", "can we continue in english",
            "can you speak in english", "can you continue in english", "english me", "english mein",
            "yes please english", "english language", "english chalega", "talk in english",
            "speak english"
        }
        if clean in english_exact:
            return "English"

        if ("english" in words and len(words) <= 4 and
                all(w in {"english", "yes", "yeah", "in", "please", "fine", "ok", "sure", "continue", "speak", "talk", "me", "mein", "is"} for w in words)):
            return "English"

        hindi_exact = {
            "hindi", "hindi please", "in hindi", "hindi is fine", "yes hindi",
            "yeah hindi", "continue in hindi", "speak in hindi", "hindi me", "hindi mein",
            "hindi me boliye", "hindi mein boliye", "hindi me baat karo", "hindi mein baat kijiye",
            "हिंदी", "हिंदी में", "हिंदी में बोलिए", "हाँ हिंदी", "हां हिंदी", "haan hindi",
            "ha hindi", "hindi chalega", "talk in hindi", "speak hindi"
        }
        if clean in hindi_exact:
            return "Hindi"

        if (("hindi" in words or "हिंदी" in clean) and len(words) <= 5 and
                all(w in {"hindi", "yes", "yeah", "haan", "ha", "in", "please", "fine", "ok", "sure", "continue", "speak", "talk", "me", "mein", "boliye", "baat", "karo", "kijiye", "हिंदी", "में", "बोलिए", "हाँ", "हां"} for w in words)):
            return "Hindi"

        return None

    def _match_detail_query(self, clean: str) -> Optional[str]:
        amount_patterns = [
            r"^(?:what\s+is\s+(?:the\s+)?amount|how\s+much\s+(?:is\s+(?:the\s+)?bill|is\s+due|amount)|amount\s+kitna\s+hai|kitna\s+(?:bill|amount|paisa|rupaye)\s+hai|kitna\s+due\s+hai)$",
            r"^(?:kitna\s+hai|how\s+much)$"
        ]
        if any(re.search(p, clean) for p in amount_patterns):
            if self.active_language == "Hindi" and self.amount_reply_hi:
                return self.amount_reply_hi
            elif self.amount_reply_en:
                return self.amount_reply_en

        invoice_patterns = [
            r"^(?:what\s+is\s+(?:the\s+)?invoice\s+number|which\s+invoice|invoice\s+number\s+kya\s+hai|invoice\s+kya\s+hai)$",
        ]
        if any(re.search(p, clean) for p in invoice_patterns):
            if self.active_language == "Hindi" and self.invoice_reply_hi:
                return self.invoice_reply_hi
            elif self.invoice_reply_en:
                return self.invoice_reply_en

        date_patterns = [
            r"^(?:when\s+is\s+(?:the\s+)?due\s+date|what\s+is\s+(?:the\s+)?due\s+date|due\s+date\s+kab\s+(?:hai|thi)|due\s+kab\s+(?:hai|tha))$",
        ]
        if any(re.search(p, clean) for p in date_patterns):
            if self.active_language == "Hindi" and self.due_reply_hi:
                return self.due_reply_hi
            elif self.due_reply_en:
                return self.due_reply_en

        return None


class ReceiverResponseCacheProcessor(FrameProcessor):
    """Pipecat FrameProcessor that intercepts LLMContextFrame to check the receiver cache.

    If a fast cached response is matched, it emits the response directly to TTS
    (and assistant aggregator) while bypassing the remote LLM completely.
    Otherwise, it passes the frame untouched down the pipeline to the LLM.
    """

    def __init__(self, cache: ReceiverResponseCache, ref_id: str):
        super().__init__()
        self._cache = cache
        self._ref_id = ref_id

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            user_text = ""
            if frame.context and frame.context.messages:
                for m in reversed(frame.context.messages):
                    if m.get("role") == "user":
                        user_text = str(m.get("content") or "")
                        break

            cached_reply = self._cache.match_intent(user_text) if user_text else None
            if cached_reply:
                logger.info(
                    f"[{self._ref_id}] Receiver cache HIT for user='{user_text}' "
                    f"-> bypassing LLM with: '{cached_reply}'"
                )
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(TextFrame(text=cached_reply))
                await self.push_frame(LLMFullResponseEndFrame())
                return

        await self.push_frame(frame, direction)
