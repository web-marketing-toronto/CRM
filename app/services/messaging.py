"""Sending SMS and email, and logging every message either way.

One entry point each: `send_sms(...)` and `send_email(...)`. Both write a message_log row first,
hand the message to the configured provider, and update the row with the provider's id or error.
Providers:

    sms:   twilio | fake (records only, for tests) | log (prints, for local dev)
    email: smtp (a Google Workspace mailbox, say) | postmark | sendgrid | fake | log

The fake provider keeps an in-memory outbox so tests can assert on what would have gone out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import psycopg

from app.config import get_settings

log = logging.getLogger("crm.messaging")

SMS_MAX = 1500  # Twilio splits long messages; keep ours short anyway


@dataclass
class SendResult:
    ok: bool
    provider: str
    provider_id: str | None = None
    error: str | None = None


class Outbox:
    """In-memory record of sends made by the fake provider (tests)."""

    def __init__(self) -> None:
        self.sms: list[dict[str, Any]] = []
        self.email: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.sms.clear()
        self.email.clear()
        self.calls.clear()


outbox = Outbox()


# --- Providers -----------------------------------------------------------------------------

def _twilio_send(to: str, from_: str, body: str) -> SendResult:
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return SendResult(False, "twilio", error="Twilio credentials are not configured")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    data = {"To": to, "From": from_, "Body": body[:SMS_MAX]}
    if s.base_url.startswith("https://"):
        data["StatusCallback"] = f"{s.base_url}/v1/twilio/status"
    try:
        r = httpx.post(url, data=data, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=15)
    except httpx.HTTPError as exc:
        return SendResult(False, "twilio", error=str(exc)[:300])
    if r.status_code >= 300:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        return SendResult(False, "twilio", error=f"{r.status_code}: {msg}"[:300])
    return SendResult(True, "twilio", provider_id=r.json().get("sid"))


def start_call(*, to: str, from_: str, connect_url: str) -> SendResult:
    """Ring `to` from the client's number; when they answer, Twilio fetches `connect_url` for what to do next
    (dial the lead). Used for click-to-call so the lead sees the business number, not a personal mobile."""
    s = get_settings()
    if s.sms_provider == "fake":
        outbox.calls.append({"to": to, "from": from_, "url": connect_url})
        return SendResult(True, "fake", provider_id=f"fake-call-{len(outbox.calls)}")
    if s.sms_provider != "twilio":
        log.info("CALL to %s from %s then %s", to, from_, connect_url)
        return SendResult(True, "log", provider_id="log-call")
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return SendResult(False, "twilio", error="Twilio credentials are not configured")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Calls.json"
    try:
        r = httpx.post(url, data={"To": to, "From": from_, "Url": connect_url, "Method": "POST", "Timeout": "25"}, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=15)
    except httpx.HTTPError as exc:
        return SendResult(False, "twilio", error=str(exc)[:300])
    if r.status_code >= 300:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        return SendResult(False, "twilio", error=f"{r.status_code}: {msg}"[:300])
    return SendResult(True, "twilio", provider_id=r.json().get("sid"))


def _postmark_send(to: str, subject: str, text: str, html: str | None, reply_to: str | None, from_name: str | None = None) -> SendResult:
    s = get_settings()
    if not s.postmark_token:
        return SendResult(False, "postmark", error="Postmark token is not configured")
    payload: dict[str, Any] = {
        "From": f"{from_name or s.email_from_name} <{s.email_from}>", "To": to, "Subject": subject, "TextBody": text,
        "MessageStream": "outbound",
    }
    if html:
        payload["HtmlBody"] = html
    if reply_to:
        payload["ReplyTo"] = reply_to
    try:
        r = httpx.post("https://api.postmarkapp.com/email", json=payload, headers={"X-Postmark-Server-Token": s.postmark_token, "Accept": "application/json"}, timeout=15)
    except httpx.HTTPError as exc:
        return SendResult(False, "postmark", error=str(exc)[:300])
    if r.status_code >= 300:
        try:
            msg = r.json().get("Message", r.text)
        except ValueError:
            msg = r.text
        return SendResult(False, "postmark", error=f"{r.status_code}: {msg}"[:300])
    return SendResult(True, "postmark", provider_id=r.json().get("MessageID"))


def _sendgrid_send(to: str, subject: str, text: str, html: str | None, reply_to: str | None, from_name: str | None = None) -> SendResult:
    s = get_settings()
    if not s.sendgrid_api_key:
        return SendResult(False, "sendgrid", error="SendGrid key is not configured")
    content = [{"type": "text/plain", "value": text}]
    if html:
        content.append({"type": "text/html", "value": html})
    payload: dict[str, Any] = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": s.email_from, "name": from_name or s.email_from_name},
        "subject": subject,
        "content": content,
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    try:
        r = httpx.post("https://api.sendgrid.com/v3/mail/send", json=payload, headers={"Authorization": f"Bearer {s.sendgrid_api_key}"}, timeout=15)
    except httpx.HTTPError as exc:
        return SendResult(False, "sendgrid", error=str(exc)[:300])
    if r.status_code >= 300:
        return SendResult(False, "sendgrid", error=f"{r.status_code}: {r.text[:200]}")
    return SendResult(True, "sendgrid", provider_id=r.headers.get("X-Message-Id"))


def _smtp_send(to: str, subject: str, text: str, html: str | None, reply_to: str | None, from_name: str | None = None) -> SendResult:
    """Plain SMTP with STARTTLS. With Gmail / Google Workspace the From address must be the mailbox itself or
    one of its 'Send mail as' aliases, otherwise Google rewrites it; the display name and Reply-To are kept."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr, make_msgid

    s = get_settings()
    user = s.smtp_user or s.email_from
    if not s.smtp_password:
        return SendResult(False, "smtp", error="SMTP password is not configured")
    msg = EmailMessage()
    msg["From"] = formataddr((from_name or s.email_from_name, s.email_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=s.email_from.split("@")[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.login(user, s.smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return SendResult(False, "smtp", error=str(exc)[:300])
    return SendResult(True, "smtp", provider_id=msg["Message-ID"])


# --- Logging + dispatch ---------------------------------------------------------------------

def _log(conn: psycopg.Connection, **cols: Any) -> int:
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    row = conn.execute(f"INSERT INTO message_log ({keys}) VALUES ({marks}) RETURNING id", list(cols.values())).fetchone()
    return int(row["id"])


def _finish(conn: psycopg.Connection, log_id: int, result: SendResult) -> None:
    conn.execute(
        "UPDATE message_log SET status = %s, provider_id = %s, error = %s, updated_at = now() WHERE id = %s",
        ("sent" if result.ok else "failed", result.provider_id, result.error, log_id),
    )


def send_sms(
    conn: psycopg.Connection,
    *,
    to: str,
    body: str,
    kind: str,
    tenant_id: UUID | None,
    lead_id: UUID | None = None,
    user_id: UUID | None = None,
    reminder_id: UUID | None = None,
    from_: str | None = None,
) -> SendResult:
    """Send one text. `from_` defaults to the agency number. Never raises; the result says what happened."""
    s = get_settings()
    sender = from_ or s.twilio_agency_from or "+10000000000"
    body = body.strip()
    if tenant_id is not None and conn.execute(
        "SELECT 1 FROM sms_opt_out WHERE tenant_id = %s AND phone_e164 = %s", (tenant_id, to)
    ).fetchone():
        result = SendResult(False, s.sms_provider, error="recipient opted out (STOP)")
        _log(conn, tenant_id=tenant_id, lead_id=lead_id, user_id=user_id, reminder_id=reminder_id, direction="out", channel="sms", kind=kind, to_addr=to, from_addr=sender, body=body, provider=s.sms_provider, status="failed", error=result.error)
        return result
    log_id = _log(conn, tenant_id=tenant_id, lead_id=lead_id, user_id=user_id, reminder_id=reminder_id, direction="out", channel="sms", kind=kind, to_addr=to, from_addr=sender, body=body, provider=s.sms_provider)
    if s.sms_provider == "twilio":
        result = _twilio_send(to, sender, body)
    elif s.sms_provider == "fake":
        outbox.sms.append({"to": to, "from": sender, "body": body, "kind": kind, "lead_id": lead_id, "user_id": user_id, "tenant_id": tenant_id})
        result = SendResult(True, "fake", provider_id=f"fake-sms-{log_id}")
    else:
        log.info("SMS to %s from %s [%s]: %s", to, sender, kind, body)
        result = SendResult(True, "log", provider_id=f"log-sms-{log_id}")
    _finish(conn, log_id, result)
    if not result.ok:
        log.warning("sms to %s failed: %s", to, result.error)
    return result


def send_email(
    conn: psycopg.Connection,
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None,
    kind: str,
    tenant_id: UUID | None,
    lead_id: UUID | None = None,
    user_id: UUID | None = None,
    reminder_id: UUID | None = None,
    reply_to: str | None = None,
    from_name: str | None = None,
) -> SendResult:
    s = get_settings()
    log_id = _log(conn, tenant_id=tenant_id, lead_id=lead_id, user_id=user_id, reminder_id=reminder_id, direction="out", channel="email", kind=kind, to_addr=to, from_addr=s.email_from, subject=subject, body=text, provider=s.email_provider)
    if s.email_provider == "smtp":
        result = _smtp_send(to, subject, text, html, reply_to, from_name)
    elif s.email_provider == "postmark":
        result = _postmark_send(to, subject, text, html, reply_to, from_name)
    elif s.email_provider == "sendgrid":
        result = _sendgrid_send(to, subject, text, html, reply_to, from_name)
    elif s.email_provider == "fake":
        outbox.email.append({"to": to, "subject": subject, "text": text, "html": html, "kind": kind, "lead_id": lead_id, "user_id": user_id, "tenant_id": tenant_id, "reply_to": reply_to, "from_name": from_name})
        result = SendResult(True, "fake", provider_id=f"fake-email-{log_id}")
    else:
        log.info("EMAIL to %s [%s] %s: %s", to, kind, subject, text[:200])
        result = SendResult(True, "log", provider_id=f"log-email-{log_id}")
    _finish(conn, log_id, result)
    if not result.ok:
        log.warning("email to %s failed: %s", to, result.error)
    return result


def record_inbound_sms(conn: psycopg.Connection, *, tenant_id: UUID | None, lead_id: UUID | None, user_id: UUID | None, from_: str, to: str, body: str, provider_id: str | None) -> int:
    return _log(conn, tenant_id=tenant_id, lead_id=lead_id, user_id=user_id, direction="in", channel="sms", kind="inbound", to_addr=to, from_addr=from_, body=body, provider="twilio", provider_id=provider_id, status="received")


def update_delivery(conn: psycopg.Connection, provider_id: str, status: str, error: str | None = None) -> None:
    mapped = {"delivered": "delivered", "sent": "sent", "failed": "failed", "undelivered": "failed"}.get(status)
    if mapped is None:
        return
    conn.execute("UPDATE message_log SET status = %s, error = COALESCE(%s, error), updated_at = now() WHERE provider_id = %s", (mapped, error, provider_id))


_DIGITS = re.compile(r"\D+")


def digits(value: str) -> str:
    return _DIGITS.sub("", value or "")
