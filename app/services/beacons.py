"""Attribution beacons: wmt.js reports the ad details (gclid, fbclid, UTMs, landing page) at the moment a
form is submitted, keyed by the email and phone typed into it. When the lead itself arrives by a route
that can't carry those fields (an emailed notification, a Wix form, a call), the intake looks for a
recent beacon with the same email or phone and takes its attribution."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.services import attribution as attr
from app.services.normalize import normalize_email, normalize_phone

MATCH_WINDOW = timedelta(hours=48)
CLICK_KEYS = ("gclid", "gbraid", "wbraid", "fbclid", "fbc", "fbp", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "campaign_id", "adset_id", "ad_id", "landing_page", "referrer")


def record(conn: psycopg.Connection, tenant: dict, payload: dict[str, Any], *, ip: str | None, user_agent: str | None) -> int | None:
    email = normalize_email(str(payload.get("email") or "")) or None
    phone = normalize_phone(str(payload.get("phone") or "")) if payload.get("phone") else None
    if not email and not phone:
        return None
    a = {k: str(payload.get(k))[:1000] for k in CLICK_KEYS if payload.get(k)}
    page = str(payload.get("page_url") or payload.get("landing_page") or "")[:1000] or None
    try:
        ip = str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        ip = None
    row = conn.execute(
        "INSERT INTO attribution_beacon (tenant_id, email_lower, phone_e164, name, attribution, page_url, ip, user_agent) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant["id"], email, phone, (str(payload.get("name") or "")[:200] or None), Jsonb(a), page, ip, (user_agent or "")[:300] or None),
    ).fetchone()
    return row["id"]


def claim(conn: psycopg.Connection, tenant_id: UUID, email: str | None, phone_e164: str | None, *, now: datetime | None = None) -> dict | None:
    """The newest unclaimed beacon for this person within the window, if any."""
    if not email and not phone_e164:
        return None
    now = now or datetime.now(UTC)
    return conn.execute(
        """
        SELECT * FROM attribution_beacon
        WHERE tenant_id = %s AND lead_id IS NULL AND created_at > %s
          AND ((%s::text IS NOT NULL AND email_lower = %s) OR (%s::text IS NOT NULL AND phone_e164 = %s))
        ORDER BY created_at DESC LIMIT 1
        """,
        (tenant_id, now - MATCH_WINDOW, email, email, phone_e164, phone_e164),
    ).fetchone()


def apply(fields: attr.IntakeFields, beacon: dict) -> bool:
    """Fill the attribution the lead arrived without. Returns whether anything changed."""
    changed = False
    for k, v in (beacon.get("attribution") or {}).items():
        if v and not fields.attribution.get(k):
            fields.attribution[k] = v
            changed = True
    if changed:
        fields.platform = attr.infer_platform(fields.channel, fields.attribution)
        fields.campaign_name = fields.campaign_name or fields.attribution.get("campaign_name") or fields.attribution.get("utm_campaign")
    return changed


def attach(conn: psycopg.Connection, beacon_id: int, lead_id: UUID) -> None:
    conn.execute("UPDATE attribution_beacon SET lead_id = %s WHERE id = %s", (lead_id, beacon_id))


def has_click(fields: attr.IntakeFields) -> bool:
    return any(fields.attribution.get(k) for k in ("gclid", "gbraid", "wbraid", "fbclid", "utm_source"))
