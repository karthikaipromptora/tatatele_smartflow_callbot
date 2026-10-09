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


# Run test assertions
ctx = {
    "customer_name": "Teja Abhishek",
    "service_name": "Tata Tele BroadBand",
    "amount": "15,000",
    "billing_period": "August 2026",
    "due_date": "15 August 2026",
    "invoice_number": "INV-8899",
    "call_type": "overdue",
}

cache = ReceiverResponseCache(ctx)

import unittest

class TestReceiverCache(unittest.TestCase):
    def setUp(self):
        self.ctx = {
            "customer_name": "Teja Abhishek",
            "service_name": "Tata Tele BroadBand",
            "amount": "15,000",
            "billing_period": "August 2026",
            "due_date": "15 August 2026",
            "invoice_number": "INV-8899",
            "call_type": "overdue",
        }
        self.cache = ReceiverResponseCache(self.ctx)

    def test_language_triggers(self):
        test_cases = [
            ("English please", True, "English"),
            ("Yes English", True, "English"),
            ("In English", True, "English"),
            ("can you speak in english", True, "English"),
            ("can we continue in english", True, "English"),
            ("Hindi please", True, "Hindi"),
            ("Haan Hindi", True, "Hindi"),
            ("Hindi mein", True, "Hindi"),
            ("हिंदी में", True, "Hindi"),
            ("हिंदी में बोलिए", True, "Hindi"),
            ("hindi me baat karo", True, "Hindi"),
            ("Who are you?", False, None),
            ("Kaun hai?", False, None),
            ("Why did you call?", False, None),
            ("Wrong number please disconnect", False, None),
            ("I am driving right now", False, None),
            ("I am in a hospital meeting", False, None),
            ("Call me later", False, None),
            ("I have already paid", False, None),
        ]
        for text, should_match, expected_lang in test_cases:
            c = ReceiverResponseCache(self.ctx)
            res = c.match_intent(text)
            if should_match:
                self.assertIsNotNone(res, f"Expected match for {text!r}")
                if expected_lang == "English":
                    self.assertIn("15,000 rupees", res)
                else:
                    self.assertIn("15,000 rupees", res)
            else:
                self.assertIsNone(res, f"Expected None for {text!r}")

    def test_detail_queries(self):
        detail_cases = [
            ("Amount kitna hai?", "The amount due is 15,000 rupees."),
            ("kitna bill hai", "The amount due is 15,000 rupees."),
            ("how much is due", "The amount due is 15,000 rupees."),
            ("what is the invoice number", "Your invoice number is INV-8899."),
            ("invoice number kya hai", "Your invoice number is INV-8899."),
            ("due date kab thi", "The payment was due on 15 August 2026."),
            ("when is the due date", "The payment was due on 15 August 2026."),
        ]
        c = ReceiverResponseCache(self.ctx)
        c.turn_count = 2
        for q, expected in detail_cases:
            self.assertEqual(c.match_intent(q), expected)

if __name__ == "__main__":
    unittest.main()
