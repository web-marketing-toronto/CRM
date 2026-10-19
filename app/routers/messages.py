"""Client-editable wording: what the Text button pre-fills. Owners (and the agency viewing as them) edit
it here; it lives in the same tenant.settings.lead_messaging.templates the agency settings page uses."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb

from app import web
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_owner
from app.auth.service import Principal
from app.db import db
from app.services import lead_messages

router = APIRouter(prefix="/messages", tags=["messages"])

MAX_LEN = 1000

# The same fill-ins the agency page lists, described for the business owner rather than for us.
CLIENT_PLACEHOLDERS = {
    "first_name": "the lead's first name",
    "name": "their full name",
    "your_name": "your name (whoever taps Text)",
    "your_first_name": "your first name",
    "client_name": "your business name",
    "job": "'your driveway paving', or 'the job' when the form didn't say",
    "job_type": "just the work type, if known",
    "town": "the town from the job address, if known",
}


def _ctx(conn, principal: Principal, *, draft: str | None = None) -> dict:
    tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
    cfg = lead_messages.settings_for(tenant)
    current = cfg["templates"]["intro"]
    template = draft if draft is not None else current
    # Preview on the newest real lead so the owner sees their own job types and names; a sample otherwise.
    lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND contact_name IS NOT NULL ORDER BY created_at DESC LIMIT 1", (tenant["id"],)).fetchone()
    sample = lead or {"contact_name": "Karen Whitfield", "job_type": (tenant.get("job_types") or ["the job"])[0], "job_address": "", "assigned_to": None}
    values = lead_messages.context(conn, tenant, sample, user=principal.user)
    try:
        preview = lead_messages.render(template, values)
        preview_error = None
    except (KeyError, ValueError, IndexError) as exc:
        preview, preview_error = "", f"There's a problem near a {{placeholder}}: {exc}"
    return {
        "tenant": tenant,
        "template": template,
        "is_default": current == lead_messages.TEMPLATES["intro"],
        "default_template": lead_messages.TEMPLATES["intro"],
        "preview": preview,
        "preview_error": preview_error,
        "sample_values": values,
        "preview_name": (sample.get("contact_name") or "the lead"),
        "placeholders": CLIENT_PLACEHOLDERS,
        "max_len": MAX_LEN,
    }


@router.get("")
def edit(request: Request, principal: Annotated[Principal, Depends(require_owner)], notice: str | None = None):
    with db(principal.tenant_id, principal.db_role) as conn:
        ctx = _ctx(conn, principal)
    return web.render(request, "messages.html", notice=notice, error=None, **ctx)


@router.post("", dependencies=[Depends(check_csrf)])
def save(
    request: Request,
    principal: Annotated[Principal, Depends(require_owner)],
    template: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
):
    text = template.strip()
    if action == "reset":
        text = ""
    with db(principal.tenant_id, principal.db_role) as conn:
        if text:
            if len(text) > MAX_LEN:
                ctx = _ctx(conn, principal, draft=text)
                return web.render(request, "messages.html", status_code=400, notice=None, error=f"That's too long for a text ({len(text)} characters; {MAX_LEN} max).", **ctx)
            try:
                lead_messages.render(text, {})
            except (KeyError, ValueError, IndexError) as exc:
                ctx = _ctx(conn, principal, draft=text)
                return web.render(request, "messages.html", status_code=400, notice=None, error=f"There's a problem near a {{placeholder}}: {exc}. Placeholders are the words in curly braces listed below.", **ctx)
        tenant = conn.execute("SELECT settings FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        lm = dict((tenant["settings"] or {}).get("lead_messaging") or {})
        templates = dict(lm.get("templates") or {})
        if text and text != lead_messages.TEMPLATES["intro"]:
            templates["intro"] = text
        else:
            templates.pop("intro", None)
        lm["templates"] = templates
        conn.execute("UPDATE tenant SET settings = settings || %s WHERE id = %s", (Jsonb({"lead_messaging": lm}), principal.tenant_id))
        auth_service.audit(conn, action="first_text_updated", user_id=principal.user_id, tenant_id=principal.tenant_id, target_type="tenant", target_id=str(principal.tenant_id), detail={"reset": not text})
    notice = "Back to the standard wording." if not text else "Saved. The Text button uses this from now on."
    return RedirectResponse(f"/messages?notice={notice.replace(' ', '+')}", status_code=303)
