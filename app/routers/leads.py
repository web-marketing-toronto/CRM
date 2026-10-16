"""Client-facing screens: the lead list, a lead, and the actions on it."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import web
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_agency, require_tenant, require_user
from app.auth.service import Principal
from app.config import get_settings
from app.db import db
from app.models import ActivityType, LeadStatus, LostReason, Platform
from app.services import followup, lead_messages
from app.services import leads as svc

router = APIRouter(tags=["leads"])

VIEWS = ("needs_action", "open", "all", "new", "contacted", "no_response", "quoted", "won", "lost", "mine", "imported")

NEEDS_ACTION_SQL = """
    (
        NOT (l.flags ? 'needs_review')
        AND (
        l.status = 'new'
        OR (l.status IN ('contacted', 'no_response') AND COALESCE(l.last_touch_at, l.created_at) < now() - interval '1 day')
        OR (l.status = 'quoted' AND COALESCE(l.quoted_at, l.created_at) < now() - interval '7 days')
        OR (l.next_action_at IS NOT NULL AND l.next_action_at <= now())
        )
    )
"""


@router.get("/")
def home(principal: Annotated[Principal, Depends(require_user)]):
    if principal.is_agency and principal.tenant is None:
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/leads", status_code=303)


@router.get("/leads")
def lead_list(request: Request, principal: Annotated[Principal, Depends(require_tenant)], view: str = "needs_action", q: str = ""):
    if view not in VIEWS:
        view = "needs_action"
    where = ["l.tenant_id = %s"]
    params: list = [principal.tenant_id]
    if view == "needs_action":
        where.append(NEEDS_ACTION_SQL)
        where.append("l.status NOT IN ('won', 'lost')")
    elif view == "open":
        where.append("l.status NOT IN ('won', 'lost')")
    elif view == "mine":
        where.append("l.assigned_to = %s")
        params.append(principal.user_id)
    elif view == "imported":
        where.append("l.flags ? 'needs_review'")
    elif view != "all":
        where.append("l.status = %s")
        params.append(view)
    if not principal.user.get("see_all_leads", True) and not principal.is_agency:
        where.append("l.assigned_to = %s")
        params.append(principal.user_id)
    q = q.strip()
    if q:
        where.append(
            "(l.contact_name ILIKE %s OR l.company ILIKE %s OR l.email ILIKE %s OR l.phone_e164 ILIKE %s OR l.job_address ILIKE %s OR l.ref_code ILIKE %s)"
        )
        like = f"%{q}%"
        params += [like, like, like, like, like, like]

    order = "l.created_at ASC" if view == "needs_action" else "l.created_at DESC"
    sql = f"""
        SELECT l.*, u.name AS assignee_name,
               {NEEDS_ACTION_SQL} AS needs_action
        FROM lead l LEFT JOIN app_user u ON u.id = l.assigned_to
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT 300
    """
    with db(principal.tenant_id, principal.db_role) as conn:
        rows = conn.execute(sql, params).fetchall()
        counts = conn.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE {NEEDS_ACTION_SQL} AND l.status NOT IN ('won','lost')) AS needs_action,
              count(*) FILTER (WHERE l.status NOT IN ('won','lost')) AS open,
              count(*) FILTER (WHERE l.flags ? 'needs_review') AS imported,
              count(*) FILTER (WHERE l.status = 'won' AND l.won_at > date_trunc('month', now())) AS won_month,
              coalesce(sum(l.deal_value) FILTER (WHERE l.status = 'won' AND l.won_at > date_trunc('month', now())), 0) AS won_value_month
            FROM lead l WHERE l.tenant_id = %s
            """,
            (principal.tenant_id,),
        ).fetchone()
    return web.render(request, "leads/list.html", leads=rows, view=view, q=q, counts=counts)


def _load(principal: Principal, lead_id: UUID) -> dict:
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def _detail_context(principal: Principal, lead_id: UUID) -> dict:
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        activity = conn.execute(
            "SELECT * FROM activity WHERE lead_id = %s ORDER BY occurred_at DESC, id DESC LIMIT 200", (lead_id,)
        ).fetchall()
        source = conn.execute("SELECT * FROM lead_source WHERE lead_id = %s ORDER BY created_at ASC LIMIT 1", (lead_id,)).fetchone()
        users = conn.execute(
            "SELECT id, name FROM app_user WHERE tenant_id = %s AND is_active ORDER BY name", (principal.tenant_id,)
        ).fetchall()
        tenant = conn.execute("SELECT job_types FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        next_reminder = conn.execute(
            "SELECT rule, due_at FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL AND rule NOT IN ('escalation', 'ack', 'decline', 'appointment') ORDER BY due_at LIMIT 1",
            (lead_id,),
        ).fetchone()
        full_tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        sync = conn.execute("SELECT * FROM conversion_event WHERE lead_id = %s ORDER BY created_at", (lead_id,)).fetchall() if principal.is_agency else []
    return {
        "sync": sync,
        "lead": lead, "activity": activity, "source": source, "users": users, "job_types": (tenant or {}).get("job_types") or [],
        "next_reminder": next_reminder, "now": datetime.now(UTC),
        "can_text": bool(full_tenant and full_tenant.get("twilio_number")), "texting_number": (full_tenant or {}).get("twilio_number"),
        "relay": bool(full_tenant and followup.relay_enabled(full_tenant)),
        "can_call": bool(full_tenant and full_tenant.get("twilio_number") and principal.user.get("mobile_e164")),
    }


