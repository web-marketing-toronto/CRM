"""CSV import of a client's existing lead list.

Three steps, each a function here:
  parse()      read the file, sniff the delimiter, return headers + rows (as dicts)
  guess_map()  propose which column feeds which standard field
  run()        dry run or commit: dedupe inside the file and against existing leads, create,
               merge, reject; set statuses from the client's own status words.

Imported leads are marked `flags.needs_review` and `flags.imported`; the reminder engine and
the needs-action view skip them until the client has looked at each one.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb

from app.models import ActivityType, Channel, LeadStatus, LostReason, Platform
from app.services.attribution import ALIASES, IntakeFields, normalize_key
from app.services.leads import DEDUPE_WINDOW, find_duplicate, log_activity, next_ref_code
from app.services.normalize import clean_text, normalize_email, normalize_phone

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024

# Standard targets a column can map to, in the order the mapping page lists them.
TARGETS: list[tuple[str, str]] = [
    ("skip", "Skip this column"),
    ("contact_name", "Name (full)"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("company", "Company"),
    ("contact_address", "Contact address"),
    ("job_address", "Job address"),
    ("city", "City / town"),
    ("province", "Province"),
    ("postal_code", "Postal code"),
    ("job_type", "Work type"),
    ("message", "Notes / message"),
    ("status", "Status (their words)"),
    ("deal_value", "Deal value"),
    ("source", "Lead source"),
    ("created_at", "Date received"),
    ("notes2", "Extra notes (appended)"),
]
TARGET_KEYS = {k for k, _ in TARGETS}

EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "status": ("status", "stage", "lead_status", "pipeline", "state", "outcome", "result"),
    "deal_value": ("deal_value", "value", "amount", "job_value", "price", "quote", "quote_amount", "sale", "revenue", "total"),
    "source": ("source", "lead_source", "channel", "platform", "how_heard", "referral_source", "origin", "campaign"),
    "created_at": ("created_at", "date", "created", "received", "lead_date", "date_received", "timestamp", "submitted", "added"),
    "notes2": ("notes", "note", "comments", "comment", "details", "remarks"),
}


@dataclass
class Parsed:
    headers: list[str]
    rows: list[dict[str, str]]
    text: str
    truncated: bool = False


def parse(data: bytes, filename: str = "leads.csv") -> Parsed:
    if len(data) > MAX_BYTES:
        raise ValueError("That file is over 5 MB. Split it and import in parts.")
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Couldn't read that file as text. Export it from Excel or Sheets as CSV (UTF-8).")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows_raw = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows_raw:
        raise ValueError("The file is empty.")
    headers = [h.strip() or f"column_{i + 1}" for i, h in enumerate(rows_raw[0])]
    seen: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h in seen:
            seen[h] += 1
            headers[i] = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
    body = rows_raw[1:]
    truncated = len(body) > MAX_ROWS
    rows = []
    for r in body[:MAX_ROWS]:
        r = list(r) + [""] * (len(headers) - len(r))
        rows.append({h: (r[i] or "").strip() for i, h in enumerate(headers)})
    return Parsed(headers=headers, rows=rows, text=text, truncated=truncated)


def guess_map(headers: list[str]) -> dict[str, str]:
    """{header: target}; headers nothing matches are 'skip'."""
    lookup: dict[str, str] = {}
    for target, names in list(ALIASES.items()) + list(EXTRA_ALIASES.items()):
        if target in ("form_name", "channel", "submitted_at"):
            continue
        for n in names:
            lookup.setdefault(n, target)
    out: dict[str, str] = {}
    used: set[str] = set()
    for h in headers:
        key = normalize_key(h)
        target = lookup.get(key)
        if target is None:
            for n, t in lookup.items():
                if n and (key.startswith(n + "_") or key.endswith("_" + n) or key == n):
                    target = t
                    break
        if target is None and ("phone" in key or "cell" in key or "mobile" in key):
            target = "phone"
        if target is None and "mail" in key:
            target = "email"
        if target is None and "name" in key and "company" not in key and "business" not in key:
            target = "contact_name"
        if target is None and ("address" in key or "street" in key):
            target = "job_address"
        if target in used and target not in ("notes2",):
            target = None
        if target:
            used.add(target)
        out[h] = target or "skip"
    return out


STATUS_GUESSES: list[tuple[tuple[str, ...], str]] = [
    (("won", "closed", "sold", "booked", "signed", "customer", "complete", "done", "scheduled", "accepted"), "won"),
    (("lost", "dead", "declined", "no sale", "not interested", "spam", "junk", "cancel", "rejected", "went elsewhere", "no response"), "lost"),
    (("quote", "estimate", "proposal", "bid"), "quoted"),
    (("contact", "called", "spoke", "reached", "in progress", "follow", "working", "pending", "active"), "contacted"),
    (("new", "open", "unassigned", "lead", "uncontacted", "fresh"), "new"),
]


def guess_status(word: str) -> str:
    w = word.strip().lower()
    if not w:
        return "review"
    for needles, status in STATUS_GUESSES:
        if any(n in w for n in needles):
            return status
    return "review"


def distinct_status_words(rows: list[dict[str, str]], mapping: dict[str, str]) -> list[str]:
    col = next((h for h, t in mapping.items() if t == "status"), None)
    if not col:
        return []
    seen: dict[str, None] = {}
    for r in rows:
        seen.setdefault(r.get(col, "").strip(), None)
    return [w for w in seen if w != ""] + ([""] if "" in seen else [])


def platform_from_text(text: str | None) -> Platform:
    t = (text or "").lower()
    if not t:
        return Platform.UNKNOWN
    if "google" in t or "adwords" in t or "ppc" in t or "search ad" in t:
        return Platform.GOOGLE_ADS
    if "facebook" in t or "meta" in t or "instagram" in t or "fb" == t.strip():
        return Platform.META_ADS
    if "referr" in t or "word of mouth" in t or "friend" in t or "repeat" in t:
        return Platform.REFERRAL
    if "organic" in t or "seo" in t or "web" in t or "site" in t:
        return Platform.ORGANIC
    if "direct" in t or "walk" in t or "sign" in t or "truck" in t:
        return Platform.DIRECT
    return Platform.UNKNOWN


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
    "%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%m-%d-%Y",
)


def parse_date(text: str | None, tz_name: str = "UTC") -> datetime | None:
    """Dates in a client's spreadsheet are local to the client; a bare date is taken as noon that day
    so it never slips to the previous day when shown in their time zone."""
    if not text:
        return None
    t = text.strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            if "%H" not in fmt and "%I" not in fmt:
                dt = dt.replace(hour=12)
            dt = dt.replace(tzinfo=tz)
        return dt
    return None


def parse_money(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        v = Decimal(cleaned)
    except InvalidOperation:
        return None
    return v if v >= 0 else None


@dataclass
class RowOutcome:
    row: int
    action: str  # create | merge | reject
    name: str
    reason: str = ""
    status: str = ""


@dataclass
class RunResult:
    created: int = 0
    merged: int = 0
    rejected: int = 0
    outcomes: list[RowOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created, "merged": self.merged, "rejected": self.rejected,
            "rejects": [{"row": o.row, "name": o.name, "reason": o.reason} for o in self.outcomes if o.action == "reject"][:200],
        }


def _fields_from_row(row: dict[str, str], mapping: dict[str, str]) -> tuple[IntakeFields, dict[str, str]]:
    """Build an IntakeFields from a CSV row plus the extra (status, value, source, date, notes2) raw values."""
    raw: dict[str, str] = {}
    extras: dict[str, str] = {}
    for header, target in mapping.items():
        value = (row.get(header) or "").strip()
        if not value or target == "skip":
            continue
        if target in ("status", "deal_value", "source", "created_at"):
            extras[target] = value
        elif target == "notes2":
            extras["notes2"] = (extras.get("notes2", "") + "\n" + f"{header}: {value}").strip()
        else:
            raw.setdefault(target, value)
    # Compose names and addresses the same way live intake does.
    name = raw.get("contact_name") or " ".join(p for p in (raw.get("first_name"), raw.get("last_name")) if p) or None
    tail = ", ".join(p for p in (raw.get("city"), " ".join(p for p in (raw.get("province"), raw.get("postal_code")) if p) or None) if p)
    contact_address = raw.get("contact_address")
    if contact_address and tail and tail.lower() not in contact_address.lower():
        contact_address = f"{contact_address}, {tail}"
    elif not contact_address and tail and not raw.get("job_address"):
        contact_address = tail
    job_address = raw.get("job_address")
    if job_address and tail and tail.lower() not in job_address.lower() and not re.search(r"\d", job_address.split(",")[-1]):
        job_address = f"{job_address}, {tail}"
    message = raw.get("message")
    if extras.get("notes2"):
        message = (message + "\n" if message else "") + extras["notes2"]
    f = IntakeFields(
        contact_name=clean_text(name, max_len=200), company=clean_text(raw.get("company"), max_len=200),
        phone_raw=clean_text(raw.get("phone"), max_len=64), email=clean_text(raw.get("email"), max_len=254),
        contact_address=clean_text(contact_address, max_len=300), job_address=clean_text(job_address, max_len=300),
        job_type=clean_text(raw.get("job_type"), max_len=120), message=clean_text(message, max_len=4000),
        channel=Channel.IMPORT, platform=platform_from_text(extras.get("source")),
    )
    if extras.get("source"):
        f.attribution["utm_source"] = extras["source"][:200]
    return f, extras


def run(
    conn: psycopg.Connection,
    tenant: dict,
    rows: list[dict[str, str]],
    mapping: dict[str, str],
    status_map: dict[str, str],
    *,
    batch_id: UUID | None,
    actor_id: UUID | None,
    actor_label: str,
    commit: bool,
) -> RunResult:
    """Dry run (commit=False) reports what would happen; commit=True writes it."""
    result = RunResult()
    tenant_id: UUID = tenant["id"]
    seen_keys: set[str] = set()
    now = datetime.now(UTC)

    for i, row in enumerate(rows, start=2):  # row 1 is the header line
        fields, extras = _fields_from_row(row, mapping)
        phone = normalize_phone(fields.phone_raw)
        email = normalize_email(fields.email)
        label = fields.contact_name or fields.company or phone or email or f"row {i}"
        if not phone and not email:
            result.rejected += 1
            result.outcomes.append(RowOutcome(i, "reject", label, "no usable phone number or email"))
            continue
        key = phone or email.lower()
        if key in seen_keys:
            result.rejected += 1
            result.outcomes.append(RowOutcome(i, "reject", label, "duplicate of an earlier row in this file"))
            continue
        seen_keys.add(key)

        status_word = extras.get("status", "")
        target = status_map.get(status_word, guess_status(status_word)) if ("status" in mapping.values()) else "review"
        needs_review = target == "review"
        status = LeadStatus.CONTACTED if needs_review else LeadStatus(target)
        deal_value = parse_money(extras.get("deal_value"))
        if status == LeadStatus.WON and deal_value is None:
            deal_value = Decimal(0)
        received = parse_date(extras.get("created_at"), tenant.get("timezone") or "UTC") or now

        existing = find_duplicate(conn, tenant_id, phone, email) if commit or True else None
        if existing is not None and (now - existing["created_at"]) <= DEDUPE_WINDOW:
            result.merged += 1
            result.outcomes.append(RowOutcome(i, "merge", label, f"already in the CRM as {existing['ref_code']}", status.value))
            if commit:
                log_activity(
                    conn, tenant_id, existing["id"], ActivityType.IMPORT, actor_id=actor_id, actor_label=actor_label,
                    body=fields.message, detail={"batch": str(batch_id), "row": i, "their_status": status_word},
                )
            continue

        result.created += 1
        result.outcomes.append(RowOutcome(i, "create", label, "", status.value))
        if not commit:
            continue

        ref_code = next_ref_code(conn, tenant_id)
        flags: dict[str, Any] = {"imported": True}
        if needs_review:
            flags["needs_review"] = True
        lead = conn.execute(
            """
            INSERT INTO lead (
                tenant_id, ref_code, previous_lead_id, assigned_to, contact_name, company, phone_e164, phone_raw, email,
                contact_address, job_address, job_type, message, channel, platform, flags, status, deal_value,
                lost_reason, won_at, lost_at, quoted_at, first_contact_at, created_at, import_batch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'import', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id, ref_code, existing["id"] if existing else None, tenant.get("default_assignee_id"),
                fields.contact_name, fields.company, phone, fields.phone_raw, email, fields.contact_address,
                fields.job_address, fields.job_type, fields.message, fields.platform.value, Jsonb(flags),
                status.value, deal_value if status == LeadStatus.WON else None,
                LostReason.OTHER.value if status == LeadStatus.LOST else None,
                received if status == LeadStatus.WON else None, received if status == LeadStatus.LOST else None,
                received if status == LeadStatus.QUOTED else None,
                received if status in (LeadStatus.CONTACTED, LeadStatus.QUOTED, LeadStatus.WON, LeadStatus.LOST) else None,
                received, batch_id,
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO lead_source (tenant_id, lead_id, channel, platform, utm_source, raw_payload) VALUES (%s, %s, 'import', %s, %s, %s)",
            (tenant_id, lead["id"], fields.platform.value, fields.attribution.get("utm_source"), Jsonb({"row": i, **row})),
        )
        log_activity(
            conn, tenant_id, lead["id"], ActivityType.IMPORT, actor_id=actor_id, actor_label=actor_label,
            body=f"Imported with status \"{status_word}\"" if status_word else "Imported",
            detail={"batch": str(batch_id), "row": i, "their_status": status_word, "mapped_to": status.value, "needs_review": needs_review},
            occurred_at=received,
        )
    return result
