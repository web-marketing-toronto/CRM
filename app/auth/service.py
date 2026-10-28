"""Login, sessions, MFA enrolment and the request-scoped Principal.

Every function here runs in the privileged 'system' DB context because, by definition, we
do not yet know who the user is. Nothing here touches lead data.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from fastapi import Request
from psycopg.types.json import Jsonb

from app.auth import crypto
from app.config import get_settings
from app.db import system_db
from app.models import Role

LOCKOUT_AFTER = 8
LOCKOUT_MINUTES = 15
MFA_REQUIRED_ROLES = {Role.AGENCY_ADMIN, Role.OWNER}


@dataclass
class Principal:
    user: dict
    session: dict
    tenant: dict | None  # the tenant whose data this request sees (own tenant, or acting-as for agency)

    @property
    def user_id(self) -> UUID:
        return self.user["id"]

    @property
    def role(self) -> Role:
        return Role(self.user["role"])

    @property
    def is_agency(self) -> bool:
        return self.role == Role.AGENCY_ADMIN

    @property
    def tenant_id(self) -> UUID | None:
        return self.tenant["id"] if self.tenant else None

    @property
    def db_role(self) -> str:
        return self.role.value

    @property
    def label(self) -> str:
        return self.user["name"]

    @property
    def mfa_required(self) -> bool:
        return self.role in MFA_REQUIRED_ROLES

    @property
    def mfa_ok(self) -> bool:
        if not self.user["totp_enabled"]:
            return not self.mfa_required
        return bool(self.session["mfa_passed"])

    @property
    def needs_mfa_enrolment(self) -> bool:
        return self.mfa_required and not self.user["totp_enabled"]

    @property
    def needs_mfa_challenge(self) -> bool:
        return bool(self.user["totp_enabled"]) and not self.session["mfa_passed"]


class AuthError(Exception):
    pass


def _ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    raw = (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else None)
    if not raw:
        return None
    try:
        ipaddress.ip_address(raw)
        return raw
    except ValueError:
        return None


def audit(conn: psycopg.Connection, *, action: str, user_id: UUID | None, tenant_id: UUID | None, target_type: str | None = None, target_id: str | None = None, detail: dict | None = None, ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit (tenant_id, user_id, action, target_type, target_id, detail, ip) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, user_id, action, target_type, target_id, Jsonb(detail or {}), ip),
    )


# --- Users --------------------------------------------------------------------------------

def create_user(
    conn: psycopg.Connection,
    *,
    email: str,
    name: str,
    role: Role,
    tenant_id: UUID | None,
    password: str,
    mobile_e164: str | None = None,
    must_set_password: bool = True,
) -> dict:
    if (role == Role.AGENCY_ADMIN) != (tenant_id is None):
        raise AuthError("agency admins have no tenant; client users must have one")
    problem = crypto.password_problem(password)
    if problem:
        raise AuthError(problem)
    row = conn.execute(
        """
        INSERT INTO app_user (tenant_id, email, name, role, password_hash, mobile_e164, must_set_password)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (tenant_id, email.strip(), name.strip(), role.value, crypto.hash_password(password), mobile_e164, must_set_password),
    ).fetchone()
    return row


def set_password(conn: psycopg.Connection, user_id: UUID, password: str) -> None:
    problem = crypto.password_problem(password)
    if problem:
        raise AuthError(problem)
    conn.execute(
        "UPDATE app_user SET password_hash = %s, must_set_password = false WHERE id = %s",
        (crypto.hash_password(password), user_id),
    )


# --- Password reset by email ------------------------------------------------------------
RESET_TOKEN_SECONDS = 60 * 60


