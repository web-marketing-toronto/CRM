"""A user's own settings: notification preferences, password, authenticator."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import web
from app.auth import crypto
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_user
from app.auth.service import AuthError, Principal
from app.db import db
from app.services.normalize import normalize_phone

router = APIRouter(prefix="/account", tags=["account"])


@router.get("")
def account(request: Request, principal: Annotated[Principal, Depends(require_user)], notice: str | None = None, error: str | None = None):
    return web.render(request, "account.html", notice=notice, error=error)


@router.post("/notifications", dependencies=[Depends(check_csrf)])
def notifications(
    principal: Annotated[Principal, Depends(require_user)],
    mobile: Annotated[str, Form()] = "",
    notify_sms: Annotated[str, Form()] = "",
    notify_email: Annotated[str, Form()] = "",
):
    mobile_e164 = normalize_phone(mobile) if mobile.strip() else None
    if mobile.strip() and not mobile_e164:
        return RedirectResponse("/account?error=That+mobile+number+doesn't+look+right", status_code=303)
    with db(None, "system") as conn:
        conn.execute(
            "UPDATE app_user SET mobile_e164 = %s, notify_sms = %s, notify_email = %s WHERE id = %s",
            (mobile_e164, notify_sms == "on" and mobile_e164 is not None, notify_email == "on", principal.user_id),
        )
    return RedirectResponse("/account?notice=Saved", status_code=303)


@router.post("/password", dependencies=[Depends(check_csrf)])
def password(
    principal: Annotated[Principal, Depends(require_user)],
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password2: Annotated[str, Form()],
):
    if not crypto.verify_password(principal.user["password_hash"], current):
        return RedirectResponse("/account?error=Current+password+is+wrong", status_code=303)
    if password != password2:
        return RedirectResponse("/account?error=The+two+new+passwords+don't+match", status_code=303)
    try:
        with db(None, "system") as conn:
            auth_service.set_password(conn, principal.user_id, password)
            conn.execute("UPDATE session SET revoked_at = now() WHERE user_id = %s AND id <> %s AND revoked_at IS NULL", (principal.user_id, principal.session["id"]))
            auth_service.audit(conn, action="password_changed", user_id=principal.user_id, tenant_id=principal.user["tenant_id"])
    except AuthError as exc:
        return RedirectResponse(f"/account?error={str(exc).replace(' ', '+')}", status_code=303)
    return RedirectResponse("/account?notice=Password+changed.+Other+devices+were+signed+out.", status_code=303)
