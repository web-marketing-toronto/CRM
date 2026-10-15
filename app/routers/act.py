"""One-tap action links in reminder emails: /act/{token}.

The token is signed and names the lead, the user and the action, so no login is needed on a
phone. A GET shows a one-line confirmation (mail scanners follow links; a GET must never change
anything) and the POST applies it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request

from app import web
from app.auth import crypto
from app.db import db, system_db
from app.models import LostReason
from app.services import followup
from app.services import leads as lead_svc

router = APIRouter(prefix="/act", tags=["act"])

TOKEN_MAX_AGE = 72 * 3600

LABELS = {
    "call": "Log a call to",
    "text": "Log a text to",
    "email": "Log an email to",
    "snooze1": "Snooze for a day:",
    "snooze3": "Snooze for three days:",
    "won": "Mark as Won:",
    "lost": "Mark as Lost:",
    "keep": "Keep following up with",
    "keep_area": "Cancel the out-of-area decline and keep",
    "decline_now": "Decline as out of area and let them know:",
}


def _unpack(token: str) -> tuple[dict, dict, str]:
    data = crypto.read_signed_token(token, salt="act", max_age_seconds=TOKEN_MAX_AGE)
    if not data or data.get("a") not in LABELS:
        raise HTTPException(status_code=404, detail="This link has expired. Open the lead in the app instead.")
    with system_db() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE id = %s AND is_active", (UUID(data["u"]),)).fetchone()
        lead = conn.execute("SELECT * FROM lead WHERE id = %s", (UUID(data["l"]),)).fetchone() if user else None
    if user is None or lead is None or (user["tenant_id"] is not None and lead["tenant_id"] != user["tenant_id"]):
        raise HTTPException(status_code=404, detail="This link has expired. Open the lead in the app instead.")
    return user, lead, data["a"]


@router.get("/{token}")
def confirm(request: Request, token: str):
    user, lead, action = _unpack(token)
    return web.render(request, "act/confirm.html", lead=lead, user=user, action=action, label=LABELS[action], token=token, error=None)


@router.post("/{token}")
def apply(request: Request, token: str, deal_value: Annotated[str, Form()] = "", lost_reason: Annotated[str, Form()] = ""):
    user, lead, action = _unpack(token)
    value: Decimal | None = None
    reason: LostReason | None = None
    if action == "won":
        try:
            value = Decimal(deal_value.replace("$", "").replace(",", "").strip())
        except InvalidOperation:
            return web.render(request, "act/confirm.html", status_code=400, lead=lead, user=user, action=action, label=LABELS[action], token=token, error="Enter the job value as a number, like 8500")
    if action == "lost":
        try:
            reason = LostReason(lost_reason)
        except ValueError:
            return web.render(request, "act/confirm.html", status_code=400, lead=lead, user=user, action=action, label=LABELS[action], token=token, error="Pick a reason")
    with db(lead["tenant_id"], user["role"]) as conn:
        fresh = lead_svc.get_lead(conn, lead["id"])
        try:
            message = followup.apply_action(conn, user, fresh, action, deal_value=value, lost_reason=reason)
        except lead_svc.LeadError as exc:
            return web.render(request, "act/confirm.html", status_code=400, lead=lead, user=user, action=action, label=LABELS[action], token=token, error=str(exc))
    return web.render(request, "act/done.html", lead=lead, message=message)