def request_password_reset(conn: psycopg.Connection, email: str, *, ip: str | None = None) -> bool:
    """Email a one-hour reset link if the address has an active login. Always returns quietly so the
    form can't be used to find out which addresses exist. At most 3 links per address per hour."""
    from app.config import get_settings
    from app.services import messaging

    user = conn.execute("SELECT * FROM app_user WHERE email_lower = lower(%s) AND is_active", (email.strip(),)).fetchone()
    if user is None:
        audit(conn, action="password_reset_requested_unknown", user_id=None, tenant_id=None, detail={"email": email.strip()[:200]}, ip=ip)
        return False
    recent = conn.execute(
        "SELECT count(*) AS n FROM audit WHERE action = 'password_reset_requested' AND user_id = %s AND created_at > now() - interval '1 hour'", (user["id"],)
    ).fetchone()["n"]
    if recent >= 3:
        return False
    token = crypto.signed_token({"u": str(user["id"]), "h": crypto.hash_backup_code(user["password_hash"])[:16]}, salt="pwreset")
    link = f"{get_settings().base_url}/login/reset/{token}"
    agency = get_settings().agency_name
    text = (
        f"Hi {user['name'].split(' ')[0]},\n\nSomeone (probably you) asked to reset the password for your {agency} CRM login. "
        f"Open this link within the hour to choose a new one:\n\n{link}\n\nIf you didn't ask, ignore this; your password is unchanged."
    )
    html = (
        f"<p>Hi {user['name'].split(' ')[0]},</p><p>Someone (probably you) asked to reset the password for your {agency} CRM login. "
        f"The link works for one hour.</p><p><a href='{link}' style='display:inline-block;padding:11px 16px;background:#ffd400;color:#16202b;font-weight:700;border-radius:6px;text-decoration:none'>Choose a new password</a></p>"
        f"<p style='color:#4f5a66;font-size:13px'>If you didn't ask, ignore this; your password is unchanged.<br>{link}</p>"
    )
    messaging.send_email(conn, to=user["email"], subject=f"Reset your {agency} CRM password", text=text, html=html, kind="password_reset", tenant_id=user["tenant_id"], user_id=user["id"])
    audit(conn, action="password_reset_requested", user_id=user["id"], tenant_id=user["tenant_id"], ip=ip)
    return True


def user_for_reset_token(conn: psycopg.Connection, token: str) -> dict | None:
    """The user a reset link belongs to, or None if the link is expired, forged, or already used
    (the token is bound to the password hash, so it dies the moment the password changes)."""
    data = crypto.read_signed_token(token, salt="pwreset", max_age_seconds=RESET_TOKEN_SECONDS)
    if not data:
        return None
    user = conn.execute("SELECT * FROM app_user WHERE id = %s AND is_active", (UUID(data["u"]),)).fetchone()
    if user is None or crypto.hash_backup_code(user["password_hash"])[:16] != data.get("h"):
        return None
    return user


def complete_password_reset(conn: psycopg.Connection, user: dict, password: str, *, ip: str | None = None) -> None:
    set_password(conn, user["id"], password)
    conn.execute("UPDATE app_user SET failed_logins = 0, locked_until = NULL WHERE id = %s", (user["id"],))
    conn.execute("DELETE FROM session WHERE user_id = %s", (user["id"],))
    audit(conn, action="password_reset", user_id=user["id"], tenant_id=user["tenant_id"], ip=ip)


# --- Login ------------------------------------------------------------------------------

