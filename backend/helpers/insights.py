"""Call transcript insights and follow-up callback scheduler.

Extracts key customer quotes, payment commitments (e.g., 'pay in 2 days'),
and schedules business-day follow-ups strictly avoiding non-working days (Saturday & Sunday).
"""

import re
from datetime import date, datetime, timedelta
from typing import Optional


# Working days rule: Monday (0) to Friday (4) are working days.
# Saturday (5) and Sunday (6) are non-working days.
def next_working_day(target_date: date) -> date:
    """Ensures a date lands on a working day (Mon-Fri).

    If target falls on Saturday (+1) or Sunday (+2), moves to Monday.
    """
    weekday = target_date.weekday()
    if weekday == 5:  # Saturday
        return target_date + timedelta(days=2)
    elif weekday == 6:  # Sunday
        return target_date + timedelta(days=1)
    return target_date


def add_working_days(start_date: date, days: int) -> date:
    """Adds business days, skipping weekends completely."""
    current = start_date
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Monday to Friday
            added += 1
    return current


# Number word mappings for English and Hindi / Hinglish
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "panch": 6,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5,
}

_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0, "somwar": 0, "सोमवार": 0,
    "tuesday": 1, "tue": 1, "mangalwar": 1, "मंगलवार": 1,
    "wednesday": 2, "wed": 2, "budhwar": 2, "बुधवार": 2,
    "thursday": 3, "thu": 3, "guruwar": 3, "veervar": 3, "गुरुवार": 3,
    "friday": 4, "fri": 4, "shukrawar": 4, "शुक्रवार": 4,
}


