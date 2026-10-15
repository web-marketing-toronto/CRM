"""Agency-only screens: clients (tenants), their settings, users, and intake instructions."""

from __future__ import annotations

import json
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb

from app import web
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_agency
from app.auth.service import AuthError, Principal
from app.config import get_settings
from app.db import db
from app.models import Role
from app.services import conversions, followup, importer, lead_messages
from app.services.normalize import normalize_phone

router = APIRouter(prefix="/admin", tags=["admin"])


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:40] or "client"


@router.get("")
def dashboard(request: Request, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        tenants = conn.execute(
            """
            SELECT t.*,
                   (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status NOT IN ('won','lost')) AS open_leads,
                   (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'new') AS new_leads,
                   (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.created_at > now() - interval '30 days') AS leads_30d,
                   (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'won' AND l.won_at > now() - interval '30 days') AS won_30d,
                   (SELECT coalesce(sum(deal_value), 0) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'won' AND l.won_at > now() - interval '30 days') AS won_value_30d,
                   (SELECT max(created_at) FROM lead l WHERE l.tenant_id = t.id) AS last_lead_at,
                   (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.is_active) AS user_count
            FROM tenant t
            ORDER BY t.name
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT count(*) FILTER (WHERE created_at > now() - interval '30 days') AS leads_30d,
                   count(*) FILTER (WHERE status = 'new' AND created_at < now() - interval '24 hours') AS stale_new,
                   count(*) FILTER (WHERE status = 'won' AND won_at > now() - interval '30 days') AS won_30d,
                   coalesce(sum(deal_value) FILTER (WHERE status = 'won' AND won_at > now() - interval '30 days'), 0) AS won_value_30d
            FROM lead
            """
        ).fetchone()
    return web.render(request, "admin/dashboard.html", tenants=tenants, totals=totals)


@router.get("/tenants/new")
def tenant_new(request: Request, principal: Annotated[Principal, Depends(require_agency)]):
    return web.render(request, "admin/tenant_new.html", error=None, form={})


@router.post("/tenants/new", dependencies=[Depends(check_csrf)])
def tenant_create(
    request: Request,
    principal: Annotated[Principal, Depends(require_agency)],
    name: Annotated[str, Form()],
    slug: Annotated[str, Form()] = "",
    timezone: Annotated[str, Form()] = "America/Toronto",
    job_types: Annotated[str, Form()] = "",
):
    slug = _slugify(slug or name)
    types = [t.strip() for t in re.split(r"[\n,]", job_types) if t.strip()]
    with db(None, "agency_admin") as conn:
        if conn.execute("SELECT 1 FROM tenant WHERE slug = %s", (slug,)).fetchone():
            return web.render(request, "admin/tenant_new.html", status_code=400, error=f"The short name '{slug}' is taken.", form={"name": name, "slug": slug, "job_types": job_types})
        row = conn.execute(
            "INSERT INTO tenant (slug, name, timezone, job_types) VALUES (%s, %s, %s, %s) RETURNING id",
            (slug, name.strip(), timezone or "America/Toronto", types),
        ).fetchone()
        auth_service.audit(conn, action="tenant_created", user_id=principal.user_id, tenant_id=row["id"], target_type="tenant", target_id=str(row["id"]))
    return RedirectResponse(f"/admin/tenants/{row['id']}", status_code=303)


def _tenant_ctx(tenant_id: UUID) -> dict:
    with db(None, "agency_admin") as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        if tenant is None:
            raise HTTPException(status_code=404, detail="Client not found")
        users = conn.execute("SELECT * FROM app_user WHERE tenant_id = %s ORDER BY role, name", (tenant_id,)).fetchall()
        recent = conn.execute(
            "SELECT id, ref_code, contact_name, status, channel, platform, created_at FROM lead WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 10",
            (tenant_id,),
        ).fetchall()
        recent_conv = conn.execute(
            """
            SELECT c.*, l.ref_code, l.contact_name FROM conversion_event c JOIN lead l ON l.id = c.lead_id
            WHERE c.tenant_id = %s ORDER BY c.created_at DESC LIMIT 10
            """,
            (tenant_id,),
        ).fetchall()
    settings = get_settings()
    hours = tenant["business_hours"] or {}
    return {
        "tenant": tenant,
        "users": users,
        "recent": recent,
        "intake_url": f"{settings.base_url}/v1/intake/{tenant['slug']}/{tenant['intake_key']}",
        "callrail_url": f"{settings.base_url}/v1/callrail/{tenant['slug']}/{tenant['intake_key']}",
        "google_leads_url": f"{settings.base_url}/v1/google-leads/{tenant['slug']}/{tenant['intake_key']}",
        "meta_webhook_url": f"{settings.base_url}/v1/meta/webhook",
        "meta_configured": bool(settings.meta_app_secret and settings.meta_access_token),
        "snippet_url": f"{settings.base_url}/static/wmt.js",
        "meta_page_id": ((tenant.get("settings") or {}).get("meta") or {}).get("page_id", ""),
        "google_ads": conversions.google_cfg(tenant),
        "meta_cfg": conversions.meta_cfg(tenant),
        "conv": conversions.conv_cfg(tenant),
        "recent_conversions": recent_conv,
        "call_min_seconds": ((tenant.get("settings") or {}).get("callrail") or {}).get("min_seconds", 30),
        "field_map_json": json.dumps(tenant["field_map"], indent=2),
        "service_area_json": json.dumps(tenant["service_area"], indent=2),
        "hours_start": _twelve_hour(hours.get("start", "07:00")),
        "hours_end": _twelve_hour(hours.get("end", "19:00")),
        "hours_days": [int(d) for d in hours.get("days", [1, 2, 3, 4, 5, 6])],
        "job_types_text": "\n".join(tenant["job_types"] or []),
        "followup": followup.settings_for(tenant),
        "followup_defaults": followup.DEFAULTS,
        "lead_msg": lead_messages.settings_for(tenant),
        "lead_msg_default_templates": lead_messages.TEMPLATES,
        "placeholders": lead_messages.PLACEHOLDERS,
    }


def _twelve_hour(value: str) -> str:
    """'07:00' -> '7:00', '19:30' -> '7:30' (the AM/PM is fixed by the field)."""
    try:
        h, m = value.split(":")
        h = int(h) % 12 or 12
        return f"{h}:{int(m):02d}"
    except (ValueError, AttributeError):
        return value


def parse_hours(start: str, end: str, days: list[str]) -> dict:
    """Two plain fields, start is always AM and end always PM, into the stored 24-hour form."""
    def clock(text: str, pm: bool) -> str:
        text = text.strip().lower().replace("am", "").replace("pm", "").replace(" ", "")
        if not text:
            raise ValueError("Enter both a start and an end time, like 7:00 and 6:30")
        if ":" in text:
            h_s, m_s = text.split(":", 1)
        else:
            h_s, m_s = text, "0"
        h, m = int(h_s), int(m_s)
        if not (1 <= h <= 12) or not (0 <= m <= 59):
            raise ValueError("Times should look like 7:00 or 6:30")
        if pm and h != 12:
            h += 12
        if not pm and h == 12:
            h = 0
        return f"{h:02d}:{m:02d}"

    chosen = sorted({int(d) for d in days if str(d).isdigit() and 1 <= int(d) <= 7}) or [1, 2, 3, 4, 5, 6]
    return {"start": clock(start, False), "end": clock(end, True), "days": chosen}


def parse_followup(form) -> dict:
    """The follow-up schedule fields into settings.followup. Blank fields fall back to the defaults."""
    def nums(key: str, *, kind=float, lo=0, hi=10_000) -> list:
        raw = str(form.get(key) or "").strip()
        if not raw:
            return list(followup.DEFAULTS[key])
        out = []
        for part in re.split(r"[,\s]+", raw):
            if not part:
                continue
            try:
                v = kind(part)
            except ValueError as exc:
                raise ValueError(f"'{part}' in the follow-up schedule isn't a number") from exc
            if not (lo <= v <= hi):
                raise ValueError(f"{part} is out of range for the follow-up schedule")
            out.append(int(v) if v == int(v) else v)
        if not out:
            return list(followup.DEFAULTS[key])
        return sorted(out)

    def num(key: str, *, lo=0, hi=10_000):
        raw = str(form.get(key) or "").strip()
        if not raw:
            return followup.DEFAULTS[key]
        try:
            v = float(raw)
        except ValueError as exc:
            raise ValueError(f"'{raw}' isn't a number") from exc
        if not (lo <= v <= hi):
            raise ValueError(f"{raw} is out of range")
        return int(v) if v == int(v) else v

    digest = str(form.get("digest_time") or "").strip() or followup.DEFAULTS["digest_time"]
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", digest):
        raise ValueError("Digest time should look like 07:30 (24-hour)")
    return {
        "nudge_hours": nums("nudge_hours", hi=72),
        "cadence_days": nums("cadence_days", hi=365),
        "close_loop_days": nums("close_loop_days", hi=365),
        "escalate_after_hours": num("escalate_after_hours", lo=1, hi=720),
        "reply_nudge_hours": num("reply_nudge_hours", lo=0.25, hi=72),
        "ignored_before_escalation": int(num("ignored_before_escalation", lo=1, hi=10)),
        "digest_time": f"{int(digest.split(':')[0]):02d}:{digest.split(':')[1]}",
    }


def parse_ads(form) -> dict:
    """Google Ads and Meta ids for conversion uploads and spend."""
    get = lambda k: str(form.get(k) or "").strip()  # noqa: E731
    customer = re.sub(r"\D", "", get("google_customer_id"))
    if customer and len(customer) != 10:
        raise ValueError("A Google Ads customer id has 10 digits (123-456-7890)")

    def action(key: str) -> str:
        v = get(key)
        if not v:
            return ""
        if re.fullmatch(r"\d+", v) or re.fullmatch(r"customers/\d+/conversionActions/\d+", v):
            return v
        raise ValueError(f"The conversion action should be its numeric id or customers/…/conversionActions/… ({key})")

    try:
        qv = float(get("google_qualified_value") or 0)
    except ValueError as exc:
        raise ValueError("The qualified-lead value should be a number") from exc
    since = get("value_bidding_since")
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ValueError("The value-based bidding date should look like 2026-11-01")
    return {
        "google_ads": {"customer_id": customer, "qualified_action": action("google_qualified_action"), "won_action": action("google_won_action"), "qualified_value": qv},
        "meta": {
            "dataset_id": re.sub(r"\D", "", get("meta_dataset_id")), "ad_account_id": re.sub(r"\D", "", get("meta_ad_account_id")),
            "qualified_event": get("meta_qualified_event") or conversions.META_DEFAULTS["qualified_event"], "won_event": get("meta_won_event") or conversions.META_DEFAULTS["won_event"],
            "test_event_code": get("meta_test_event_code"),
        },
        "conversions": {"enabled": form.get("conversions_enabled") == "on", "value_bidding_since": since},
    }


@router.post("/conversions/{event_id}/retry", dependencies=[Depends(check_csrf)])
def conversion_retry(event_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        ev = conn.execute("SELECT lead_id FROM conversion_event WHERE id = %s", (event_id,)).fetchone()
        if ev is None:
            raise HTTPException(status_code=404, detail="Not found")
        conversions.retry(conn, event_id)
        conversions.process_pending(conn, lead_id=ev["lead_id"])
    return RedirectResponse(f"/leads/{ev['lead_id']}", status_code=303)


def parse_lead_messaging(form) -> dict:
    templates = {}
    for key in ("ack", "decline", "appointment"):
        text = str(form.get(f"tpl_{key}") or "").strip()
        if text and text != lead_messages.TEMPLATES[key]:
            try:
                lead_messages.render(text, {})
            except (KeyError, ValueError, IndexError) as exc:
                raise ValueError(f"The {key} message template has a problem near a {{placeholder}}: {exc}") from exc
            if len(text) > 1000:
                raise ValueError(f"The {key} message is too long for a text (1000 characters max)")
            templates[key] = text
    reply_to = str(form.get("lead_reply_to") or "").strip()
    if reply_to and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", reply_to):
        raise ValueError("The reply-to address for lead emails doesn't look like an email address")
    return {
        "ack": form.get("msg_ack") == "on",
        "ack_meta": form.get("msg_ack_meta") == "on",
        "auto_decline": form.get("msg_auto_decline") == "on",
        "appointment": form.get("msg_appointment") == "on",
        "reply_to": reply_to,
        "templates": templates,
    }


@router.get("/tenants/{tenant_id}")
def tenant_detail(request: Request, tenant_id: UUID, principal: Annotated[Principal, Depends(require_agency)], error: str | None = None, notice: str | None = None):
    return web.render(request, "admin/tenant.html", error=error, notice=notice, new_user_password=None, **_tenant_ctx(tenant_id))


@router.post("/tenants/{tenant_id}", dependencies=[Depends(check_csrf)])
async def tenant_update(request: Request, tenant_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    form = await request.form()
    get = lambda k: str(form.get(k) or "").strip()  # noqa: E731
    try:
        field_map = json.loads(get("field_map") or "{}")
        service_area = json.loads(get("service_area") or "{}")
        if not isinstance(field_map, dict) or not isinstance(service_area, dict):
            raise ValueError("expected JSON objects")
    except ValueError as exc:
        return web.render(request, "admin/tenant.html", status_code=400, error=f"Settings JSON is invalid: {exc}", notice=None, new_user_password=None, **_tenant_ctx(tenant_id))
    try:
        business_hours = parse_hours(get("hours_start"), get("hours_end"), [str(d) for d in form.getlist("hours_days")])
    except ValueError as exc:
        return web.render(request, "admin/tenant.html", status_code=400, error=str(exc), notice=None, new_user_password=None, **_tenant_ctx(tenant_id))
    types = [t.strip() for t in re.split(r"[\n,]", get("job_types")) if t.strip()]
    try:
        followup_cfg = parse_followup(form)
        lead_msg_cfg = parse_lead_messaging(form)
        meta_page_id = re.sub(r"\D", "", get("meta_page_id"))
        call_min = int(get("call_min_seconds") or 30)
        if not (0 <= call_min <= 600):
            raise ValueError("Minimum call length should be between 0 and 600 seconds")
        ads_cfg = parse_ads(form)
    except ValueError as exc:
        return web.render(request, "admin/tenant.html", status_code=400, error=str(exc), notice=None, new_user_password=None, **_tenant_ctx(tenant_id))
    default_assignee = get("default_assignee_id") or None
    twilio = normalize_phone(get("twilio_number")) if get("twilio_number") else None
    with db(None, "agency_admin") as conn:
        conn.execute(
            """
            UPDATE tenant SET name = %s, timezone = %s, field_map = %s, service_area = %s, business_hours = %s,
                              job_types = %s, default_assignee_id = %s, twilio_number = %s, is_active = %s,
                              settings = settings || %s
            WHERE id = %s
            """,
            (
                get("name") or "Client", get("timezone") or "America/Toronto", Jsonb(field_map), Jsonb(service_area),
                Jsonb(business_hours), types,
                UUID(default_assignee) if default_assignee else None, twilio, get("is_active") == "on",
                Jsonb({
                    "followup": followup_cfg, "lead_messaging": lead_msg_cfg, "callrail": {"min_seconds": call_min},
                    "meta": {**ads_cfg["meta"], "page_id": meta_page_id}, "google_ads": ads_cfg["google_ads"], "conversions": ads_cfg["conversions"],
                }), tenant_id,
            ),
        )
        auth_service.audit(conn, action="tenant_updated", user_id=principal.user_id, tenant_id=tenant_id, target_type="tenant", target_id=str(tenant_id))
    return RedirectResponse(f"/admin/tenants/{tenant_id}?notice=Saved", status_code=303)


@router.get("/tenants/{tenant_id}/leads/{lead_id}")
def open_lead(tenant_id: UUID, lead_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    """Click-through from a client's settings page: switch to that client, then show the lead."""
    with db(None, "agency_admin") as conn:
        if conn.execute("SELECT 1 FROM lead WHERE id = %s AND tenant_id = %s", (lead_id, tenant_id)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Lead not found")
    auth_service.set_acting_tenant(principal.session["id"], tenant_id, user_id=principal.user_id, ip=None)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.post("/tenants/{tenant_id}/rotate-key", dependencies=[Depends(check_csrf)])
def rotate_key(tenant_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        conn.execute("UPDATE tenant SET intake_key = encode(gen_random_bytes(24), 'hex') WHERE id = %s", (tenant_id,))
        auth_service.audit(conn, action="intake_key_rotated", user_id=principal.user_id, tenant_id=tenant_id)
    return RedirectResponse(f"/admin/tenants/{tenant_id}?notice=New intake key generated. Update every form that posts here.", status_code=303)


@router.post("/tenants/{tenant_id}/users", dependencies=[Depends(check_csrf)])
def user_create(
    request: Request,
    tenant_id: UUID,
    principal: Annotated[Principal, Depends(require_agency)],
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    role: Annotated[str, Form()] = "member",
    mobile: Annotated[str, Form()] = "",
):
    import secrets

    temp_password = secrets.token_urlsafe(9)
    try:
        r = Role(role)
        if r == Role.AGENCY_ADMIN:
            raise AuthError("Create agency admins from the command line, not here.")
        with db(None, "agency_admin") as conn:
            if conn.execute("SELECT 1 FROM app_user WHERE email_lower = lower(%s)", (email.strip(),)).fetchone():
                raise AuthError("That email already has a login.")
            auth_service.create_user(conn, email=email, name=name, role=r, tenant_id=tenant_id, password=temp_password, mobile_e164=normalize_phone(mobile) if mobile else None)
            auth_service.audit(conn, action="user_created", user_id=principal.user_id, tenant_id=tenant_id, target_type="user", target_id=email.strip())
    except (AuthError, ValueError) as exc:
        return web.render(request, "admin/tenant.html", status_code=400, error=str(exc), notice=None, new_user_password=None, **_tenant_ctx(tenant_id))
    return web.render(request, "admin/tenant.html", error=None, notice=f"Login created for {email.strip()}. Give them this temporary password now; it is not shown again.", new_user_password=temp_password, **_tenant_ctx(tenant_id))


@router.post("/users/{user_id}/reset-mfa", dependencies=[Depends(check_csrf)])
def user_reset_mfa(user_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        user = conn.execute("SELECT tenant_id FROM app_user WHERE id = %s", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        auth_service.reset_totp(conn, user_id, by_user_id=principal.user_id)
    return RedirectResponse(f"/admin/tenants/{user['tenant_id']}?notice=Authenticator cleared. They will set up a new one at next login.", status_code=303)


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(check_csrf)])
def user_reset_password(request: Request, user_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    import secrets

    temp_password = secrets.token_urlsafe(9)
    with db(None, "agency_admin") as conn:
        user = conn.execute("SELECT tenant_id, email FROM app_user WHERE id = %s", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        auth_service.set_password(conn, user_id, temp_password)
        conn.execute("UPDATE app_user SET must_set_password = true, failed_logins = 0, locked_until = NULL WHERE id = %s", (user_id,))
        conn.execute("UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        auth_service.audit(conn, action="password_reset_by_agency", user_id=principal.user_id, tenant_id=user["tenant_id"], target_type="user", target_id=str(user_id))
    return web.render(request, "admin/tenant.html", error=None, notice=f"Temporary password set for {user['email']}. Give it to them now; it is not shown again.", new_user_password=temp_password, **_tenant_ctx(user["tenant_id"]))


@router.post("/users/{user_id}/toggle-active", dependencies=[Depends(check_csrf)])
def user_toggle(user_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        user = conn.execute("UPDATE app_user SET is_active = NOT is_active WHERE id = %s RETURNING tenant_id, is_active", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not user["is_active"]:
            conn.execute("UPDATE session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        auth_service.audit(conn, action="user_active_toggled", user_id=principal.user_id, tenant_id=user["tenant_id"], target_type="user", target_id=str(user_id), detail={"is_active": user["is_active"]})
    return RedirectResponse(f"/admin/tenants/{user['tenant_id']}", status_code=303)


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

def _batch_ctx(tenant_id: UUID, batch_id: UUID, *, mapping: dict | None = None, status_map: dict | None = None, dry: importer.RunResult | None = None) -> dict:
    with db(None, "agency_admin") as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        batch = conn.execute("SELECT * FROM import_batch WHERE id = %s AND tenant_id = %s", (batch_id, tenant_id)).fetchone()
    if tenant is None or batch is None:
        raise HTTPException(status_code=404, detail="Import not found")
    if batch["status"] != "uploaded" or not batch["raw_text"]:
        raise HTTPException(status_code=404, detail="That import has already been finished")
    parsed = importer.parse(batch["raw_text"].encode("utf-8"), batch["filename"])
    mapping = mapping or batch["mapping"] or importer.guess_map(parsed.headers)
    words = importer.distinct_status_words(parsed.rows, mapping)
    status_map = status_map or batch["status_map"] or {w: importer.guess_status(w) for w in words}
    return {
        "tenant": tenant, "batch": batch, "headers": parsed.headers, "sample": parsed.rows[:5], "rows": parsed.rows,
        "mapping": mapping, "targets": importer.TARGETS, "status_words": words, "status_map": status_map,
        "status_targets": [("new", "New"), ("contacted", "Contacted"), ("no_response", "No response"), ("quoted", "Quoted"), ("won", "Won"), ("lost", "Lost"), ("review", "Needs review (Contacted + flag)")],
        "dry": dry, "truncated": parsed.truncated,
    }


@router.get("/tenants/{tenant_id}/import")
def import_form(request: Request, tenant_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        history = conn.execute(
            "SELECT id, filename, row_count, status, result, created_at, committed_at FROM import_batch WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 10",
            (tenant_id,),
        ).fetchall()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return web.render(request, "admin/import.html", tenant=tenant, history=history, error=None)


@router.post("/tenants/{tenant_id}/import", dependencies=[Depends(check_csrf)])
async def import_upload(request: Request, tenant_id: UUID, principal: Annotated[Principal, Depends(require_agency)], file: UploadFile = File(...)):
    data = await file.read()
    try:
        parsed = importer.parse(data, file.filename or "leads.csv")
    except ValueError as exc:
        with db(None, "agency_admin") as conn:
            tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        return web.render(request, "admin/import.html", status_code=400, tenant=tenant, history=[], error=str(exc))
    with db(None, "agency_admin") as conn:
        if conn.execute("SELECT 1 FROM tenant WHERE id = %s", (tenant_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Client not found")
        batch = conn.execute(
            """
            INSERT INTO import_batch (tenant_id, created_by, filename, raw_text, headers, row_count, mapping)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (tenant_id, principal.user_id, (file.filename or "leads.csv")[:200], parsed.text, parsed.headers, len(parsed.rows), Jsonb(importer.guess_map(parsed.headers))),
        ).fetchone()
    return RedirectResponse(f"/admin/tenants/{tenant_id}/import/{batch['id']}", status_code=303)


@router.get("/tenants/{tenant_id}/import/{batch_id}")
def import_map(request: Request, tenant_id: UUID, batch_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    ctx = _batch_ctx(tenant_id, batch_id)
    with db(None, "agency_admin") as conn:
        ctx["dry"] = importer.run(conn, ctx["tenant"], ctx["rows"], ctx["mapping"], ctx["status_map"], batch_id=batch_id, actor_id=principal.user_id, actor_label=principal.label, commit=False)
    return web.render(request, "admin/import_map.html", **ctx)


@router.post("/tenants/{tenant_id}/import/{batch_id}", dependencies=[Depends(check_csrf)])
async def import_apply(request: Request, tenant_id: UUID, batch_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    form = await request.form()
    action = str(form.get("action") or "preview")
    if action == "discard":
        with db(None, "agency_admin") as conn:
            conn.execute("UPDATE import_batch SET status = 'discarded', raw_text = NULL WHERE id = %s AND tenant_id = %s", (batch_id, tenant_id))
        return RedirectResponse(f"/admin/tenants/{tenant_id}/import", status_code=303)
    ctx = _batch_ctx(tenant_id, batch_id)
    mapping = {h: (str(form.get(f"map::{h}") or "skip")) for h in ctx["headers"]}
    mapping = {h: (t if t in importer.TARGET_KEYS else "skip") for h, t in mapping.items()}
    words = importer.distinct_status_words(ctx["rows"], mapping)
    status_map = {w: str(form.get(f"status::{w}") or importer.guess_status(w)) for w in words}
    with db(None, "agency_admin") as conn:
        conn.execute("UPDATE import_batch SET mapping = %s, status_map = %s WHERE id = %s", (Jsonb(mapping), Jsonb(status_map), batch_id))
        if action == "commit":
            result = importer.run(conn, ctx["tenant"], ctx["rows"], mapping, status_map, batch_id=batch_id, actor_id=principal.user_id, actor_label=principal.label, commit=True)
            conn.execute(
                "UPDATE import_batch SET status = 'committed', committed_at = now(), raw_text = NULL, result = %s WHERE id = %s",
                (Jsonb(result.as_dict()), batch_id),
            )
            auth_service.audit(conn, action="leads_imported", user_id=principal.user_id, tenant_id=tenant_id, target_type="import_batch", target_id=str(batch_id), detail=result.as_dict())
            notice = f"Imported {result.created} leads ({result.merged} merged into existing leads, {result.rejected} skipped)."
            return RedirectResponse(f"/admin/tenants/{tenant_id}?notice={notice.replace(' ', '+')}", status_code=303)
        dry = importer.run(conn, ctx["tenant"], ctx["rows"], mapping, status_map, batch_id=batch_id, actor_id=principal.user_id, actor_label=principal.label, commit=False)
    ctx = _batch_ctx(tenant_id, batch_id, mapping=mapping, status_map=status_map, dry=dry)
    return web.render(request, "admin/import_map.html", **ctx)
