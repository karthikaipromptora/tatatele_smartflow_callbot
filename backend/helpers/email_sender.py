"""Email notification service for completed calls.

Uses Microsoft Graph API (OAuth2 Client Credentials with Tenant ID) to send
post-call summary reports and transcript attachments via Outlook.
Falls back to SMTP if SMTP credentials are provided, or logs clear instructions
if credentials have not yet been configured in .env.
"""
from __future__ import annotations

import asyncio
import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import os
import re
import smtplib
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from loguru import logger
from openai import AsyncOpenAI


IST = timezone(timedelta(hours=5, minutes=30))

_token_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,
}
_token_lock: asyncio.Lock | None = None


def _get_token_lock() -> asyncio.Lock:
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


def get_email_config() -> dict[str, str]:
    return {
        "tenant_id": os.getenv("OUTLOOK_TENANT_ID", "").strip(),
        "client_id": os.getenv("OUTLOOK_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("OUTLOOK_CLIENT_SECRET", "").strip(),
        "sender_email": os.getenv("OUTLOOK_SENDER_EMAIL", "").strip(),
        # SMTP fallback optional config
        "smtp_host": os.getenv("SMTP_HOST", "").strip(),
        "smtp_port": os.getenv("SMTP_PORT", "587").strip(),
        "smtp_user": os.getenv("SMTP_USER", "").strip(),
        "smtp_password": os.getenv("SMTP_PASSWORD", "").strip(),
    }


def is_outlook_configured() -> bool:
    cfg = get_email_config()
    return bool(cfg["tenant_id"] and cfg["client_id"] and cfg["client_secret"] and cfg["sender_email"])


def is_smtp_configured() -> bool:
    cfg = get_email_config()
    return bool(cfg["smtp_host"] and cfg["smtp_user"] and cfg["smtp_password"])


def is_email_configured() -> bool:
    return is_outlook_configured() or is_smtp_configured()


def _ended_at_ist(call_details: dict) -> datetime:
    """When the call ended, in IST (falls back to now for calls without an end time)."""
    raw = call_details.get("ended_at")
    try:
        when = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    except ValueError:
        when = None
    if not isinstance(when, datetime):
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(IST)


# ── Microsoft Graph OAuth2 ────────────────────────────────────────────────────


async def get_graph_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Acquire a Microsoft Graph token (client credentials grant), cached until shortly before expiry."""
    actual_tenant, actual_client_id, actual_secret = tenant_id, client_id, client_secret

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    lock = _get_token_lock()
    async with lock:
        now = time.time()
        if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
            return _token_cache["access_token"]

        token_url = f"https://login.microsoftonline.com/{actual_tenant}/oauth2/v2.0/token"
        data = {
            "client_id": actual_client_id,
            "client_secret": actual_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }

        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(token_url, data=data, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Failed to acquire Microsoft Graph token (HTTP {resp.status}): {text}")
                payload = await resp.json()
                token = payload.get("access_token")
                expires_in = int(payload.get("expires_in", 3600))
                _token_cache["access_token"] = token
                _token_cache["expires_at"] = now + expires_in
                return token


# ── Transcript Summarizer ─────────────────────────────────────────────────────


def _format_transcript_text(transcript: list[dict]) -> str:
    lines = []
    for t in transcript:
        role = "Bot (Arjun)" if t.get("role") == "assistant" else "Customer"
        lines.append(f"{role}: {t.get('text', '').strip()}")
    return "\n".join(lines)


def clean_summary_text(text: str) -> str:
    """Strip all markdown asterisks, stars, and bolding artifacts so summary text is clean."""
    if not text:
        return ""
    # Strip markdown bold/italics: **Header:** -> Header:, *point* -> point
    cleaned = re.sub(r"\*+([^*]+?)\*+", r"\1", text)
    # Strip any remaining stray asterisks
    cleaned = cleaned.replace("*", "").strip()
    return cleaned


async def generate_transcript_summary(call_details: dict, transcript: list[dict]) -> dict:
    """Generate an executive summary using the local LLM with rule-based fallback."""
    if not transcript:
        return {
            "summary": "The call was initiated and completed, but no customer conversation turns were recorded.",
            "commitment": "None recorded",
            "sentiment": "Neutral / No dialogue",
            "next_steps": "Retry call during business hours or verify customer phone number.",
        }

    transcript_text = _format_transcript_text(transcript)
    llm_url = os.getenv("LOCAL_LLM_URL", "http://164.52.198.104:8049/v1")
    llm_model = os.getenv("LOCAL_LLM_MODEL", "google/gemma-4-26B-A4B-it")

    system_prompt = (
        "You are an executive collections analyst for Tata Tele Business Services. "
        "Analyze the completed call transcript between the automated voice bot (Arjun) and the customer. "
        "Provide a concise, professional assessment in clean text covering:\n"
        "1. Executive Summary: 1-2 sentence overview of what happened.\n"
        "2. Customer Commitment & Timeline: Note any promise to pay, specific dates or days mentioned (e.g., 'within 2 days'), or disputes.\n"
        "3. Customer Sentiment: Cooperative, hesitant, disputed, or unreachable.\n"
        "4. Recommended Next Step: Follow-up action for the collections officer.\n"
        "IMPORTANT RULES:\n"
        "- Do NOT use any asterisks (* or **), markdown bold stars, or bullet stars anywhere in your response.\n"
        "- Use plain section titles without asterisks (e.g. 'Executive Summary:' or 'Customer Sentiment:').\n"
        "- Present the assessment cleanly and professionally."
    )

    user_prompt = (
        f"Customer Name: {call_details.get('customer_name', 'Customer')}\n"
        f"Phone: {call_details.get('phone_number', '')}\n"
        f"Amount Due: INR {call_details.get('amount', '')}\n"
        f"Billing Period: {call_details.get('billing_period', '')}\n"
        f"Service: {call_details.get('service_name', '')}\n\n"
        f"Call Transcript:\n{transcript_text}\n"
    )

    try:
        client = AsyncOpenAI(base_url=llm_url, api_key="dummy", timeout=12.0)
        resp = await client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=350,
        )
        content = resp.choices[0].message.content or ""
        clean_content = clean_summary_text(content)
        return {"summary": clean_content, "raw_llm": True}
    except Exception as e:
        logger.warning(f"LLM summary generation failed ({e}); using rule-based fallback")
        customer_turns = [t.get("text", "") for t in transcript if t.get("role") == "user"]
        return {
            "summary": clean_summary_text(
                f"Completed call with {len(transcript)} total dialogue turns. "
                f"Customer spoke {len(customer_turns)} times. "
                f"Last customer statement: \"{customer_turns[-1] if customer_turns else 'None'}\"."
            ),
            "commitment": "Review attached transcript for exact customer statement.",
            "sentiment": "Completed",
            "next_steps": "Verify payment status in CRM.",
            "raw_llm": False,
        }


# ── Attachment Builder ────────────────────────────────────────────────────────


def build_transcript_attachment(call_details: dict, transcript: list[dict], ref_id: str) -> tuple[str, bytes]:
    """Build a nicely formatted plain-text transcript file and return (filename, bytes)."""
    cust_clean = re.sub(r"[^a-zA-Z0-9]+", "_", str(call_details.get("customer_name") or "customer").strip()).strip("_")
    phone_digits = re.sub(r"\D", "", str(call_details.get("phone_number") or ""))[-10:]
    phone_part = f"_{phone_digits}" if phone_digits else ""
    filename = f"transcript_{cust_clean}{phone_part}_{ref_id[:8]}.txt"

    header = [
        "=" * 72,
        "TATA TELE BUSINESS SERVICES - SMARTFLOW CALL TRANSCRIPT",
        "=" * 72,
        f"Call Reference ID : {ref_id}",
        f"Customer Name     : {call_details.get('customer_name', 'N/A')}",
        f"Customer Phone    : +91 {call_details.get('phone_number', 'N/A')}",
        f"Amount Due        : INR {call_details.get('amount', 'N/A')}",
        f"Billing Period    : {call_details.get('billing_period', 'N/A')}",
        f"Service Name      : {call_details.get('service_name', 'N/A')}",
        f"Invoice Number    : {call_details.get('invoice_number') or 'N/A'}",
        f"Due Date          : {call_details.get('due_date') or 'N/A'}",
    ]
    if call_details.get("batch_file_name"):
        header.append(f"Upload Batch      : {call_details['batch_file_name']}")
    header.extend([
        f"Call Completed At : {_ended_at_ist(call_details).strftime('%Y-%m-%d %H:%M:%S IST')}",
        "=" * 72,
        "",
        "CONVERSATION TRANSCRIPT:",
        "-" * 72,
    ])

    body = []
    if not transcript:
        body.append("(No dialogue recorded during this call)")
    else:
        for idx, turn in enumerate(transcript, start=1):
            speaker = "Arjun (Tata Tele Bot)" if turn.get("role") == "assistant" else f"{call_details.get('customer_name', 'Customer')}"
            body.append(f"[{idx:02d}] {speaker}:")
            body.append(f"     {turn.get('text', '').strip()}\n")

    footer = [
        "-" * 72,
        "End of Transcript",
        "=" * 72,
    ]

    full_text = "\n".join(header + body + footer)
    return filename, full_text.encode("utf-8")


# ── HTML Email Builder ────────────────────────────────────────────────────────


def format_summary_html(summary_text: str) -> str:
    """Convert summary text into clean, elegant HTML with zero asterisks."""
    # LLM output is text, never markup: escape it before wrapping it in HTML.
    clean_text = escape(clean_summary_text(summary_text), quote=False)
    if not clean_text:
        return ""

    paragraphs = [p.strip() for p in clean_text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in clean_text.split("\n") if p.strip()]

    known_headers = (
        "executive summary",
        "customer commitment & timeline",
        "customer commitment",
        "customer sentiment",
        "recommended next step",
        "recommended next steps",
        "next steps",
    )

    html_blocks = []
    for p in paragraphs:
        lines = [line.strip() for line in p.split("\n") if line.strip()]
        if not lines:
            continue

        first = lines[0]
        first_clean = clean_summary_text(first).rstrip(":")
        first_lower = first_clean.lower()
        is_header = any(h in first_lower for h in known_headers) or first.endswith(":")

        if is_header and len(lines) > 1:
            header_text = first_clean + ":"
            body_lines = [clean_summary_text(l) for l in lines[1:] if clean_summary_text(l)]
            body_text = "<br/>".join(body_lines)
            html_blocks.append(
                f'<div style="margin-bottom: 14px;">'
                f'<div style="font-weight: 700; color: #0f172a; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px;">{header_text}</div>'
                f'<div style="color: #334155; font-size: 14px; line-height: 1.6;">{body_text}</div>'
                f'</div>'
            )
        elif is_header and len(lines) == 1:
            header_text = first_clean + ":"
            html_blocks.append(
                f'<div style="font-weight: 700; color: #0f172a; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 10px; margin-bottom: 4px;">{header_text}</div>'
            )
        else:
            cleaned_lines = [clean_summary_text(l) for l in lines if clean_summary_text(l)]
            joined = "<br/>".join(cleaned_lines)
            html_blocks.append(
                f'<div style="margin-bottom: 10px; color: #334155; font-size: 14px; line-height: 1.6;">{joined}</div>'
            )

    result_html = "\n".join(html_blocks)
    # Absolute guarantee: no asterisk character exists
    result_html = result_html.replace("*", "")
    return result_html


def build_email_html(call_details: dict, summary_info: dict, ref_id: str) -> str:
    summary_text = summary_info.get("summary", "")
    summary_html = format_summary_html(summary_text)

    # Every value comes from uploaded spreadsheets or the call record: escape all of it.
    field = lambda key, default="": escape(str(call_details.get(key) or default))
    phone = field("phone_number")
    cust_name = field("customer_name", "Customer")
    amount = field("amount")
    service = field("service_name")
    billing_period = field("billing_period")
    invoice = field("invoice_number")
    due_date = field("due_date")
    batch_file_name = field("batch_file_name")
    recipient = field("initiator_email")
    ref_id = escape(ref_id)
    ended_at = _ended_at_ist(call_details).strftime("%d %B %Y, %I:%M %p IST")
    call_type = "Pre-due reminder" if call_details.get("call_type") == "predue" else "Overdue collection"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #1e293b; margin: 0; padding: 24px; }}
    .container {{ max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #ffffff; padding: 24px 30px; border-bottom: 3px solid #3b82f6; }}
    .header h1 {{ margin: 0 0 6px 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }}
    .header .subtitle {{ margin: 0; font-size: 13px; color: #94a3b8; }}
    .content {{ padding: 28px 30px; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; background: #dcfce7; color: #15803d; }}
    .summary-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #3b82f6; border-radius: 8px; padding: 18px 20px; margin: 20px 0 24px 0; }}
    .summary-card h3 {{ margin: 0 0 10px 0; font-size: 15px; color: #0f172a; display: flex; align-items: center; gap: 8px; }}
    .summary-text {{ font-size: 14px; line-height: 1.6; color: #334155; margin: 0; }}
    .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 13px; }}
    .meta-table th, .meta-table td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #f1f5f9; }}
    .meta-table th {{ width: 38%; color: #64748b; font-weight: 500; background: #fafafa; }}
    .meta-table td {{ color: #0f172a; font-weight: 600; }}
    .attachment-notice {{ background: #eff6ff; border: 1px dashed #93c5fd; border-radius: 8px; padding: 14px 18px; font-size: 13px; color: #1e40af; display: flex; align-items: center; }}
    .footer {{ background: #f8fafc; padding: 18px 30px; text-align: center; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <div>
          <h1>Tata Tele SmartFlow &bull; Call Summary</h1>
          <p class="subtitle">Automated Voice Bot (Arjun) &bull; Ref: {ref_id[:12]}</p>
        </div>
        <span class="badge">Call Completed</span>
      </div>
    </div>
    
    <div class="content">
      <div class="summary-card">
        <h3>
          <svg style="width:18px;height:18px;vertical-align:middle;margin-right:6px;" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
          Executive Summary &amp; Outcome
        </h3>
        <div class="summary-text">{summary_html}</div>
      </div>

      <h4 style="font-size: 14px; color: #475569; margin: 0 0 10px 0; text-transform: uppercase; letter-spacing: 0.05em;">Customer &amp; Call Details</h4>
      <table class="meta-table">
        <tr><th>Customer Name</th><td>{cust_name}</td></tr>
        <tr><th>Phone Number</th><td>+91 {phone}</td></tr>
        <tr><th>Amount Due</th><td>₹{amount}</td></tr>
        <tr><th>Billing Period</th><td>{billing_period}</td></tr>
        <tr><th>Service</th><td>{service}</td></tr>
        {f"<tr><th>Invoice Number</th><td>{invoice}</td></tr>" if invoice else ""}
        {f"<tr><th>Payment Due Date</th><td>{due_date}</td></tr>" if due_date else ""}
        {f"<tr><th>Upload Batch</th><td>{batch_file_name}</td></tr>" if batch_file_name else ""}
        <tr><th>Call Type</th><td>{call_type}</td></tr>
        <tr><th>Call Ended</th><td>{ended_at}</td></tr>
      </table>

      <div class="attachment-notice">
        <span>&#128206; <strong>Full Transcript Attached:</strong> The complete conversation transcript between Arjun and {cust_name} is attached as a text file for your records.</span>
      </div>
    </div>

    <div class="footer">
      Generated automatically by Tata Tele SmartFlow Callbot for {recipient}
    </div>
  </div>
</body>
</html>"""
    return html


# ── Delivery via Microsoft Graph API ──────────────────────────────────────────


async def send_via_microsoft_graph(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    html_body: str,
    attachment_name: str,
    attachment_bytes: bytes,
) -> None:
    """Send an email with attachment via Microsoft Graph API using application permissions."""
    token = await get_graph_access_token(tenant_id, client_id, client_secret)

    send_url = f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    b64_content = base64.b64encode(attachment_bytes).decode("utf-8")

    message = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html_body,
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": recipient_email,
                    }
                }
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment_name,
                    "contentType": "text/plain",
                    "contentBytes": b64_content,
                }
            ],
        },
        "saveToSentItems": "true",
    }

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(send_url, json=message, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status not in (200, 202):
                text = await resp.text()
                raise RuntimeError(f"Microsoft Graph sendMail failed (HTTP {resp.status}): {text}")


# ── Delivery via SMTP Fallback ────────────────────────────────────────────────


def _send_via_smtp_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    html_body: str,
    attachment_name: str,
    attachment_bytes: bytes,
) -> None:
    msg = MIMEMultipart()
    msg["From"] = sender_email or user
    msg["To"] = recipient_email
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    part = MIMEApplication(attachment_bytes, Name=attachment_name)
    part["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
    msg.attach(part)

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)