def login(email: str, password: str, request: Request | None = None) -> tuple[dict, str]:
    """Verify credentials, open a session, return (user, cookie_value). Raises AuthError.

    Failures are recorded in their own committed transaction before the error is raised,
    otherwise the rollback would erase the failed-attempt counter.
    """
    settings = get_settings()
    ip = _ip(request)
    ua = request.headers.get("user-agent", "")[:300] if request else None
    now = datetime.now(UTC)
    generic = "That email and password don't match."
    failure: str | None = None
    with system_db() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE email_lower = lower(%s)", (email.strip(),)).fetchone()
        if user is None:
            # Burn roughly the same time as a real verify so timing doesn't reveal valid emails.
            crypto.verify_password(crypto.hash_password("x" * 12), password)
            failure = generic
        elif not user["is_active"]:
            failure = generic
        elif user["locked_until"] and user["locked_until"] > now:
            failure = "Too many failed attempts. Try again in a few minutes."
        elif not crypto.verify_password(user["password_hash"], password):
            failed = int(user["failed_logins"]) + 1
            locked = now + timedelta(minutes=LOCKOUT_MINUTES) if failed >= LOCKOUT_AFTER else None
            conn.execute("UPDATE app_user SET failed_logins = %s, locked_until = %s WHERE id = %s", (failed, locked, user["id"]))
            audit(conn, action="login_failed", user_id=user["id"], tenant_id=user["tenant_id"], ip=ip)
            failure = generic
    if failure:
        raise AuthError(failure)

    with system_db() as conn:
        if crypto.password_needs_rehash(user["password_hash"]):
            conn.execute("UPDATE app_user SET password_hash = %s WHERE id = %s", (crypto.hash_password(password), user["id"]))
        conn.execute(
            "UPDATE app_user SET failed_logins = 0, locked_until = NULL, last_login_at = %s WHERE id = %s", (now, user["id"])
        )
        session = conn.execute(
            """
            INSERT INTO session (user_id, tenant_id, mfa_passed, ip, user_agent, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (user["id"], user["tenant_id"], not user["totp_enabled"] and user["role"] == Role.MEMBER.value, ip, ua, now + timedelta(hours=settings.session_ttl_hours)),
        ).fetchone()
        audit(conn, action="login", user_id=user["id"], tenant_id=user["tenant_id"], ip=ip)
    return user, crypto.sign_session_id(str(session["id"]))


def login_from_link(user_id: UUID, request: Request | None = None) -> tuple[dict, str] | None:
    """A signed link from an alert text opens a session on that phone without a password: the text
    reached the mobile number on the account, which is the same factor a reset code would use. The
    authenticator (if the user has one) is still asked for, as after a normal sign-in."""
    settings = get_settings()
    ip, ua = _ip(request), (request.headers.get("user-agent", "")[:300] if request else None)
    now = datetime.now(UTC)
    with system_db() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE id = %s AND is_active", (user_id,)).fetchone()
        if user is None or (user["locked_until"] and user["locked_until"] > now):
            return None
        conn.execute("UPDATE app_user SET last_login_at = %s WHERE id = %s", (now, user["id"]))
        session = conn.execute(
            "INSERT INTO session (user_id, tenant_id, mfa_passed, ip, user_agent, expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (user["id"], user["tenant_id"], not user["totp_enabled"] and user["role"] == Role.MEMBER.value, ip, ua, now + timedelta(hours=settings.session_ttl_hours)),
        ).fetchone()
        audit(conn, action="login_link", user_id=user["id"], tenant_id=user["tenant_id"], ip=ip)
    return user, crypto.sign_session_id(str(session["id"]))


def logout(session_id: UUID) -> None:
    with system_db() as conn:
        conn.execute("UPDATE session SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (session_id,))


def load_principal(cookie_value: str | None) -> Principal | None:
    if not cookie_value:
        return None
    settings = get_settings()
    session_id = crypto.unsign_session_id(cookie_value, settings.session_ttl_hours * 3600)
    if not session_id:
        return None
    with system_db() as conn:
        session = conn.execute(
            "SELECT * FROM session WHERE id = %s AND revoked_at IS NULL AND expires_at > now()", (session_id,)
        ).fetchone()
        if session is None:
            return None
        user = conn.execute("SELECT * FROM app_user WHERE id = %s AND is_active", (session["user_id"],)).fetchone()
        if user is None:
            return None
        tenant = None
        if session["tenant_id"]:
            tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (session["tenant_id"],)).fetchone()
        if session["last_seen_at"] < datetime.now(UTC) - timedelta(minutes=5):
            conn.execute("UPDATE session SET last_seen_at = now() WHERE id = %s", (session["id"],))
    return Principal(user=user, session=session, tenant=tenant)


def set_acting_tenant(session_id: UUID, tenant_id: UUID | None, *, user_id: UUID, ip: str | None) -> None:
    """Agency admins switch which client's data they are looking at."""
    with system_db() as conn:
        conn.execute("UPDATE session SET tenant_id = %s WHERE id = %s", (tenant_id, session_id))
        audit(conn, action="act_as_tenant", user_id=user_id, tenant_id=tenant_id, ip=ip)


# --- MFA ---------------------------------------------------------------------------------

def start_totp_enrolment(user_id: UUID) -> tuple[str, str]:
    """Create (but don't enable) a TOTP secret. Returns (secret, otpauth URI)."""
    settings = get_settings()
    with system_db() as conn:
        user = conn.execute("SELECT email FROM app_user WHERE id = %s", (user_id,)).fetchone()
        secret = crypto.new_totp_secret()
        conn.execute(
            "UPDATE app_user SET totp_secret_enc = %s, totp_enabled = false, totp_last_counter = NULL WHERE id = %s",
            (crypto.encrypt_secret(secret), user_id),
        )
    return secret, crypto.totp_uri(secret, user["email"], f"{settings.agency_name} CRM")


def confirm_totp_enrolment(user_id: UUID, session_id: UUID, code: str) -> list[str]:
    """Verify the first code from the authenticator, enable MFA, return fresh backup codes (shown once)."""
    with system_db() as conn:
        user = conn.execute("SELECT totp_secret_enc FROM app_user WHERE id = %s", (user_id,)).fetchone()
        secret = crypto.decrypt_secret(user["totp_secret_enc"] or "")
        if not secret:
            raise AuthError("Start the setup again.")
        counter = crypto.verify_totp(secret, code, None)
        if counter is None:
            raise AuthError("That code didn't match. Codes change every 30 seconds; try the current one.")
        codes = crypto.new_backup_codes()
        conn.execute(
            "UPDATE app_user SET totp_enabled = true, totp_last_counter = %s, backup_codes = %s WHERE id = %s",
            (counter, Jsonb([crypto.hash_backup_code(c) for c in codes]), user_id),
        )
        conn.execute("UPDATE session SET mfa_passed = true WHERE id = %s", (session_id,))
        audit(conn, action="mfa_enabled", user_id=user_id, tenant_id=None)
    return codes


def verify_mfa(user_id: UUID, session_id: UUID, code: str) -> None:
    code = code.strip()
    with system_db() as conn:
        user = conn.execute(
            "SELECT totp_secret_enc, totp_last_counter, backup_codes, tenant_id FROM app_user WHERE id = %s", (user_id,)
        ).fetchone()
        secret = crypto.decrypt_secret(user["totp_secret_enc"] or "")
        if secret:
            counter = crypto.verify_totp(secret, code, user["totp_last_counter"])
            if counter is not None:
                conn.execute("UPDATE app_user SET totp_last_counter = %s WHERE id = %s", (counter, user_id))
                conn.execute("UPDATE session SET mfa_passed = true WHERE id = %s", (session_id,))
                return
        remaining = crypto.consume_backup_code(list(user["backup_codes"] or []), code)
        if remaining is not None:
            conn.execute("UPDATE app_user SET backup_codes = %s WHERE id = %s", (Jsonb(remaining), user_id))
            conn.execute("UPDATE session SET mfa_passed = true WHERE id = %s", (session_id,))
            audit(conn, action="mfa_backup_code_used", user_id=user_id, tenant_id=user["tenant_id"])
            return
    with system_db() as conn:
        audit(conn, action="mfa_failed", user_id=user_id, tenant_id=user["tenant_id"])
    raise AuthError("That code didn't match.")


def reset_totp(conn: psycopg.Connection, user_id: UUID, *, by_user_id: UUID) -> None:
    """Agency admin clears a user's authenticator so they can enrol a new phone."""
    conn.execute(
        "UPDATE app_user SET totp_enabled = false, totp_secret_enc = NULL, totp_last_counter = NULL, backup_codes = '[]' WHERE id = %s",
        (user_id,),
    )
    conn.execute("UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    audit(conn, action="mfa_reset", user_id=by_user_id, tenant_id=None, target_type="user", target_id=str(user_id))
