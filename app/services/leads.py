"""Lead lifecycle: create (with dedupe), log touches, change status, assign.

All functions take an open psycopg connection whose RLS context is already set by `app.db.db`.
They never commit; the caller's transaction does.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.models import (
    TOUCH_TYPES,
    ActivityType,
    AreaStatus,
    Channel,
    LeadStatus,
    LostReason,
    Platform,
)
from app.services import geo
from app.services.attribution import IntakeFields
from app.services.normalize import normalize_email, normalize_phone, postal_fsa

DEDUPE_WINDOW = timedelta(days=90)


class LeadError(Exception):
    pass


@dataclass
class IntakeResult:
    lead_id: UUID
    ref_code: str
    created: bool  # False when the submission attached to an existing lead
    reopened: bool
    area_status: AreaStatus


def next_ref_code(conn: psycopg.Connection, tenant_id: UUID) -> str:
    """A000 … Z999, per tenant, short enough to type back from an SMS."""
    row = conn.execute(
        "UPDATE tenant SET lead_seq = lead_seq + 1 WHERE id = %s RETURNING lead_seq", (tenant_id,)
    ).fetchone()
    if row is None:
        raise LeadError("tenant not found")
    n = int(row["lead_seq"])
    return f"{chr(65 + (n // 1000) % 26)}{n % 1000:03d}"


def log_activity(
    conn: psycopg.Connection,
    tenant_id: UUID,
    lead_id: UUID,
    type_: ActivityType,
    *,
    actor_id: UUID | None = None,
    actor_label: str = "system",
    body: str | None = None,
    detail: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO activity (tenant_id, lead_id, type, actor_id, actor_label, body, detail, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
        RETURNING id
        """,
        (tenant_id, lead_id, type_.value, actor_id, actor_label, body, Jsonb(detail or {}), occurred_at),
    ).fetchone()
    return int(row["id"])


