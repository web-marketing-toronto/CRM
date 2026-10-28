"""Password hashing, TOTP secrets at rest, backup codes, cookie signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from uuid import UUID

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if password.lower() in {"password12", "password123", "1234567890", "qwertyuiop"}:
        return "That password is too common."
    return None


# --- TOTP secrets are encrypted with a key derived from SECRET_KEY -----------------------------

def _fernet() -> Fernet:
    raw = hashlib.sha256(("totp:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def totp_uri(secret: str, email: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str, last_counter: int | None) -> int | None:
    """Return the time-step counter the code matched, or None. Rejects a counter already used (replay)."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    import time

    now = int(time.time())
    for offset in (0, -1, 1):
        t = now + offset * totp.interval
        counter = t // totp.interval
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp.at(t), code):
            return counter
    return None


# --- Backup codes: 8 one-time codes, stored hashed -------------------------------------------

def new_backup_codes(n: int = 8) -> list[str]:
    return [f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}" for _ in range(n)]


def hash_backup_code(code: str) -> str:
    norm = code.replace("-", "").replace(" ", "")
    return hashlib.sha256(("backup:" + get_settings().secret_key + ":" + norm).encode()).hexdigest()


def consume_backup_code(stored: list[str], code: str) -> list[str] | None:
    """Return the remaining list if the code matched one of the stored hashes, else None."""
    h = hash_backup_code(code)
    for i, s in enumerate(stored):
        if hmac.compare_digest(s, h):
            return stored[:i] + stored[i + 1 :]
    return None


# --- Cookie signing -------------------------------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="crm-session")


def sign_session_id(session_id: str) -> str:
    return _serializer().dumps(session_id)


def unsign_session_id(value: str, max_age_seconds: int) -> str | None:
    try:
        return _serializer().loads(value, max_age=max_age_seconds)
    except BadSignature:
        return None


def signed_token(payload: dict, salt: str) -> str:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt).dumps(payload)


def read_signed_token(token: str, salt: str, max_age_seconds: int) -> dict | None:
    try:
        return URLSafeTimedSerializer(get_settings().secret_key, salt=salt).loads(token, max_age=max_age_seconds)
    except BadSignature:
        return None


# --- Compact links for texts ---------------------------------------------------------------------
# 16-byte lead id + 16-byte user id + 4-byte expiry, base64url (48 chars), then a 12-byte HMAC tag
# (16 chars): ~65 characters, short enough to ride in an SMS alongside the message.

def short_token(lead_id: UUID | str, user_id: UUID | str, *, salt: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    exp = int(time.time()) + ttl_seconds
    body = UUID(str(lead_id)).bytes + UUID(str(user_id)).bytes + struct.pack(">I", exp)
    tag = hmac.new(get_settings().secret_key.encode(), salt.encode() + b"\0" + body, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(body + tag).decode().rstrip("=")


def read_short_token(token: str, *, salt: str) -> tuple[UUID, UUID] | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != 48:
        return None
    body, tag = raw[:36], raw[36:]
    want = hmac.new(get_settings().secret_key.encode(), salt.encode() + b"\0" + body, hashlib.sha256).digest()[:12]
    if not hmac.compare_digest(tag, want):
        return None
    exp = struct.unpack(">I", body[32:36])[0]
    if exp < time.time():
        return None
    return UUID(bytes=body[:16]), UUID(bytes=body[16:32])