def parse_commitment_eta(text: str, call_date: Optional[date] = None) -> tuple[Optional[str], Optional[date], str]:
    """Analyzes customer utterance and extracts commitment ETA description, calculated callback date, and type.

    Returns:
    (eta_label, callback_date, category)
    category is either 'callback', 'payment', or 'commitment'.
    Strictly enforces that callback_date will never fall on Saturday or Sunday.
    """
    if not text:
        return None, None, ""

    base_date = call_date or date.today()
    clean = text.lower().strip()

    is_callback = any(w in clean for w in ("call back", "callback", "call me", "call later", "call after", "will call", "baad me call", "baad mein call"))
    is_payment = any(w in clean for w in ("pay", "payment", "clear", "settle", "transfer", "paise", "bhej dunga", "bhej dungi"))

    def _format_label(days: int) -> str:
        s = "s" if days > 1 else ""
        if is_callback:
            return f"Callback in {days} day{s}"
        elif is_payment:
            return f"Payment promised in {days} day{s}"
        return f"Promised within {days} day{s}"

    # Pattern 0: Range expressions like 'in 2 to 3 days', '2-3 days', '3-4 din' -> Worst case: take UPPER bound
    m_range = re.search(r"(?:in|within)?\s*(\d+)\s*(?:to|-)\s*(\d+)\s*(?:days?|din|दिन)", clean)
    if m_range:
        upper_days = int(m_range.group(2))
        if 1 <= upper_days <= 60:
            raw_target = base_date + timedelta(days=upper_days)
            final_target = next_working_day(raw_target)
            cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
            return _format_label(upper_days), final_target, cat

    # Pattern 1: 'within a week', 'in a week', '1 week', 'ek hafta', '2 weeks' -> Worst case: give customer most time (full week: 7 days, 14 days)
    if re.search(r"\b(within (?:a |one )?week|in (?:a |one )?week|after (?:a )?week|one week|1 week|ek hafta|1 hafta|एक हफ्ता)\b", clean):
        # Worst case: full 7 days given to customer
        raw_target = base_date + timedelta(days=7)
        final_target = next_working_day(raw_target)
        label = "Callback within a week" if is_callback else ("Payment promised within a week" if is_payment else "Promised within a week")
        cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
        return label, final_target, cat

    if re.search(r"\b(within (?:two |2 )?weeks|in (?:two |2 )?weeks|two weeks|2 weeks|do hafte|2 hafte|दो हफ्ते)\b", clean):
        raw_target = base_date + timedelta(days=14)
        final_target = next_working_day(raw_target)
        label = "Callback within 2 weeks" if is_callback else ("Payment promised within 2 weeks" if is_payment else "Promised within 2 weeks")
        cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
        return label, final_target, cat

    # Pattern 2: 'in X days' / 'within X days' / 'X days' / 'X din' / 'X din ke baad'
    day_patterns = [
        r"(?:in|within|after)?\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten|ek|do|teen|char|paanch|एक|दो|तीन|चार|पांच)\s*(?:days?|din|dino|दिन)(?:\s*(?:ke\s*baad|mein|me|time))?",
        r"(\d+)\s*(?:days?|din|दिन)",
    ]
    for pat in day_patterns:
        m = re.search(pat, clean)
        if m:
            raw_num = m.group(1)
            days = _NUMBER_WORDS.get(raw_num) or (int(raw_num) if raw_num.isdigit() else None)
            if days is not None and 1 <= days <= 60:
                raw_target = base_date + timedelta(days=days)
                final_target = next_working_day(raw_target)
                cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
                return _format_label(days), final_target, cat

    # Pattern 3: 'tomorrow' / 'kal' / 'kal tak'
    if re.search(r"\b(tomorrow|kal|कल)\b", clean):
        raw_target = base_date + timedelta(days=1)
        final_target = next_working_day(raw_target)
        label = "Callback tomorrow" if is_callback else ("Payment tomorrow" if is_payment else "Promised tomorrow")
        cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
        return label, final_target, cat

    # Pattern 4: 'day after tomorrow' / 'parson' / 'परसों'
    if re.search(r"\b(day after tomorrow|parson|parso|परसों)\b", clean):
        raw_target = base_date + timedelta(days=2)
        final_target = next_working_day(raw_target)
        label = "Callback day after tomorrow" if is_callback else ("Payment day after tomorrow" if is_payment else "Promised day after tomorrow")
        cat = "callback" if is_callback else ("payment" if is_payment else "commitment")
        return label, final_target, cat

    # Pattern 5: Named day of week (e.g. 'by Monday', 'Friday ko', 'next Monday')
    for name, target_wd in _WEEKDAY_NAMES.items():
        if re.search(rf"\b{name}\b", clean):
            current_wd = base_date.weekday()
            days_ahead = (target_wd - current_wd) % 7
            if days_ahead == 0:
                days_ahead = 7  # next week's occurrence
            target_date = base_date + timedelta(days=days_ahead)
            final_target = next_working_day(target_date)
            cap_name = name.capitalize()
            label = f"Callback by {cap_name}" if is_callback else f"Promised by {cap_name}"
            cat = "callback" if is_callback else "commitment"
            return label, final_target, cat

    # Pattern 6: 'next week' / 'agle hafte' / 'अगले हफ्ते' -> Worst case: Friday of next week (maximum time given)
    if re.search(r"\b(next week|agle hafte|अगले हफ्ते)\b", clean):
        # Calculate next week's Friday (weekday 4) to give customer maximum time
        days_to_next_friday = (4 - base_date.weekday()) % 7 + 7
        target_date = base_date + timedelta(days=days_to_next_friday)
        final_target = next_working_day(target_date)
        label = "Callback next week" if is_callback else "Promised next week"
        cat = "callback" if is_callback else "commitment"
        return label, final_target, cat

    # Pattern 7: 'end of the month' / 'month end'
    if re.search(r"\b(month end|end of (?:this )?month|mahine ke aakhri)\b", clean):
        next_month = date(base_date.year + (1 if base_date.month == 12 else 0), 1 if base_date.month == 12 else base_date.month + 1, 1)
        last_day = next_month - timedelta(days=1)
        final_target = next_working_day(last_day)
        label = "Callback month end" if is_callback else "Promised month end"
        cat = "callback" if is_callback else "commitment"
        return label, final_target, cat

    return None, None, ""