def find_duplicate(conn: psycopg.Connection, tenant_id: UUID, phone_e164: str | None, email: str | None) -> dict | None:
    """Most recent lead in this tenant sharing a phone number or email address."""
    if not phone_e164 and not email:
        return None
    row = conn.execute(
        """
        SELECT id, ref_code, status, created_at
        FROM lead
        WHERE tenant_id = %s
          AND ((%s::text IS NOT NULL AND phone_e164 = %s) OR (%s::text IS NOT NULL AND email_lower = lower(%s)))
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant_id, phone_e164, phone_e164, email, email),
    ).fetchone()
    return row


def evaluate_area(tenant: dict, fields: IntakeFields, *, maps_api_key: str | None) -> tuple[AreaStatus, float | None, float | None, bool]:
    """Returns (status, lat, lng, precise). Uses the job address, falling back to the contact address."""
    service_area = tenant.get("service_area") or {}
    address = fields.job_address or fields.contact_address
    if not address:
        return AreaStatus.UNKNOWN, None, None, False
    if not service_area:
        return AreaStatus.UNKNOWN, None, None, False
    result = geo.geocode(address, maps_api_key)
    if result is not None and (service_area.get("polygon") or service_area.get("center")):
        return geo.evaluate_point(service_area, result.lat, result.lng), result.lat, result.lng, result.precise
    fsa_status = geo.evaluate_fsa(service_area, postal_fsa(address))
    if result is not None:
        return fsa_status, result.lat, result.lng, result.precise
    return fsa_status, None, None, False


def create_from_intake(
    conn: psycopg.Connection,
    tenant: dict,
    fields: IntakeFields,
    *,
    raw_payload: dict[str, Any],
    ip: str | None,
    user_agent: str | None,
    maps_api_key: str | None,
    now: datetime | None = None,
    extra_flags: dict[str, Any] | None = None,
) -> IntakeResult:
    now = now or datetime.now(UTC)
    tenant_id: UUID = tenant["id"]
    phone_e164 = normalize_phone(fields.phone_raw)
    email = normalize_email(fields.email)
    if not phone_e164 and not email and not fields.contact_name:
        raise LeadError("a lead needs at least a name, a phone number or an email address")

    area_status, lat, lng, precise = evaluate_area(tenant, fields, maps_api_key=maps_api_key)
    attribution = dict(fields.attribution)
    source_detail = {
        "channel": fields.channel.value,
        "platform": fields.platform.value,
        "form_name": fields.form_name,
        "campaign": fields.campaign_name,
    }

    existing = find_duplicate(conn, tenant_id, phone_e164, email)
    if existing is not None and (now - existing["created_at"]) <= DEDUPE_WINDOW:
        lead_id: UUID = existing["id"]
        reopened = False
        if existing["status"] in (LeadStatus.WON.value, LeadStatus.LOST.value):
            conn.execute(
                "UPDATE lead SET status = 'new', lost_reason = NULL, next_action_at = NULL WHERE id = %s", (lead_id,)
            )
            log_activity(
                conn, tenant_id, lead_id, ActivityType.STATUS_CHANGE,
                body="Reopened: contacted us again", detail={"from": existing["status"], "to": "new"},
            )
            reopened = True
        _insert_source(conn, tenant_id, lead_id, fields, attribution, raw_payload, ip, user_agent)
        # Enrich blanks on the existing record without overwriting what the client typed.
        conn.execute(
            """
            UPDATE lead SET
                contact_name = COALESCE(contact_name, %s),
                company = COALESCE(company, %s),
                phone_e164 = COALESCE(phone_e164, %s),
                phone_raw = COALESCE(phone_raw, %s),
                email = COALESCE(email, %s),
                contact_address = COALESCE(contact_address, %s),
                job_address = COALESCE(job_address, %s),
                job_type = COALESCE(job_type, %s)
            WHERE id = %s
            """,
            (
                fields.contact_name, fields.company, phone_e164, fields.phone_raw, email,
                fields.contact_address, fields.job_address, fields.job_type, lead_id,
            ),
        )
        log_activity(
            conn, tenant_id, lead_id, ActivityType.DUPLICATE_SUBMISSION,
            body=fields.message, detail=source_detail,
        )
        from app.services import followup

        refreshed = conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
        if reopened:
            followup.on_lead_created(conn, tenant, {**refreshed, "created_at": now}, now=now)
        else:
            followup.on_resubmitted(conn, tenant, refreshed, now=now)
        return IntakeResult(lead_id=lead_id, ref_code=existing["ref_code"], created=False, reopened=reopened, area_status=area_status)

    ref_code = next_ref_code(conn, tenant_id)
    flags: dict[str, Any] = dict(extra_flags or {})
    if area_status == AreaStatus.OUT and not precise:
        flags["possibly_out_of_area"] = True
    elif area_status == AreaStatus.NEAR:
        flags["near_edge_of_area"] = True
    if fields.channel == Channel.CALL and raw_payload.get("missed") in (True, "true", "1", 1):
        flags["missed_call"] = True
    assignee = tenant.get("default_assignee_id")

    row = conn.execute(
        """
        INSERT INTO lead (
            tenant_id, ref_code, previous_lead_id, assigned_to, contact_name, company, phone_e164, phone_raw, email,
            contact_address, job_address, job_lat, job_lng, area_status, job_type, message, channel, platform,
            campaign_name, flags, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id, ref_code, existing["id"] if existing else None, assignee, fields.contact_name, fields.company,
            phone_e164, fields.phone_raw, email, fields.contact_address, fields.job_address, lat, lng,
            area_status.value, fields.job_type, fields.message, fields.channel.value, fields.platform.value,
            fields.campaign_name, Jsonb(flags), now,
        ),
    ).fetchone()
    lead_id = row["id"]
    _insert_source(conn, tenant_id, lead_id, fields, attribution, raw_payload, ip, user_agent)
    log_activity(conn, tenant_id, lead_id, ActivityType.CREATED, body=fields.message, detail=source_detail, occurred_at=now)
    if assignee:
        log_activity(conn, tenant_id, lead_id, ActivityType.ASSIGNMENT, detail={"to": str(assignee), "how": "default"}, occurred_at=now)
    from app.services import followup

    followup.on_lead_created(
        conn, tenant,
        {"id": lead_id, "created_at": now, "assigned_to": assignee, "channel": fields.channel.value, "flags": flags, "status": "new"},
        now=now,
    )
    return IntakeResult(lead_id=lead_id, ref_code=ref_code, created=True, reopened=False, area_status=area_status)


