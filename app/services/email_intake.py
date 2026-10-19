"""Leads by email, for sites that can only send a notification (RealtyNinja, Wix, Squarespace, Houzz…).

Each client gets an address in the CRM mailbox using plus-addressing, e.g.
    crm+braun.3f9a1c2b7d4e@web-marketing-toronto.com
The site BCCs its lead notification there. The minute tick reads the mailbox over IMAP with the same
app password the SMTP sender uses, turns each message into a lead through the normal intake path
(field aliases, the client's field map, dedupe, follow-ups) and records every message it looked at so
nothing is processed twice. Messages to the bare mailbox address are left alone.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import html
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from uuid import UUID

import psycopg

from app.config import get_settings
from app.db import system_db
from app.models import Channel
from app.services import attribution
from app.services.leads import LeadError, create_from_intake

log = logging.getLogger("crm.email_intake")

MAX_PER_POLL = 50
BODY_KEEP = 4000


# --- Addresses ------------------------------------------------------------------------------------

def enabled() -> bool:
    s = get_settings()
    return bool(s.imap_host and s.smtp_password and s.email_from and "@" in s.email_from)


def token_for(slug: str) -> str:
    """Stable per-client token in the address; not the intake key, so the address can be given to a web
    platform without exposing the webhook."""
    return hmac.new(get_settings().secret_key.encode(), f"email-intake:{slug}".encode(), hashlib.sha256).hexdigest()[:12]


def address_for(tenant: dict) -> str | None:
    s = get_settings()
    if not s.email_from or "@" not in s.email_from:
        return None
    local, domain = s.email_from.split("@", 1)
    return f"{local}+{tenant['slug']}.{token_for(tenant['slug'])}@{domain}"


_ADDR = re.compile(r"^([^+@]+)\+([a-z0-9][a-z0-9-]{1,40})\.([0-9a-f]{12})@(.+)$", re.I)


def tenant_for_recipients(conn: psycopg.Connection, recipients: list[str]) -> tuple[dict | None, str | None]:
    """The client a message was addressed to, from any of its recipient addresses. (tenant, address)."""
    s = get_settings()
    local, domain = (s.email_from or "@").lower().split("@", 1)
    for addr in recipients:
        m = _ADDR.match(addr.strip().lower())
        if not m or m.group(1) != local or m.group(4) != domain:
            continue
        slug, token = m.group(2), m.group(3)
        if not hmac.compare_digest(token, token_for(slug)):
            continue
        tenant = conn.execute("SELECT * FROM tenant WHERE slug = %s AND is_active", (slug,)).fetchone()
        if tenant is not None:
            return tenant, addr.strip().lower()
    return None, None


def recipients_of(msg: EmailMessage) -> list[str]:
    out: list[str] = []
    for header in ("Delivered-To", "X-Original-To", "Envelope-To", "X-Envelope-To", "X-Forwarded-To", "To", "Cc", "Bcc", "Resent-To"):
        for value in msg.get_all(header, []):
            out.extend(a for _, a in getaddresses([str(value)]) if a)
    return out


# --- Parsing the notification into lead fields --------------------------------------------------

_KEY_LINE = re.compile(r"^\s*[*\-•]?\s*([A-Za-z][A-Za-z0-9 /_().'’-]{0,40}?)\s*[:：]\s*(.*?)\s*$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_URL = re.compile(r"https?://[^\s<>\"]+")
_LONG_KEYS = ("message", "comments", "comment", "notes", "details", "description", "question", "inquiry", "enquiry", "how_can_we_help", "tell_us_more", "additional_info")
# Lines that are boilerplate rather than lead data.
_NOISE = re.compile(r"(unsubscribe|view (this|in) browser|powered by|sent (from|via|by)|this email was|do not reply|copyright|all rights reserved)", re.I)


def html_to_text(markup: str) -> str:
    """Enough HTML-to-text for notification emails: table cells become 'Label: value' lines."""
    t = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", markup)
    t = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", ": ", t)          # label cell | value cell
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", t)
    t = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", lambda m: f"{m.group(2)} {m.group(1)}" if m.group(2).strip() and m.group(1) not in m.group(2) else m.group(1), t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = plain.get_content()
        if text.strip():
            return text
    rich = msg.get_body(preferencelist=("html",))
    if rich is not None:
        return html_to_text(rich.get_content())
    return ""


def parse_fields(subject: str, text: str, *, exclude_emails: tuple[str, ...] = ()) -> dict[str, str]:
    """'Label: value' lines (and a label on one line with its value below) into a flat dict the normal
    intake aliases understand, with email/phone/name fallbacks found anywhere in the message."""
    fields: dict[str, str] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            current = None
            continue
        if _NOISE.search(line):
            continue
        m = _KEY_LINE.match(line)
        if m and not _URL.match(line.strip()):
            key = attribution.normalize_key(m.group(1))
            value = m.group(2).strip()
            if key and len(key) <= 40:
                if key in fields and value:
                    # Repeated label (e.g. two "Phone:" lines): keep the first, unless it was blank.
                    if not fields[key]:
                        fields[key] = value
                    current = key
                    continue
                fields[key] = value
                current = key
                continue
        if current and (current in _LONG_KEYS or not fields.get(current)):
            fields[current] = (fields[current] + "\n" + line.strip()).strip() if fields[current] else line.strip()
    # Fallbacks for notifications that don't label things.
    if not any(fields.get(k) for k in attribution.ALIASES["email"]):
        own = (get_settings().email_from or "@").lower().split("@", 1)[1]
        skip = {e.lower() for e in exclude_emails}
        found = [e for e in _EMAIL.findall(text) if e.lower() not in skip and not e.lower().endswith("@" + own) and not re.match(r"(no-?reply|notifications?|mailer|bounce)", e, re.I)]
        if found:
            fields["email"] = found[0]
    if not any(fields.get(k) for k in attribution.ALIASES["phone"]):
        m = _PHONE.search(text)
        if m:
            fields["phone"] = m.group(0)
    if not any(fields.get(k) for k in attribution.ALIASES["contact_name"] + attribution.ALIASES["first_name"]):
        m = re.search(r"(?:lead|inquiry|enquiry|message|request|contact)\s+from\s+(.+?)(?:\s+(?:on|via|for|about|at)\b|\s*[-–|]|$)", subject, re.I)
        if m and "@" not in m.group(1) and not re.search(r"\b(your|the|our|a|new)\b|website|web site|site|form|page|listing", m.group(1), re.I):
            fields["name"] = m.group(1).strip()[:200]
    if not any(fields.get(k) for k in attribution.ATTRIBUTION_ALIASES["landing_page"]):
        for key in ("listing", "listing_url", "property_url", "page", "link", "url"):
            if fields.get(key) and _URL.search(fields[key]):
                fields["page_url"] = _URL.search(fields[key]).group(0)
                break
    fields.setdefault("form_name", subject.strip()[:120])
    return fields


# --- The mailbox ----------------------------------------------------------------------------------

class Mailbox:
    """Thin IMAP wrapper (Gmail / Workspace). Replaced by a fake in tests."""

    def __init__(self) -> None:
        s = get_settings()
        self.conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
        self.conn.login(s.imap_user or s.smtp_user or s.email_from, s.smtp_password)
        self.conn.select("INBOX")

    def unseen(self, limit: int = MAX_PER_POLL) -> list[bytes]:
        _, data = self.conn.uid("SEARCH", None, "UNSEEN")
        uids = (data[0] or b"").split()
        return uids[:limit]

    def fetch(self, uid: bytes) -> bytes:
        _, data = self.conn.uid("FETCH", uid, "(RFC822)")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return b""

    def mark_seen(self, uid: bytes) -> None:
        self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass


def open_mailbox() -> Mailbox:
    return Mailbox()


@dataclass
class PollResult:
    looked_at: int = 0
    leads: int = 0
    duplicates: int = 0
    ignored: int = 0
    rejected: int = 0
    errors: int = 0
    lead_ids: list[UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {"looked_at": self.looked_at, "leads": self.leads, "duplicates": self.duplicates, "ignored": self.ignored, "rejected": self.rejected, "errors": self.errors}


def message_key(msg: EmailMessage, raw: bytes) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    return mid[:500] if mid else "sha256:" + hashlib.sha256(raw).hexdigest()


def process_message(conn: psycopg.Connection, raw: bytes, *, now: datetime | None = None) -> tuple[str, UUID | None]:
    """One message → (status, lead_id). Status: lead | duplicate | ignored | rejected | seen."""
    now = now or datetime.now(UTC)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    key = message_key(msg, raw)
    if conn.execute("SELECT 1 FROM inbound_email WHERE message_id = %s", (key,)).fetchone():
        return "seen", None
    subject = str(msg.get("Subject") or "").strip()
    from_addr = ", ".join(a for _, a in getaddresses([str(msg.get("From") or "")]) if a)[:200]
    try:
        received_at = parsedate_to_datetime(msg["Date"]) if msg.get("Date") else None
    except (TypeError, ValueError):
        received_at = None
    tenant, to_addr = tenant_for_recipients(conn, recipients_of(msg))

    def record(status: str, tenant_id: UUID | None = None, lead_id: UUID | None = None, error: str | None = None) -> None:
        conn.execute(
            "INSERT INTO inbound_email (message_id, tenant_id, lead_id, from_addr, to_addr, subject, status, error, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING",
            (key, tenant_id, lead_id, from_addr, to_addr, subject[:300], status, error, received_at),
        )

    if tenant is None:
        record("ignored")
        return "ignored", None
    text = body_text(msg)
    parsed = parse_fields(subject, text, exclude_emails=tuple(a for a in from_addr.split(", ") if a))
    payload: dict[str, Any] = {**parsed, "_email": {"subject": subject, "from": from_addr, "to": to_addr, "received_at": received_at.isoformat() if received_at else None, "body": text[:BODY_KEEP]}}
    fields = attribution.interpret(parsed, tenant.get("field_map") or {}, default_channel=Channel.WEBSITE_FORM)
    try:
        result = create_from_intake(conn, tenant, fields, raw_payload=payload, ip=None, user_agent="email intake", maps_api_key=get_settings().google_maps_api_key, now=now)
    except LeadError as exc:
        record("rejected", tenant["id"], error=str(exc)[:500])
        log.info("email intake for %s rejected: %s (subject %r)", tenant["slug"], exc, subject[:80])
        return "rejected", None
    status = "lead" if result.created else "duplicate"
    record(status, tenant["id"], result.lead_id)
    log.info("email intake %s ref=%s created=%s from %s", tenant["slug"], result.ref_code, result.created, from_addr)
    return status, result.lead_id


def poll(*, now: datetime | None = None, mailbox: Mailbox | None = None) -> PollResult:
    """Read unseen mail, make leads, mark everything seen. Each message is its own transaction so one bad
    email never blocks the rest."""
    res = PollResult()
    if not enabled() and mailbox is None:
        return res
    box = mailbox or open_mailbox()
    try:
        for uid in box.unseen():
            raw = box.fetch(uid)
            res.looked_at += 1
            try:
                with system_db() as conn:
                    status, lead_id = process_message(conn, raw, now=now)
            except Exception:  # noqa: BLE001 - log it, mark it seen, move on
                log.exception("email intake: message uid %r failed", uid)
                res.errors += 1
                box.mark_seen(uid)
                continue
            box.mark_seen(uid)
            if status == "lead":
                res.leads += 1
                res.lead_ids.append(lead_id)
            elif status == "duplicate":
                res.duplicates += 1
                res.lead_ids.append(lead_id)
            elif status == "rejected":
                res.rejected += 1
            elif status == "ignored":
                res.ignored += 1
    finally:
        box.close()
    return res