def extract_call_insights(call_details: dict, transcript: list[dict]) -> dict:
    """Analyzes a completed call transcript and returns structured enterprise insights.

    Extracts:
    - customer_quote: Key quote from customer (e.g., 'I will pay in 2 days')
    - commitment_eta: Clean label for promised payment (e.g., 'Promised within 2 days')
    - callback_date: Strict business day date (ISO YYYY-MM-DD), never weekend
    - sentiment: Committed | Callback Requested | Disputed | Paid | Escalated | Unreachable
    - summary: 1-2 sentence executive summary
    """
    if not transcript:
        return {
            "customer_quote": "",
            "commitment_eta": "",
            "callback_date": None,
            "sentiment": "Unreachable",
            "summary": "No dialogue turns were recorded for this call.",
        }

    user_turns = [t.get("text", "").strip() for t in transcript if t.get("role") == "user" and t.get("text")]
    call_created = call_details.get("created_at") or call_details.get("dialed_at")
    base_date = date.today()
    if call_created:
        try:
            if isinstance(call_created, (datetime, date)):
                base_date = call_created.date() if isinstance(call_created, datetime) else call_created
            else:
                base_date = datetime.fromisoformat(str(call_created).replace("Z", "+00:00")).date()
        except Exception:
            pass

    commitment_eta: Optional[str] = None
    callback_date: Optional[date] = None
    customer_quote = ""
    sentiment = "Completed"

    # Scan user turns in reverse to prioritize their latest commitment
    for turn in reversed(user_turns):
        eta, cb_date, cat = parse_commitment_eta(turn, base_date)
        if eta:
            commitment_eta = eta
            callback_date = cb_date
            customer_quote = turn
            sentiment = "Callback Requested" if cat == "callback" else "Committed"
            break

    # If no explicit ETA found, check other sentiments and quotes
    if not commitment_eta and user_turns:
        # Find the most meaningful user turn (skip simple 'English', 'Hello', 'Yes')
        substantive_turns = [t for t in user_turns if len(t.split()) > 2 and t.lower() not in ("english is fine", "i would like to continue in english", "hindi mein baat karo")]
        chosen_turn = substantive_turns[-1] if substantive_turns else user_turns[-1]
        customer_quote = chosen_turn
        clean_quote = chosen_turn.lower()

        if any(w in clean_quote for w in ("already paid", "paid already", "already pay", "paise de diye", "pay kar diya")):
            sentiment = "Paid"
            commitment_eta = "Customer states already paid"
        elif any(w in clean_quote for w in ("dispute", "wrong amount", "galat bill", "issue", "service issue", "not working")):
            sentiment = "Disputed"
            commitment_eta = "Dispute raised by customer"
        elif any(w in clean_quote for w in ("busy", "driving", "meeting", "hospital", "call later", "baad me call", "call back")):
            sentiment = "Callback Requested"
            commitment_eta = "Callback requested"
            # Schedule callback for next business day
            callback_date = next_working_day(base_date + timedelta(days=1))
        elif any(w in clean_quote for w in ("manager", "approval", "finance", "authority")):
            sentiment = "Pending Approval"
            commitment_eta = "Under internal approval"
            # Follow-up in 2 business days
            callback_date = next_working_day(base_date + timedelta(days=2))
        elif any(w in clean_quote for w in ("wrong number", "galat number", "disconnect")):
            sentiment = "Wrong Number"
            commitment_eta = "Wrong contact number"

    # Check for human callback requests
    full_user_text = " ".join(user_turns).lower()
    is_human_requested = any(w in full_user_text for w in (
        "talk to human", "human call", "real person", "connect to agent", "speak with agent",
        "agent se baat", "manager se baat", "executive call", "representative", "aadmi se baat",
        "kisi se baat", "speak to someone", "customer care person", "human agent", "talk to a person"
    ))
    callback_type = "human" if is_human_requested else "auto"

    # Incident / Ticket detection rules:
    # 1. Service issue / disruption
    # 2. Human callback requested
    # 3. Call back later / meeting / busy
    # 4. Billing dispute / invoice question
    ticket_needed = False
    ticket_category = ""
    ticket_priority = "medium"
    ticket_title = ""
    ticket_description = ""

    customer_name = call_details.get("customer_name") or "Customer"
    amount = call_details.get("amount") or ""
    service = call_details.get("service_name") or "Telecom Services"
    amt_str = f"INR {amount}" if amount else "the outstanding amount"

    has_service_issue = any(w in full_user_text for w in (
        "service not working", "internet not working", "not working", "slow speed", "broadband",
        "down", "network problem", "line cut", "connection issue", "technical issue", "complaint",
        "fault", "net nahi chal raha", "kaam nahi kar raha", "link down"
    ))
    has_dispute = any(w in full_user_text for w in (
        "dispute", "wrong amount", "galat bill", "already paid", "paise de diye", "fraud", "extra charges", "wrong bill"
    ))
    has_callback_req = is_human_requested or any(w in full_user_text for w in (
        "call back later", "call me later", "call later", "baad me call", "driving", "in a meeting", "busy right now"
    )) or (sentiment == "Callback Requested")

    if has_service_issue:
        ticket_needed = True
        ticket_category = "service_issue"
        ticket_priority = "high"
        ticket_title = f"Service Outage / Complaint: {service} ({customer_name})"
        ticket_description = f"Customer reported service interruption: '{customer_quote}'. Priority investigation required for {service} account."
    elif is_human_requested:
        ticket_needed = True
        ticket_category = "human_callback"
        ticket_priority = "high"
        ticket_title = f"Human Agent Callback Requested: {customer_name}"
        ticket_description = f"Customer explicitly requested to speak with a human agent / executive. Quote: '{customer_quote}'. Assigned for personal callback."
    elif has_dispute:
        ticket_needed = True
        ticket_category = "billing_dispute"
        ticket_priority = "medium"
        ticket_title = f"Billing Dispute / Concern: {customer_name}"
        ticket_description = f"Customer raised invoice discrepancy or claimed prior payment ({amt_str}). Quote: '{customer_quote}'."
    elif has_callback_req:
        ticket_needed = True
        ticket_category = "callback_request"
        ticket_priority = "medium"
        eta_desc = commitment_eta or (f"by {callback_date}" if callback_date else "soon")
        ticket_title = f"Follow-up Callback: {customer_name} ({eta_desc})"
        ticket_description = f"Customer requested a callback ({callback_type}). Scheduled for {callback_date or 'next working day'}. Quote: '{customer_quote}'."

    # Executive summary
    if sentiment == "Committed" and commitment_eta:
        cb_str = callback_date.strftime("%a, %d %b %Y") if callback_date else "upcoming date"
        summary = f"{customer_name} acknowledged the {amt_str} bill for {service}. {commitment_eta}. Next follow-up scheduled for {cb_str}."
    elif sentiment == "Paid":
        summary = f"{customer_name} stated that payment for {service} has already been completed. Account verification recommended."
    elif sentiment == "Disputed":
        summary = f"{customer_name} raised a concern regarding the invoice or service terms. Action required from accounts team."
    elif sentiment == "Callback Requested":
        cb_str = callback_date.strftime("%a, %d %b %Y") if callback_date else "next business day"
        cb_mode = "Human callback" if callback_type == "human" else "Automated callback"
        summary = f"{customer_name} requested a callback ({cb_mode}). Follow-up set for {cb_str}."
    else:
        summary = f"Call completed with {len(transcript)} turns for {customer_name} ({amt_str}). Review transcript for full dialogue."

    return {
        "customer_quote": customer_quote[:300],
        "commitment_eta": commitment_eta or "",
        "callback_date": callback_date.isoformat() if callback_date else None,
        "callback_type": callback_type,
        "sentiment": sentiment,
        "summary": summary,
        "ticket_needed": ticket_needed,
        "ticket_category": ticket_category,
        "ticket_priority": ticket_priority,
        "ticket_title": ticket_title,
        "ticket_description": ticket_description,
    }