def _insert_source(
    conn: psycopg.Connection,
    tenant_id: UUID,
    lead_id: UUID,
    fields: IntakeFields,
    attribution: dict[str, str],
    raw_payload: dict[str, Any],
    ip: str | None,
    user_agent: str | None,
) -> None:
    a = attribution
    conn.execute(
        """
        INSERT INTO lead_source (
            tenant_id, lead_id, channel, platform, gclid, gbraid, wbraid, fbclid, fbc, fbp, meta_lead_id,
            google_lead_id, callrail_call_id, utm_source, utm_medium, utm_campaign, utm_term, utm_content,
            campaign_id, adset_id, ad_id, landing_page, referrer, form_name, ip, user_agent, raw_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id, lead_id, fields.channel.value, fields.platform.value, a.get("gclid"), a.get("gbraid"),
            a.get("wbraid"), a.get("fbclid"), a.get("fbc"), a.get("fbp"), a.get("meta_lead_id"),
            a.get("google_lead_id"), a.get("callrail_call_id"), a.get("utm_source"), a.get("utm_medium"),
            a.get("utm_campaign"), a.get("utm_term"), a.get("utm_content"), a.get("campaign_id"),
            a.get("adset_id"), a.get("ad_id"), a.get("landing_page"), a.get("referrer"), fields.form_name,
            _inet(ip or a.get("ip")), user_agent or a.get("user_agent"), Jsonb(_jsonable(raw_payload)),
        ),
    )


def _inet(value: str | None) -> str | None:
    if not value:
        return None
    v = value.split(",")[0].strip()
    try:
        ipaddress.ip_address(v)
    except ValueError:
        return None
    return v


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return json.loads(json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# Actions taken by users in the app
# ---------------------------------------------------------------------------

def get_lead(conn: psycopg.Connection, lead_id: UUID) -> dict | None:
    return conn.execute(
        """
        SELECT l.*, u.name AS assignee_name
        FROM lead l LEFT JOIN app_user u ON u.id = l.assigned_to
        WHERE l.id = %s
        """,
        (lead_id,),
    ).fetchone()


def log_touch(
    conn: psycopg.Connection, lead: dict, type_: ActivityType, *, actor_id: UUID | None, actor_label: str, note: str | None = None,
    now: datetime | None = None, detail: dict[str, Any] | None = None,
) -> None:
    """Record a call/text/email attempt. A New lead becomes Contacted; the last-touch clock resets."""
    if type_ not in TOUCH_TYPES:
        raise LeadError("not a touch type")
    tenant_id = lead["tenant_id"]
    now = now or datetime.now(UTC)
    log_activity(conn, tenant_id, lead["id"], type_, actor_id=actor_id, actor_label=actor_label, body=note, detail=detail, occurred_at=now)
    sets = ["last_touch_at = %s", "next_action_at = NULL", "flags = flags - 'needs_review'"]
    params: list[Any] = [now]
    if lead["first_contact_at"] is None:
        sets.append("first_contact_at = %s")
        params.append(now)
    if lead["status"] == LeadStatus.NEW.value:
        sets.append("status = 'contacted'")
        log_activity(
            conn, tenant_id, lead["id"], ActivityType.STATUS_CHANGE, actor_id=actor_id, actor_label=actor_label,
            detail={"from": "new", "to": "contacted", "auto": True}, occurred_at=now,
        )
    params.append(lead["id"])
    conn.execute(f"UPDATE lead SET {', '.join(sets)} WHERE id = %s", params)
    from app.services import followup

    followup.on_touch(conn, _tenant(conn, tenant_id), {**lead, "status": "contacted" if lead["status"] == "new" else lead["status"]}, now=now)


def _tenant(conn: psycopg.Connection, tenant_id: UUID) -> dict:
    return conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant_id,)).fetchone()


def change_status(
    conn: psycopg.Connection,
    lead: dict,
    new_status: LeadStatus,
    *,
    actor_id: UUID | None,
    actor_label: str,
    deal_value: Decimal | None = None,
    lost_reason: LostReason | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    old = LeadStatus(lead["status"])
    if new_status == old:
        return
    if new_status == LeadStatus.WON and deal_value is None:
        raise LeadError("Enter the deal value to mark a lead Won")
    if new_status == LeadStatus.LOST and lost_reason is None:
        raise LeadError("Pick a reason to mark a lead Lost")
    now = now or datetime.now(UTC)
    sets = ["status = %s", "next_action_at = NULL", "flags = flags - 'needs_review'"]
    params: list[Any] = [new_status.value]
    if new_status == LeadStatus.WON:
        sets += ["deal_value = %s", "won_at = %s", "lost_reason = NULL", "lost_at = NULL"]
        params += [deal_value, now]
    elif new_status == LeadStatus.LOST:
        sets += ["lost_reason = %s", "lost_at = %s", "deal_value = NULL", "won_at = NULL"]
        params += [lost_reason.value, now]
    elif new_status == LeadStatus.QUOTED:
        sets += ["quoted_at = %s"]
        params += [now]
    if new_status in (LeadStatus.CONTACTED, LeadStatus.QUOTED) and lead["first_contact_at"] is None:
        sets += ["first_contact_at = %s"]
        params += [now]
    if old.is_closed and not new_status.is_closed:
        sets += ["deal_value = NULL", "lost_reason = NULL", "won_at = NULL", "lost_at = NULL"]
    params.append(lead["id"])
    conn.execute(f"UPDATE lead SET {', '.join(sets)} WHERE id = %s", params)
    detail: dict[str, Any] = {"from": old.value, "to": new_status.value}
    if deal_value is not None:
        detail["deal_value"] = str(deal_value)
    if lost_reason is not None:
        detail["lost_reason"] = lost_reason.value
    log_activity(
        conn, lead["tenant_id"], lead["id"], ActivityType.STATUS_CHANGE,
        actor_id=actor_id, actor_label=actor_label, body=note, detail=detail, occurred_at=now,
    )
    from app.services import followup

    followup.on_status_change(conn, _tenant(conn, lead["tenant_id"]), lead, old, new_status, now=now)


def add_note(conn: psycopg.Connection, lead: dict, body: str, *, actor_id: UUID | None, actor_label: str) -> None:
    body = body.strip()
    if not body:
        raise LeadError("Note is empty")
    log_activity(conn, lead["tenant_id"], lead["id"], ActivityType.NOTE, actor_id=actor_id, actor_label=actor_label, body=body[:4000])


def assign(conn: psycopg.Connection, lead: dict, user_id: UUID | None, *, actor_id: UUID | None, actor_label: str) -> None:
    conn.execute("UPDATE lead SET assigned_to = %s WHERE id = %s", (user_id, lead["id"]))
    log_activity(
        conn, lead["tenant_id"], lead["id"], ActivityType.ASSIGNMENT, actor_id=actor_id, actor_label=actor_label,
        detail={"to": str(user_id) if user_id else None, "how": "manual"},
    )


def create_manual(
    conn: psycopg.Connection,
    tenant: dict,
    *,
    contact_name: str | None,
    phone_raw: str | None,
    email: str | None,
    company: str | None,
    contact_address: str | None,
    job_address: str | None,
    job_type: str | None,
    message: str | None,
    platform: Platform,
    actor_id: UUID | None,
    actor_label: str,
    maps_api_key: str | None,
) -> IntakeResult:
    fields = IntakeFields(
        contact_name=contact_name, company=company, phone_raw=phone_raw, email=email,
        contact_address=contact_address, job_address=job_address, job_type=job_type, message=message,
        channel=Channel.MANUAL, platform=platform,
    )
    result = create_from_intake(
        conn, tenant, fields, raw_payload={"entered_by": actor_label}, ip=None, user_agent=None, maps_api_key=maps_api_key
    )
    if result.created:
        conn.execute("UPDATE activity SET actor_id = %s, actor_label = %s WHERE lead_id = %s AND type = 'created'", (actor_id, actor_label, result.lead_id))
    return result