@router.get("/leads/new")
def new_lead_form(request: Request, principal: Annotated[Principal, Depends(require_tenant)]):
    with db(principal.tenant_id, principal.db_role) as conn:
        tenant = conn.execute("SELECT job_types FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
    return web.render(request, "leads/new.html", error=None, job_types=(tenant or {}).get("job_types") or [], form={})


@router.post("/leads/new", dependencies=[Depends(check_csrf)])
async def new_lead_submit(request: Request, principal: Annotated[Principal, Depends(require_tenant)]):
    form = await request.form()
    get = lambda k: (str(form.get(k) or "").strip() or None)  # noqa: E731
    try:
        platform = Platform(get("platform") or "unknown")
    except ValueError:
        platform = Platform.UNKNOWN
    with db(principal.tenant_id, principal.db_role) as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        try:
            result = svc.create_manual(
                conn, tenant,
                contact_name=get("contact_name"), phone_raw=get("phone"), email=get("email"), company=get("company"),
                contact_address=get("contact_address"), job_address=get("job_address"), job_type=get("job_type"),
                message=get("message"), platform=platform, actor_id=principal.user_id, actor_label=principal.label,
                maps_api_key=get_settings().google_maps_api_key,
            )
        except svc.LeadError as exc:
            return web.render(request, "leads/new.html", status_code=400, error=str(exc), job_types=tenant.get("job_types") or [], form=dict(form))
    return RedirectResponse(f"/leads/{result.lead_id}", status_code=303)


@router.get("/leads/{lead_id}")
def lead_detail(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)]):
    return web.render(request, "leads/detail.html", **_detail_context(principal, lead_id))


def _respond(request: Request, principal: Principal, lead_id: UUID, error: str | None = None):
    if request.headers.get("hx-request"):
        ctx = _detail_context(principal, lead_id)
        return web.render(request, "leads/_lead.html", status_code=400 if error else 200, error=error, **ctx)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.post("/leads/{lead_id}/touch", dependencies=[Depends(check_csrf)])
def touch(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], kind: Annotated[str, Form(alias="type")], note: Annotated[str, Form()] = ""):
    kinds = {"call": ActivityType.CALL_ATTEMPT, "text": ActivityType.TEXT_SENT, "email": ActivityType.EMAIL_SENT}
    if kind not in kinds:
        raise HTTPException(status_code=400, detail="unknown touch type")
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        svc.log_touch(conn, lead, kinds[kind], actor_id=principal.user_id, actor_label=principal.label, note=note.strip() or None)
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/status", dependencies=[Depends(check_csrf)])
def status(
    request: Request,
    lead_id: UUID,
    principal: Annotated[Principal, Depends(require_tenant)],
    status: Annotated[str, Form()],
    deal_value: Annotated[str, Form()] = "",
    lost_reason: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    send_decline: Annotated[str, Form()] = "",
):
    try:
        new_status = LeadStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unknown status") from exc
    value: Decimal | None = None
    if deal_value.strip():
        try:
            value = Decimal(deal_value.replace("$", "").replace(",", "").strip())
        except InvalidOperation:
            return _respond(request, principal, lead_id, error="Deal value should be a number, like 8500")
        if value < 0:
            return _respond(request, principal, lead_id, error="Deal value can't be negative")
    reason: LostReason | None = None
    if lost_reason.strip():
        try:
            reason = LostReason(lost_reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="unknown reason") from exc
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        try:
            if new_status == LeadStatus.LOST and reason == LostReason.OUT_OF_AREA and send_decline == "on":
                tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
                lead_messages.decline_now(conn, tenant, lead, actor_id=principal.user_id, actor_label=principal.label)
            else:
                svc.change_status(
                    conn, lead, new_status, actor_id=principal.user_id, actor_label=principal.label,
                    deal_value=value, lost_reason=reason, note=note.strip() or None,
                )
        except svc.LeadError as exc:
            return _respond(request, principal, lead_id, error=str(exc))
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/call", dependencies=[Depends(check_csrf)])
def call_lead(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)]):
    """Click-to-call through the client's number: the user's phone rings, then the lead is dialled."""
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        try:
            lead_messages.start_call(conn, tenant, lead, principal.user, actor_label=principal.label)
        except svc.LeadError as exc:
            return _respond(request, principal, lead_id, error=str(exc))
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/text", dependencies=[Depends(check_csrf)])
def text_lead(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], body: Annotated[str, Form()] = ""):
    """A text typed in the app, sent from the client's number so the thread stays in one place."""
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        try:
            lead_messages.send_text(conn, tenant, lead, body, actor_id=principal.user_id, actor_label=principal.label)
        except svc.LeadError as exc:
            return _respond(request, principal, lead_id, error=str(exc))
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/appointment", dependencies=[Depends(check_csrf)])
def appointment(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], when: Annotated[str, Form()] = ""):
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        at: datetime | None = None
        if when.strip():
            try:
                at = datetime.fromisoformat(when.strip()).replace(tzinfo=followup.tz_for(tenant))
            except ValueError:
                return _respond(request, principal, lead_id, error="Pick a date and time for the appointment")
        lead_messages.set_appointment(conn, tenant, lead, at, actor_id=principal.user_id, actor_label=principal.label)
    background_dispatch(lead_id)
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/area", dependencies=[Depends(check_csrf)])
def area_decision(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], decision: Annotated[str, Form()]):
    """One tap on a flagged lead: decline (message + Lost) or keep (clear the flag, acknowledge)."""
    if decision not in ("decline", "keep"):
        raise HTTPException(status_code=400, detail="unknown decision")
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        if decision == "decline":
            lead_messages.decline_now(conn, tenant, lead, actor_id=principal.user_id, actor_label=principal.label)
        else:
            lead_messages.cancel_auto_decline(conn, tenant, lead, actor_id=principal.user_id, actor_label=principal.label)
    background_dispatch(lead_id)
    return _respond(request, principal, lead_id)


