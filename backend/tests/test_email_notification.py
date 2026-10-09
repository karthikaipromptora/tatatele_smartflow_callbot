"""Unit tests for the email notification and transcript summary service."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv

load_dotenv()

from helpers.email_sender import (
    _format_transcript_text,
    build_email_html,
    build_transcript_attachment,
    generate_transcript_summary,
    get_email_config,
    is_outlook_configured,
    trigger_call_completed_email,
)


class TestEmailNotification(unittest.TestCase):

    def setUp(self):
        self.sample_call = {
            "ref_id": "test_ref_12345678",
            "customer_name": "Abhishek Sharma",
            "phone_number": "9876543210",
            "amount": "10,000",
            "billing_period": "September 2026",
            "service_name": "Monthly Telecom Services",
            "invoice_number": "INV-2026-9901",
            "due_date": "20 September 2026",
            "initiator_email": "collections.user@example.com",
            "ended_at": "2026-10-08T06:30:00+00:00",
            "call_type": "overdue",
        }
        self.sample_transcript = [
            {"role": "assistant", "text": "Hi, this is Arjun from Tata Tele Business Services regarding your pending payment. Would you like to continue in English or Hindi?"},
            {"role": "user", "text": "English please. I had a busy week."},
            {"role": "assistant", "text": "I understand. Can you let us know when we can expect the payment of ten thousand rupees?"},
            {"role": "user", "text": "I will do the payment in 2 days."},
            {"role": "assistant", "text": "Thank you for confirming. I have noted that you will make the payment within two days."},
        ]

    def test_format_transcript_text(self):
        text = _format_transcript_text(self.sample_transcript)
        self.assertIn("Bot (Arjun):", text)
        self.assertIn("Customer:", text)
        self.assertIn("I will do the payment in 2 days.", text)

    def test_build_transcript_attachment(self):
        filename, raw_bytes = build_transcript_attachment(self.sample_call, self.sample_transcript, self.sample_call["ref_id"])
        self.assertTrue(filename.startswith("transcript_Abhishek_Sharma_"))
        self.assertTrue(filename.endswith(".txt"))
        decoded = raw_bytes.decode("utf-8")
        self.assertIn("TATA TELE BUSINESS SERVICES - SMARTFLOW CALL TRANSCRIPT", decoded)
        self.assertIn("Abhishek Sharma", decoded)
        self.assertIn("9876543210", decoded)
        self.assertIn("INR 10,000", decoded)
        self.assertIn("I will do the payment in 2 days.", decoded)

    def test_build_email_html(self):
        summary_info = {
            "summary": "Customer promised to pay within 2 days.",
            "commitment": "Within 2 days",
            "sentiment": "Cooperative",
        }
        html = build_email_html(self.sample_call, summary_info, self.sample_call["ref_id"])
        self.assertIn("Tata Tele SmartFlow &bull; Call Summary", html)
        self.assertIn("Abhishek Sharma", html)
        self.assertIn("9876543210", html)
        self.assertIn("Customer promised to pay within 2 days.", html)
        self.assertIn("collections.user@example.com", html)
        self.assertIn("08 October 2026, 12:00 PM IST", html)  # ended_at shown in IST
        self.assertIn("Full Transcript Attached", html)

    def test_asterisk_removal_from_summary(self):
        sample_with_stars = {
            "summary": "**Executive Summary:**\nCustomer was *busy*.\n\n**Customer Commitment & Timeline:**\n* Promised in **2 days**.\n* Will transfer online.\n\n***Customer Sentiment:*** Cooperative.",
        }
        html = build_email_html(self.sample_call, sample_with_stars, self.sample_call["ref_id"])
        self.assertNotIn("*", html)
        self.assertIn("Executive Summary:", html)
        self.assertIn("Customer Commitment &amp; Timeline:", html)
        self.assertIn("Customer was busy.", html)

    def test_empty_transcript_handling(self):
        res = asyncio.run(generate_transcript_summary(self.sample_call, []))
        self.assertIn("no customer conversation turns were recorded", res["summary"])
        filename, raw_bytes = build_transcript_attachment(self.sample_call, [], self.sample_call["ref_id"])
        decoded = raw_bytes.decode("utf-8")
        self.assertIn("(No dialogue recorded during this call)", decoded)

    def test_html_escapes_uploaded_values(self):
        call = {**self.sample_call, "customer_name": "<img src=x onerror=alert(1)> Ltd", "invoice_number": "<b>9</b>"}
        html = build_email_html(call, {"summary": "Said <script>bad()</script> then agreed."}, call["ref_id"])
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; Ltd", html)
        self.assertIn("&lt;b&gt;9&lt;/b&gt;", html)

    def test_trigger_email_safe_when_unconfigured(self):
        # Without Outlook or SMTP settings the trigger reports why and never raises.
        blank = {k: "" for k in ("OUTLOOK_TENANT_ID", "OUTLOOK_CLIENT_ID", "OUTLOOK_CLIENT_SECRET", "OUTLOOK_SENDER_EMAIL",
                                 "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")}
        with patch.dict(os.environ, blank),              patch("helpers.email_sender.generate_transcript_summary", AsyncMock(return_value={"summary": "ok"})):
            res = asyncio.run(trigger_call_completed_email("ref_abc", self.sample_transcript, self.sample_call))
        self.assertFalse(res["success"])
        self.assertEqual(res["method"], "none")
        self.assertIn("Outlook credentials not configured in .env", res["reason"])
        self.assertEqual(res["recipient"], "collections.user@example.com")

    def test_no_recipient_means_no_email(self):
        # A call nobody started from the dashboard (e.g. inbound) is never emailed to a fallback address.
        call = {**self.sample_call, "initiator_email": ""}
        res = asyncio.run(trigger_call_completed_email("ref_in", self.sample_transcript, call))
        self.assertFalse(res["success"])
        self.assertNotIn("recipient", res)

    def test_email_config_has_no_default_recipient(self):
        self.assertNotIn("default_recipient", get_email_config())

if __name__ == "__main__":
    unittest.main()
