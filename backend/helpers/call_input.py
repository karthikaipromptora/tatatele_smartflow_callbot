import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from helpers.prompts import DEFAULT_CONTEXT
from helpers.voices import DEFAULT_VOICE, VOICE_IDS

_MONTH_LIST = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
_MONTH_NAMES = dict(enumerate(_MONTH_LIST, 1))
_MONTHS = {m.lower(): i for i, m in _MONTH_NAMES.items()} | {m[:3].lower(): i for i, m in _MONTH_NAMES.items()}
_MONTHS["sept"] = 9


def normalize_phone(raw) -> str | None:
    """Return the 10-digit Indian number (mobile or landline without the 0), or None."""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _indian_grouping(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def normalize_amount(raw) -> str | None:
    """'25000', 25000.0, '₹ 25,000', 'Rs. 1,25,000.50' → '25,000' / '1,25,000.50'. None if not a positive amount."""
    text = re.sub(r"(?i)rs\.?|inr|₹|,|\s", "", str(raw if raw is not None else ""))
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    value = value.quantize(Decimal("0.01"))
    whole, frac = divmod(value, 1)
    out = _indian_grouping(int(whole))
    return f"{out}.{int(frac * 100):02d}" if frac else out


def normalize_period(raw) -> str | None:
    """Excel dates, '2026-03', '03/2026', 'Mar 2026', 'march-2026' → 'March 2026'. Other non-empty text is kept as-is."""
    if isinstance(raw, (datetime, date)):
        return f"{_MONTH_NAMES[raw.month]} {raw.year}"
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?", text) or None
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{_MONTH_NAMES[int(m.group(2))]} {m.group(1)}"
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{_MONTH_NAMES[int(m.group(1))]} {m.group(2)}"
    m = re.fullmatch(r"([A-Za-z]+)[\s\-/,']*(\d{2}|\d{4})", text)
    if m and m.group(1).lower() in _MONTHS:
        year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
        return f"{_MONTH_NAMES[_MONTHS[m.group(1).lower()]]} {year}"
    return text[:40]


def parse_date(raw) -> date | None:
    """Excel dates, '2026-09-20', '20/09/2026', '20-Sep-2026', '20 September 2026' → date."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def spoken_date(d: date) -> str:
    return f"{d.day} {_MONTH_NAMES[d.month]} {d.year}"


def email_domain(raw) -> str:
    first = re.split(r"[;,\s]+", str(raw or "").strip())[0]
    return first.split("@", 1)[1].lower()[:80] if "@" in first else ""


def validate_call(raw: dict, default_voice: str = DEFAULT_VOICE, today: date | None = None) -> tuple[dict | None, list[str]]:
    """Validate one call request. Returns ({'phone_number', 'ctx'}, []) or (None, [errors])."""
    errors = []
    phone = normalize_phone(raw.get("phone_number"))
    if not phone:
        errors.append("Phone number must have 10 digits")

    name = str(raw.get("customer_name") or "").strip()
    if not name:
        errors.append("Customer name is missing")
    elif len(name) > 80:
        errors.append("Customer name is longer than 80 characters")

    amount = normalize_amount(raw.get("amount"))
    if not amount:
        errors.append("Amount must be a number greater than 0")

    period = normalize_period(raw.get("billing_period"))
    if not period:
        errors.append("Billing period is missing")

    voice = str(raw.get("voice_id") or "").strip().lower() or default_voice
    if voice not in VOICE_IDS:
        errors.append(f"Unknown voice '{voice}'")

    call_type = "predue" if str(raw.get("call_type") or "").strip().lower() in ("predue", "pre-due", "reminder") else "overdue"

    due_raw = raw.get("due_date")
    due = parse_date(due_raw)
    if due_raw not in (None, "") and not due:
        errors.append("Due date is not a valid date")
    days_overdue = ""
    if due:
        days_overdue = str(((today or date.today()) - due).days)

    amount_paid = normalize_amount(raw.get("amount_paid")) or ""
    service = str(raw.get("service_name") or "").strip()[:120] or DEFAULT_CONTEXT["service_name"]
    clean = lambda key, n: re.sub(r"\.0$", "", str(raw.get(key) or "").strip())[:n]
    initiator_email = str(raw.get("initiator_email") or raw.get("user_email") or os.getenv("DEFAULT_NOTIFICATION_EMAIL", "tejaabhishek@gmail.com")).strip().lower()

    if errors:
        return None, errors
    return {
        "phone_number": phone,
        "ctx": {
            "customer_name": name,
            "service_name": service,
            "amount": amount,
            "billing_period": period,
            "language": "English",  # the bot always opens in English and offers Hindi
            "voice_id": voice,
            "call_type": call_type,
            "account_number": clean("account_number", 30),
            "invoice_number": clean("invoice_number", 30),
            "due_date": spoken_date(due) if due else "",
            "days_overdue": days_overdue,
            "amount_paid": amount_paid,
            "email_domain": str(raw.get("email_domain") or "").strip().lower()[:80] or email_domain(raw.get("email")),
            "initiator_email": initiator_email,
        },
        "due_iso": due.isoformat() if due else "",
    }, []