def background_dispatch(lead_id: UUID) -> None:
    """Send what the action just made due (an acknowledgement, an appointment confirmation)."""
    from app.services import worker

    worker.dispatch_lead_now(lead_id)


@router.post("/leads/{lead_id}/snooze", dependencies=[Depends(check_csrf)])
def snooze(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], days: Annotated[str, Form()] = "1"):
    n = 3 if days.strip() == "3" else 1
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        until = followup.on_snooze(conn, tenant, lead, n)
        svc.log_activity(conn, lead["tenant_id"], lead_id, ActivityType.NOTE, actor_id=principal.user_id, actor_label=principal.label, body=f"Snoozed {n} day{'s' if n > 1 else ''}, until {until.astimezone(followup.tz_for(tenant)).strftime('%a %b %-d, %-I:%M %p')}")
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/note", dependencies=[Depends(check_csrf)])
def note(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], body: Annotated[str, Form()] = ""):
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        try:
            svc.add_note(conn, lead, body, actor_id=principal.user_id, actor_label=principal.label)
        except svc.LeadError as exc:
            return _respond(request, principal, lead_id, error=str(exc))
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/assign", dependencies=[Depends(check_csrf)])
def assign(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)], user_id: Annotated[str, Form()] = ""):
    target = UUID(user_id) if user_id else None
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        if target is not None and conn.execute("SELECT 1 FROM app_user WHERE id = %s AND tenant_id = %s", (target, principal.tenant_id)).fetchone() is None:
            raise HTTPException(status_code=400, detail="That person isn't on this account")
        svc.assign(conn, lead, target, actor_id=principal.user_id, actor_label=principal.label)
    return _respond(request, principal, lead_id)


@router.post("/leads/{lead_id}/edit", dependencies=[Depends(check_csrf)])
async def edit(request: Request, lead_id: UUID, principal: Annotated[Principal, Depends(require_tenant)]):
    form = await request.form()
    get = lambda k: (str(form.get(k) or "").strip() or None)  # noqa: E731
    from app.services.normalize import normalize_email, normalize_phone

    phone_raw = get("phone")
    with db(principal.tenant_id, principal.db_role) as conn:
        lead = svc.get_lead(conn, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        conn.execute(
            """
            UPDATE lead SET contact_name = %s, company = %s, phone_raw = %s, phone_e164 = %s, email = %s,
                            contact_address = %s, job_address = %s, job_type = %s
            WHERE id = %s
            """,
            (
                get("contact_name"), get("company"), phone_raw, normalize_phone(phone_raw) if phone_raw else None,
                normalize_email(get("email")) or get("email"), get("contact_address"), get("job_address"), get("job_type"), lead_id,
            ),
        )
        svc.log_activity(conn, lead["tenant_id"], lead_id, ActivityType.NOTE, actor_id=principal.user_id, actor_label=principal.label, body="Updated contact details")
    return _respond(request, principal, lead_id)



@router.post("/leads/{lead_id}/delete", dependencies=[Depends(check_csrf)])
def delete_lead(lead_id: UUID, principal: Annotated[Principal, Depends(require_agency)]):
    """Agency only: remove a lead and everything logged on it (tests, spam, duplicates). Clients mark Lost instead."""
    with db(None, "agency_admin") as conn:
        lead = conn.execute("SELECT id, tenant_id, ref_code, contact_name, phone_e164, email FROM lead WHERE id = %s", (lead_id,)).fetchone()
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        conn.execute("DELETE FROM lead WHERE id = %s", (lead_id,))
        auth_service.audit(
            conn, action="lead_deleted", user_id=principal.user_id, tenant_id=lead["tenant_id"], target_type="lead",
            target_id=str(lead_id), detail={"ref": lead["ref_code"], "name": lead["contact_name"], "phone": lead["phone_e164"], "email": lead["email"]},
        )
    return RedirectResponse("/leads?notice=Lead+deleted", status_code=303)
