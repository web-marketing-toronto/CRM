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
from datetime import UTC, datetime, timedelta
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


def lead_address(tenant: dict, lead: dict) -> str | None:
    """A per-lead address: the client's intake address with the lead's ref on the end. Emails sent to the lead
    carry it as Reply-To (and the compose links BCC it), so both directions of the conversation land here."""
    base = address_for(tenant)
    if not base:
        return None
    local, domain = base.split("@", 1)
    return f"{local}.{str(lead['ref_code']).lower()}@{domain}"


_ADDR = re.compile(r"^([^+@]+)\+([a-z0-9][a-z0-9-]{1,40})\.([0-9a-f]{12})(?:\.([a-z]\d{3}))?@(.+)$", re.I)


def tenant_for_recipients(conn: psycopg.Connection, recipients: list[str]) -> tuple[dict | None, str | None]:
    """The client a message was addressed to, from any of its recipient addresses. (tenant, address)."""
    tenant, addr, _lead = route_for_recipients(conn, recipients)
    return tenant, addr


def route_for_recipients(conn: psycopg.Connection, recipients: list[str]) -> tuple[dict | None, str | None, dict | None]:
    """(tenant, address, lead): the lead is set when the address carries a lead ref."""
    s = get_settings()
    local, domain = (s.email_from or "@").lower().split("@", 1)
    for addr in recipients:
        m = _ADDR.match(addr.strip().lower())
        if not m or m.group(1) != local or m.group(5) != domain:
            continue
        slug, token, ref = m.group(2), m.group(3), m.group(4)
        if not hmac.compare_digest(token, token_for(slug)):
            continue
        tenant = conn.execute("SELECT * FROM tenant WHERE slug = %s AND is_active", (slug,)).fetchone()
        if tenant is None:
            continue
        lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND ref_code = %s", (tenant["id"], ref.upper())).fetchone() if ref else None
        return tenant, addr.strip().lower(), lead
    return None, None, None


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


_META = re.compile(r"(?is)<meta\s+[^>]*?name\s*=\s*[\"']?(?:lead|form)[_-]([a-z0-9_]+)[\"']?[^>]*?content\s*=\s*[\"']([^\"']*)[\"']")
_META_REV = re.compile(r"(?is)<meta\s+[^>]*?content\s*=\s*[\"']([^\"']*)[\"'][^>]*?name\s*=\s*[\"']?(?:lead|form)[_-]([a-z0-9_]+)[\"']?")


def meta_fields(markup: str) -> dict[str, str]:
    """Some platforms (RealtyNinja) put the lead in <meta name="lead_name" content="…"> tags in the head.
    Those are exact, so they win over anything read from the visible layout."""
    out: dict[str, str] = {}
    for key, value in _META.findall(markup) + [(k, v) for v, k in _META_REV.findall(markup)]:
        key = attribution.normalize_key(key)
        value = html.unescape(value).strip()
        if key and value and key not in out and key not in ("information_version", "version"):
            out[key] = value
    return out


