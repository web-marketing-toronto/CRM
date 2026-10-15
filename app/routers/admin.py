"""Agency-only screens: clients (tenants), their settings, users, and intake instructions."""

from __future__ import annotations

import json
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb

from app import web
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_agency
from app.auth.service import AuthError, Principal
from app.config import get_settings
from app.db import db
from app.models import Role
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
    settings = get_settings()
    return {
        "tenant": tenant,
        "users": users,
        "recent": recent,
        "intake_url": f"{settings.base_url}/v1/intake/{tenant['slug']}/{tenant['intake_key']}",
        "field_map_json": json.dumps(tenant["field_map"], indent=2),
        "service_area_json": json.dumps(tenant["service_area"], indent=2),
        "business_hours_json": json.dumps(tenant["business_hours"]),
        "job_types_text": "\n".join(tenant["job_types"] or []),
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
        business_hours = json.loads(get("business_hours") or "{}")
        if not isinstance(field_map, dict) or not isinstance(service_area, dict) or not isinstance(business_hours, dict):
            raise ValueError("expected JSON objects")
    except ValueError as exc:
        return web.render(request, "admin/tenant.html", status_code=400, error=f"Settings JSON is invalid: {exc}", notice=None, new_user_password=None, **_tenant_ctx(tenant_id))
    types = [t.strip() for t in re.split(r"[\n,]", get("job_types")) if t.strip()]
    default_assignee = get("default_assignee_id") or None
    twilio = normalize_phone(get("twilio_number")) if get("twilio_number") else None
    with db(None, "agency_admin") as conn:
        conn.execute(
            """
            UPDATE tenant SET name = %s, timezone = %s, field_map = %s, service_area = %s, business_hours = %s,
                              job_types = %s, default_assignee_id = %s, twilio_number = %s, is_active = %s
            WHERE id = %s
            """,
            (
                get("name") or "Client", get("timezone") or "America/Toronto", Jsonb(field_map), Jsonb(service_area),
                Jsonb(business_hours or {"start": "07:00", "end": "19:00", "days": [1, 2, 3, 4, 5, 6]}), types,
                UUID(default_assignee) if default_assignee else None, twilio, get("is_active") == "on", tenant_id,
            ),
        )
        auth_service.audit(conn, action="tenant_updated", user_id=principal.user_id, tenant_id=tenant_id, target_type="tenant", target_id=str(tenant_id))
    return RedirectResponse(f"/admin/tenants/{tenant_id}?notice=Saved", status_code=303)


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