async def send_via_smtp(
    host: str,
    port: int,
    user: str,
    password: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    html_body: str,
    attachment_name: str,
    attachment_bytes: bytes,
) -> None:
    await asyncio.to_thread(
        _send_via_smtp_sync,
        host,
        port,
        user,
        password,
        sender_email,
        recipient_email,
        subject,
        html_body,
        attachment_name,
        attachment_bytes,
    )


# ── Main Entrypoint ───────────────────────────────────────────────────────────


async def trigger_call_completed_email(
    ref_id: str,
    transcript: list[dict],
    call_details: dict,
) -> dict:
    """Trigger the automated post-call summary email.

    Called when a call reaches 'completed' status.
    Returns status dict: {'success': bool, 'recipient': str, 'method': str, ...}
    """
    cfg = get_email_config()
    # The summary goes to the dashboard user who started the call (set by the server from the
    # signed-in account). There is deliberately no fallback address.
    recipient = (call_details.get("initiator_email") or "").strip()
    if not recipient:
        return {"success": False, "method": "none", "reason": "This call has no user to email", "ref_id": ref_id}

    cust_name = call_details.get("customer_name") or "Customer"
    phone = call_details.get("phone_number") or ""
    subject = re.sub(r"[\r\n]+", " ", f"Call Summary: {cust_name} ({phone}) — Tata Tele Callbot")  # no header injection

    logger.info(f"[{ref_id}] Generating post-call summary for email notification to {recipient}...")

    # 1. Generate summary and attachment
    summary_info = await generate_transcript_summary(call_details, transcript)
    att_name, att_bytes = build_transcript_attachment(call_details, transcript, ref_id)
    html_body = build_email_html(call_details, summary_info, ref_id)

    # 2. Check credentials and route email
    if is_outlook_configured():
        logger.info(f"[{ref_id}] Sending post-call email via Microsoft Graph (tenant: {cfg['tenant_id'][:8]}...) to {recipient}")
        try:
            await send_via_microsoft_graph(
                tenant_id=cfg["tenant_id"],
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                sender_email=cfg["sender_email"],
                recipient_email=recipient,
                subject=subject,
                html_body=html_body,
                attachment_name=att_name,
                attachment_bytes=att_bytes,
            )
            logger.info(f"[{ref_id}] Post-call email sent successfully to {recipient} via Outlook (Microsoft Graph)")
            return {"success": True, "method": "graph", "recipient": recipient, "ref_id": ref_id}
        except Exception as e:
            logger.exception(f"[{ref_id}] Microsoft Graph email failed: {e}")
            return {"success": False, "method": "graph", "error": str(e), "recipient": recipient, "ref_id": ref_id}

    elif is_smtp_configured():
        logger.info(f"[{ref_id}] Sending post-call email via SMTP ({cfg['smtp_host']}) to {recipient}")
        try:
            await send_via_smtp(
                host=cfg["smtp_host"],
                port=int(cfg["smtp_port"]),
                user=cfg["smtp_user"],
                password=cfg["smtp_password"],
                sender_email=cfg["sender_email"] or cfg["smtp_user"],
                recipient_email=recipient,
                subject=subject,
                html_body=html_body,
                attachment_name=att_name,
                attachment_bytes=att_bytes,
            )
            logger.info(f"[{ref_id}] Post-call email sent successfully to {recipient} via SMTP")
            return {"success": True, "method": "smtp", "recipient": recipient, "ref_id": ref_id}
        except Exception as e:
            logger.exception(f"[{ref_id}] SMTP email failed: {e}")
            return {"success": False, "method": "smtp", "error": str(e), "recipient": recipient, "ref_id": ref_id}

    else:
        logger.warning(
            f"[{ref_id}] Outlook email not configured in .env! "
            f"Please set OUTLOOK_TENANT_ID, OUTLOOK_CLIENT_ID, OUTLOOK_CLIENT_SECRET, and OUTLOOK_SENDER_EMAIL. "
            f"Summary prepared for {recipient} ({len(transcript)} turns), but email sending was skipped."
        )
        return {
            "success": False,
            "method": "none",
            "reason": "Outlook credentials not configured in .env",
            "recipient": recipient,
            "ref_id": ref_id,
        }