def _cell_text(fragment: str) -> str:
    # Paragraph-level breaks keep a blank line, so a label's value stops at the next paragraph.
    t = re.sub(r"(?i)\s*(?:</(?:p|div|h[1-6])>|<hr[^>]*>)\s*", "\n\n", fragment)
    t = re.sub(r"(?i)\s*(?:<br\s*/?>|</li>)\s*", "\n", t)
    t = re.sub(r"(?is)<a [^>]*href=\"mailto:([^\"]+)\"[^>]*>.*?</a>", r"\1", t)
    t = re.sub(r"(?is)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", lambda m: m.group(2) if m.group(1) in m.group(2) or not m.group(2).strip() else f"{m.group(2)} {m.group(1)}", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    lines = [re.sub(r"[ \t\xa0\r]+", " ", ln).strip() for ln in t.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _table_to_text(inner: str) -> str:
    """One table (no nested tables left in it): each row becomes 'Label: value' when it has two cells,
    otherwise its cells one per line."""
    lines: list[str] = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", inner):
        cells = [c for c in (_cell_text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)) if c]
        if not cells:
            continue
        if len(cells) == 2 and "\n" not in cells[0] and len(cells[0]) <= 60:
            label = cells[0].rstrip(":").strip()
            lines.append(f"{label}: {cells[1]}" if "\n" not in cells[1] else f"{label}:\n{cells[1]}")
        else:
            lines.extend(cells)
    return "\n" + "\n".join(lines) + "\n"


def html_to_text(markup: str) -> str:
    """Enough HTML-to-text for notification emails: nested layout tables are flattened innermost first,
    so a label cell and its value cell come out as one 'Label: value' line."""
    t = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", markup)
    t = re.sub(r"(?is)<div[^>]*display:\s*none[^>]*>.*?</div>", " ", t)  # hidden preheaders
    for _ in range(50):
        m = re.search(r"(?is)<table[^>]*>((?:(?!<table\b).)*?)</table>", t)
        if not m:
            break
        t = t[: m.start()] + _table_to_text(m.group(1)) + t[m.end():]
    return _cell_text(t)


def body_text(msg: EmailMessage) -> tuple[str, dict[str, str]]:
    """(text to parse, exact fields from meta tags if the HTML part carries any)."""
    meta: dict[str, str] = {}
    rich = msg.get_body(preferencelist=("html",))
    if rich is not None:
        markup = rich.get_content()
        meta = meta_fields(markup)
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = plain.get_content()
        if text.strip():
            return text, meta
    if rich is not None:
        return html_to_text(markup), meta
    return "", meta


_QUOTE_HEAD = re.compile(r"^(On .{5,120}wrote:|From: .+|-{2,}\s*Original Message\s*-{2,}|_{5,}|Sent from my .+)$", re.I | re.M)


def reply_text(text: str) -> str:
    """Just what the person typed: everything above the quoted message they replied to."""
    m = _QUOTE_HEAD.search(text)
    head = text[: m.start()] if m else text
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


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
            if len(key) > 40 and not value:
                current = None  # a long label ("The user agreed to the following disclaimer:") ends the previous value
                continue
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
        found = [e for e in _EMAIL.findall(text) if e.lower() not in skip and not e.lower().endswith("@" + own) and not re.match(r"(no-?reply|notifications?|mailer|bounce|support|abuse|help|info)@", e, re.I)]
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
        for key, value in fields.items():
            if re.search(r"page|url|link|listing|property|form", key) and value and _URL.search(value):
                fields["page_url"] = _URL.search(value).group(0)
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
    replies: int = 0
    lead_ids: list[UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {"looked_at": self.looked_at, "leads": self.leads, "duplicates": self.duplicates, "ignored": self.ignored, "rejected": self.rejected, "errors": self.errors, "replies": self.replies}


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
    tenant, to_addr, lead = route_for_recipients(conn, recipients_of(msg))

    def record(status: str, tenant_id: UUID | None = None, lead_id: UUID | None = None, error: str | None = None) -> None:
        conn.execute(
            "INSERT INTO inbound_email (message_id, tenant_id, lead_id, from_addr, to_addr, subject, status, error, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING",
            (key, tenant_id, lead_id, from_addr, to_addr, subject[:300], status, error, received_at),
        )

    if tenant is None:
        record("ignored")
        return "ignored", None
    text, meta = body_text(msg)
    if lead is not None:
        status = lead_mail(conn, tenant, lead, msg, subject=subject, from_addr=from_addr, text=text, now=now)
        record(status, tenant["id"], lead["id"])
        return status, lead["id"]
    parsed = {**parse_fields(subject, text, exclude_emails=tuple(a for a in from_addr.split(", ") if a)), **meta}
    # Platforms usually set Reply-To to the person who filled in the form.
    reply_to = next((a for _, a in getaddresses([str(msg.get("Reply-To") or "")]) if a), "")
    if reply_to and not any(parsed.get(k) for k in attribution.ALIASES["email"]) and not reply_to.lower().endswith("@" + (get_settings().email_from or "@").split("@", 1)[1]):
        parsed["email"] = reply_to
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


def lead_mail(conn: psycopg.Connection, tenant: dict, lead: dict, msg: EmailMessage, *, subject: str, from_addr: str, text: str, now: datetime) -> str:
    """Mail to a lead's own address. Three cases:
      * the lead (or anyone who isn't the client) wrote: a reply, attached to the lead and alerted;
      * the client BCC'd us on an email they sent the lead from their own mail app: kept as the sent email;
      * the client answered our alert from their inbox (only the lead address in To): relayed on to the lead.
    Returns the inbound_email status: reply | sent_copy | relayed | closed_reply."""
    from app.services import followup, messaging
    from app.services import leads as lead_svc

    sender = from_addr.split(", ")[0].lower()
    user = conn.execute("SELECT * FROM app_user WHERE tenant_id = %s AND email_lower = %s AND is_active", (tenant["id"], sender)).fetchone()
    own_domain = (get_settings().email_from or "@").lower().split("@", 1)[1]
    if user is None and sender.endswith("@" + own_domain):
        user = conn.execute("SELECT * FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active ORDER BY created_at LIMIT 1", (tenant["id"],)).fetchone()
    body = reply_text(text)[:BODY_KEEP]
    if user is None:
        # The lead replied.
        followup.on_lead_replied(conn, tenant, lead, body or "(empty message)", now=now, channel="email", subject=subject)
        return "reply"
    # The client wrote. Was the lead already a recipient (a BCC copy), or is this an answer to our alert?
    to_cc = {a.lower() for h in ("To", "Cc") for _, a in getaddresses([str(v) for v in msg.get_all(h, [])]) if a}
    lead_email = (lead.get("email") or "").lower()
    if lead_email and lead_email in to_cc:
        _log_sent_email(conn, tenant, lead, user, subject, body, via="copy", now=now)
        return "sent_copy"
    if not lead_email:
        lead_svc.add_note(conn, lead, f"Email from {user['name']} could not be sent: the lead has no email address.\n\n{body}", actor_id=user["id"], actor_label=user["name"])
        return "sent_copy"
    result = messaging.send_email(
        conn, to=lead_email, subject=subject or f"Re: your request to {tenant['name']}", text=body, html=None, kind="lead_email",
        tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], reply_to=lead_address(tenant, lead), from_name=f"{user['name'].split(' ')[0]} at {tenant['name']}",
    )
    _log_sent_email(conn, tenant, lead, user, subject, body, via="inbox", now=now, delivered=result.ok, error=result.error)
    return "relayed"


def _log_sent_email(conn: psycopg.Connection, tenant: dict, lead: dict, user: dict, subject: str, body: str, *, via: str, now: datetime, delivered: bool = True, error: str | None = None) -> None:
    """An email the client sent the lead. If the Email button logged a bare attempt a few minutes ago, fill
    that in rather than adding a second line."""
    from app.models import ActivityType
    from app.services import leads as lead_svc

    recent = conn.execute(
        "SELECT id FROM activity WHERE lead_id = %s AND type = 'email_sent' AND body IS NULL AND occurred_at > %s ORDER BY occurred_at DESC LIMIT 1",
        (lead["id"], now - timedelta(hours=2)),
    ).fetchone()
    detail = {"kind": "lead_email", "via": via, "subject": subject[:300], "delivered": delivered, "error": error}
    if recent is not None:
        from psycopg.types.json import Jsonb

        conn.execute("UPDATE activity SET body = %s, detail = detail || %s WHERE id = %s", (body, Jsonb(detail), recent["id"]))
        return
    lead_svc.log_touch(conn, lead, ActivityType.EMAIL_SENT, actor_id=user["id"], actor_label=user["name"], note=body, now=now, detail=detail)


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
            elif status in ("reply", "relayed", "sent_copy"):
                res.lead_ids.append(lead_id)
                res.replies += 1
    finally:
        box.close()
    return res
