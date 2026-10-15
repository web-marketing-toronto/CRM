"""Feeding outcomes back to the ad platforms, and pulling spend in.

Two status changes push out: the first of Contacted / Quoted sends "Qualified Lead", and Won sends
"Closed Won" with the deal value in CAD. Each push is a conversion_event row that the minute tick
works through with backoff for 72 hours, so a failed match is never silent: it shows on the lead.

    Google Ads   uploadClickConversions with the gclid/gbraid/wbraid when we have one, always with
                 hashed email + phone (Enhanced Conversions for Leads matches on those)
    Meta         Conversions API stage events (system_generated) with the leadgen id for instant-form
                 leads, fbc/fbp + hashed email/phone for website leads

Imported leads, test leads and leads that are still "to review" never upload. The conversion time
is the status change, never the import date.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.models import ActivityType, Channel, LeadStatus, Platform
from app.services import adplatforms
from app.services import leads as lead_svc

log = logging.getLogger("crm.conversions")

RETRY_FOR = timedelta(hours=72)
MAX_BACKOFF = timedelta(hours=6)
ACTION_LABELS = {"qualified": "Qualified lead", "won": "Closed won"}
PLATFORM_LABELS = {"google_ads": "Google Ads", "meta_ads": "Meta"}

GOOGLE_DEFAULTS: dict[str, Any] = {"customer_id": "", "qualified_action": "", "won_action": "", "qualified_value": 0}
META_DEFAULTS: dict[str, Any] = {"page_id": "", "dataset_id": "", "ad_account_id": "", "qualified_event": "Qualified", "won_event": "Converted", "test_event_code": ""}
CONV_DEFAULTS: dict[str, Any] = {"enabled": True, "value_bidding_since": ""}


def google_cfg(tenant: dict) -> dict[str, Any]:
    return {**GOOGLE_DEFAULTS, **((tenant.get("settings") or {}).get("google_ads") or {})}


def meta_cfg(tenant: dict) -> dict[str, Any]:
    return {**META_DEFAULTS, **((tenant.get("settings") or {}).get("meta") or {})}


def conv_cfg(tenant: dict) -> dict[str, Any]:
    return {**CONV_DEFAULTS, **((tenant.get("settings") or {}).get("conversions") or {})}


def _skip(lead: dict) -> bool:
    flags = lead.get("flags") or {}
    return lead.get("channel") == Channel.IMPORT.value or bool(flags.get("needs_review")) or bool(flags.get("test"))


def _source(conn: psycopg.Connection, lead_id: UUID) -> dict | None:
    return conn.execute("SELECT * FROM lead_source WHERE lead_id = %s ORDER BY created_at ASC LIMIT 1", (lead_id,)).fetchone()


def platforms_for(tenant: dict, lead: dict, source: dict | None) -> list[str]:
    out: list[str] = []
    g, m = google_cfg(tenant), meta_cfg(tenant)
    src = source or {}
    if g["customer_id"] and lead.get("platform") != Platform.META_ADS.value and (src.get("gclid") or src.get("gbraid") or src.get("wbraid") or lead.get("email") or lead.get("phone_e164")):
        out.append(Platform.GOOGLE_ADS.value)
    if m["dataset_id"] and (lead.get("platform") == Platform.META_ADS.value or src.get("fbclid") or src.get("fbc") or src.get("fbp") or src.get("meta_lead_id")):
        out.append(Platform.META_ADS.value)
    return out


def queue(conn: psycopg.Connection, *, tenant_id: UUID, lead_id: UUID, platform: str, action: str, value: Decimal | None, event_time: datetime) -> bool:
    row = conn.execute(
        """
        INSERT INTO conversion_event (tenant_id, lead_id, platform, action, value, currency, status, event_time)
        VALUES (%s, %s, %s, %s, %s, 'CAD', 'pending', %s)
        ON CONFLICT (lead_id, platform, action) DO NOTHING
        RETURNING id
        """,
        (tenant_id, lead_id, platform, action, value, event_time),
    ).fetchone()
    return row is not None


def on_status_change(conn: psycopg.Connection, tenant: dict, lead: dict, old: LeadStatus, new: LeadStatus, *, now: datetime | None = None, deal_value: Decimal | None = None) -> list[str]:
    """Queue what this status change means for the ad platforms. Returns the actions queued."""
    if _skip(lead) or not conv_cfg(tenant)["enabled"]:
        return []
    now = now or datetime.now(UTC)
    source = _source(conn, lead["id"])
    platforms = platforms_for(tenant, lead, source)
    if not platforms:
        return []
    queued: list[str] = []
    # Reaching Contacted, Quoted or Won for the first time is "qualified"; the unique index keeps it to once per lead.
    if new in (LeadStatus.CONTACTED, LeadStatus.QUOTED, LeadStatus.WON):
        g = google_cfg(tenant)
        qv = Decimal(str(g.get("qualified_value") or 0)) or None
        for p in platforms:
            if queue(conn, tenant_id=tenant["id"], lead_id=lead["id"], platform=p, action="qualified", value=qv if p == "google_ads" else None, event_time=now):
                queued.append(f"qualified:{p}")
    if new == LeadStatus.WON:
        value = deal_value if deal_value is not None else lead.get("deal_value")
        for p in platforms:
            if queue(conn, tenant_id=tenant["id"], lead_id=lead["id"], platform=p, action="won", value=value, event_time=now):
                queued.append(f"won:{p}")
    return queued


# --- Building the payloads ----------------------------------------------------------------------

def _google_time(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d %H:%M:%S%z")[:-2] + ":" + local.strftime("%z")[-2:]


def build_google(tenant: dict, lead: dict, source: dict | None, ev: dict) -> tuple[str, dict[str, Any]]:
    g = google_cfg(tenant)
    action = g["qualified_action"] if ev["action"] == "qualified" else g["won_action"]
    if not action:
        raise adplatforms.AdsError(f"No Google conversion action set for '{ev['action']}' on {tenant['name']}", retryable=False)
    customer = str(g["customer_id"]).replace("-", "")
    if "/" not in action:
        action = f"customers/{customer}/conversionActions/{action}"
    conv: dict[str, Any] = {
        "conversionAction": action,
        "conversionDateTime": _google_time(ev["event_time"], tenant.get("timezone") or "America/Toronto"),
        "currencyCode": "CAD",
        "orderId": f"{lead['id']}:{ev['action']}",
        "consent": {"adUserData": "GRANTED", "adPersonalization": "GRANTED"},
    }
    if ev.get("value") is not None:
        conv["conversionValue"] = float(ev["value"])
    src = source or {}
    if src.get("gclid"):
        conv["gclid"] = src["gclid"]
    elif src.get("gbraid"):
        conv["gbraid"] = src["gbraid"]
    elif src.get("wbraid"):
        conv["wbraid"] = src["wbraid"]
    ids = []
    if lead.get("email"):
        ids.append({"hashedEmail": adplatforms.sha256(adplatforms.normalize_email_for_hash(lead["email"]))})
    if lead.get("phone_e164"):
        ids.append({"hashedPhoneNumber": adplatforms.sha256(lead["phone_e164"])})
    if ids:
        conv["userIdentifiers"] = ids
    if not ids and "gclid" not in conv and "gbraid" not in conv and "wbraid" not in conv:
        raise adplatforms.AdsError("Nothing to match on: no click id, email or phone", retryable=False)
    return customer, conv


def build_meta(tenant: dict, lead: dict, source: dict | None, ev: dict) -> tuple[str, dict[str, Any]]:
    m = meta_cfg(tenant)
    if not m["dataset_id"]:
        raise adplatforms.AdsError(f"No Meta dataset id on {tenant['name']}", retryable=False)
    src = source or {}
    user: dict[str, Any] = {}
    if src.get("meta_lead_id"):
        user["lead_id"] = src["meta_lead_id"]
    if lead.get("email"):
        user["em"] = [adplatforms.sha256(adplatforms.normalize_email_for_hash(lead["email"]))]
    if lead.get("phone_e164"):
        user["ph"] = [adplatforms.sha256(lead["phone_e164"].lstrip("+"))]
    if src.get("fbc"):
        user["fbc"] = src["fbc"]
    if src.get("fbp"):
        user["fbp"] = src["fbp"]
    if not user:
        raise adplatforms.AdsError("Nothing to match on: no lead id, email, phone or pixel cookies", retryable=False)
    event: dict[str, Any] = {
        "event_name": m["qualified_event"] if ev["action"] == "qualified" else m["won_event"],
        "event_time": int(ev["event_time"].timestamp()),
        "event_id": f"{lead['id']}:{ev['action']}",
        "action_source": "system_generated",
        "user_data": user,
        "custom_data": {"lead_event_source": get_settings().agency_name + " CRM", "event_source": "crm", "currency": "CAD"},
    }
    if ev.get("value") is not None:
        event["custom_data"]["value"] = float(ev["value"])
    return str(m["dataset_id"]), event


# --- Sending -------------------------------------------------------------------------------------

def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=5) * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)


def process_pending(conn: psycopg.Connection, *, now: datetime | None = None, limit: int = 50, lead_id: UUID | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT * FROM conversion_event
        WHERE status = 'pending' AND (%s::uuid IS NULL OR lead_id = %s)
        ORDER BY created_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (lead_id, lead_id, limit),
    ).fetchall()
    stats = {"sent": 0, "failed": 0, "deferred": 0}
    for ev in rows:
        if ev["attempted_at"] and ev["attempted_at"] + _backoff(ev["attempts"]) > now:
            stats["deferred"] += 1
            continue
        lead = conn.execute("SELECT * FROM lead WHERE id = %s", (ev["lead_id"],)).fetchone()
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (ev["tenant_id"],)).fetchone()
        if lead is None or tenant is None:
            conn.execute("UPDATE conversion_event SET status = 'failed', last_error = 'lead or client gone' WHERE id = %s", (ev["id"],))
            continue
        source = _source(conn, lead["id"])
        label = f"{ACTION_LABELS[ev['action']]} → {PLATFORM_LABELS.get(ev['platform'], ev['platform'])}"
        try:
            if ev["platform"] == "google_ads":
                customer, payload = build_google(tenant, lead, source, ev)
                response = adplatforms.google_upload_conversion(customer, payload)
            else:
                dataset, payload = build_meta(tenant, lead, source, ev)
                response = adplatforms.meta_send_event(dataset, payload, test_event_code=meta_cfg(tenant).get("test_event_code") or None)
        except adplatforms.AdsError as exc:
            attempts = ev["attempts"] + 1
            give_up = not exc.retryable or (now - ev["created_at"]) > RETRY_FOR
            conn.execute(
                "UPDATE conversion_event SET attempts = %s, attempted_at = %s, last_error = %s, status = %s WHERE id = %s",
                (attempts, now, str(exc)[:500], "failed" if give_up else "pending", ev["id"]),
            )
            if give_up:
                stats["failed"] += 1
                lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.CONVERSION_SENT, body=f"{label}: not sent ({exc})"[:1000], detail={"platform": ev["platform"], "action": ev["action"], "status": "failed", "error": str(exc)[:500]}, occurred_at=now)
            else:
                stats["deferred"] += 1
            continue
        conn.execute(
            "UPDATE conversion_event SET status = 'sent', sent_at = %s, attempted_at = %s, attempts = attempts + 1, response = %s, last_error = NULL WHERE id = %s",
            (now, now, Jsonb(response), ev["id"]),
        )
        value = f" (${ev['value']:,.0f})" if ev.get("value") else ""
        lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.CONVERSION_SENT, body=f"{label}{value}", detail={"platform": ev["platform"], "action": ev["action"], "status": "sent", "value": str(ev["value"]) if ev.get("value") is not None else None}, occurred_at=now)
        stats["sent"] += 1
    return stats


def retry(conn: psycopg.Connection, event_id: UUID) -> None:
    conn.execute("UPDATE conversion_event SET status = 'pending', attempts = 0, attempted_at = NULL, last_error = NULL, created_at = now() WHERE id = %s", (event_id,))


# --- Spend ---------------------------------------------------------------------------------------

def ingest_spend(conn: psycopg.Connection, since: date, until: date) -> dict[str, Any]:
    """Pull daily campaign spend for every client with an ad account configured; upsert into ad_spend."""
    report: dict[str, Any] = {"tenants": 0, "rows": 0, "errors": []}
    for tenant in conn.execute("SELECT * FROM tenant WHERE is_active").fetchall():
        g, m = google_cfg(tenant), meta_cfg(tenant)
        jobs: list[tuple[str, str]] = []
        if g["customer_id"]:
            jobs.append(("google_ads", str(g["customer_id"])))
        if m["ad_account_id"]:
            jobs.append(("meta_ads", str(m["ad_account_id"])))
        if not jobs:
            continue
        report["tenants"] += 1
        for platform, account in jobs:
            try:
                rows = adplatforms.google_campaign_spend(account, since, until) if platform == "google_ads" else adplatforms.meta_campaign_spend(account, since, until)
            except adplatforms.AdsError as exc:
                report["errors"].append(f"{tenant['name']} {platform}: {exc}")
                log.warning("spend %s %s: %s", tenant["slug"], platform, exc)
                continue
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO ad_spend (tenant_id, platform, campaign_id, campaign_name, date, spend, clicks, impressions, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (tenant_id, platform, campaign_id, date) DO UPDATE
                    SET campaign_name = EXCLUDED.campaign_name, spend = EXCLUDED.spend, clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions, updated_at = now()
                    """,
                    (tenant["id"], platform, r["campaign_id"], r.get("campaign_name"), r["date"], round(float(r["spend"]), 2), int(r.get("clicks", 0)), int(r.get("impressions", 0))),
                )
                report["rows"] += 1
    return report


def run_nightly(conn: psycopg.Connection, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Once a day after 04:00 Toronto: yesterday's and the day before's spend (platforms restate late)."""
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo("America/Toronto"))
    if local.hour < 4:
        return None
    claimed = conn.execute(
        "INSERT INTO job_run (name, ran_on) VALUES ('spend', %s) ON CONFLICT DO NOTHING RETURNING name", (local.date(),)
    ).fetchone()
    if claimed is None:
        return None
    report = ingest_spend(conn, local.date() - timedelta(days=2), local.date() - timedelta(days=1))
    conn.execute("UPDATE job_run SET finished_at = now(), detail = %s WHERE name = 'spend' AND ran_on = %s", (Jsonb(report), local.date()))
    return report
