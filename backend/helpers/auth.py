"""Dashboard sign-in: password hashing, session tokens and login throttling."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

SESSION_COOKIE = "tt_session"
SESSION_DAYS = 7
MIN_PASSWORD_LENGTH = 8
ROLES = ("admin", "user")

# scrypt cost: ~50 ms per hash, 16 MB of memory.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw) -> str:
    return str(raw or "").strip().lower()


def email_error(email: str) -> str | None:
    if not email:
        return "Email is required"
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return "Enter a valid email address"
    return None


def password_error(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 200:
        return "Password is too long"
    return None


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = await asyncio.to_thread(_scrypt, password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


async def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = await asyncio.to_thread(_scrypt, password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


# A real hash to check against when the email is unknown, so response time doesn't reveal which emails exist.
_DUMMY_HASH = f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(b'0' * 16)}${_b64(b'0' * 32)}"


async def verify_unknown_user(password: str) -> bool:
    await verify_password(password, _DUMMY_HASH)
    return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Sessions are stored by hash, so a leaked database can't be used to sign in."""
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottle:
    """At most `limit` failed sign-ins per email (and per client IP) in `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 15 * 60):
        self.limit, self.window = limit, window
        self._failures: dict[str, deque] = defaultdict(deque)

    def _recent(self, key: str) -> deque:
        q = self._failures[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        waits = []
        for k in keys:
            q = self._recent(k)
            if not q:
                self._failures.pop(k, None)  # nothing recent: forget the key
            elif len(q) >= self.limit:
                waits.append(int(q[0] + self.window - time.monotonic()) + 1)
        return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        if len(self._failures) > 10_000:  # drop keys whose failures have all expired
            for k in [k for k in self._failures if not self._recent(k)]:
                del self._failures[k]
        for k in keys:
            self._recent(k).append(time.monotonic())

    def succeeded(self, *keys: str) -> None:
        for k in keys:
            self._failures.pop(k, None)
