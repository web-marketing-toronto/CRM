"""A user's own settings: notification preferences, password, authenticator."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import web
from app.auth import crypto
from app.auth import service as auth_service
from app.auth.deps import check_csrf, require_user
from app.auth.service import AuthError, Principal
from app.config import get_settings
from app.db import db
from app.services import messaging
from app.services.normalize import format_phone_display, normalize_phone

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


EMAIL_CLIENTS = ("ask", "here", "mailto", "gmail", "outlook")


@router.post("/email-client", dependencies=[Depends(check_csrf)])
def email_client(principal: Annotated[Principal, Depends(require_user)], email_client: Annotated[str, Form()] = "ask", next: Annotated[str, Form()] = ""):
    """Which program the Email button on a lead opens."""
    choice = email_client if email_client in EMAIL_CLIENTS else "ask"
    with db(None, "system") as conn:
        conn.execute("UPDATE app_user SET email_client = %s WHERE id = %s", (choice, principal.user_id))
    if next.startswith("/") and not next.startswith("//"):
        return RedirectResponse(next, status_code=303)
    return RedirectResponse("/account?notice=Saved", status_code=303)


@router.post("/test-text", dependencies=[Depends(check_csrf)])
def test_text(principal: Annotated[Principal, Depends(require_user)]):
    """Prove the texting pipe end to end: a real text to this user's saved mobile, from the agency number."""
    mobile = principal.user.get("mobile_e164")
    if not mobile:
        return RedirectResponse("/account?error=Save+a+mobile+number+first,+then+send+the+test.", status_code=303)
    s = get_settings()
    body = f"{s.agency_name} CRM: texts are working. Reply ? to see the reply codes."
    with db(None, "system") as conn:
        r = messaging.send_sms(conn, to=mobile, body=body, kind="test", tenant_id=principal.user["tenant_id"], user_id=principal.user_id)
    if not r.ok:
        return RedirectResponse(f"/account?error={quote('Could not send: ' + (r.error or 'no error from the provider'))}", status_code=303)
    if s.sms_provider != "twilio":
        return RedirectResponse(f"/account?notice={quote(f'Logged, but SMS_PROVIDER is {s.sms_provider} on this server, so nothing was really sent.')}", status_code=303)
    return RedirectResponse(f"/account?notice={quote(f'Test text sent to {format_phone_display(mobile)}. Reply ? to it to check the inbound side too.')}", status_code=303)


@router.post("/test-email", dependencies=[Depends(check_csrf)])
def test_email(principal: Annotated[Principal, Depends(require_user)]):
    s = get_settings()
    text = f"This is a test from {s.agency_name} CRM. If you are reading it, lead alerts and reminders will reach this inbox.\n\n{s.base_url}"
    with db(None, "system") as conn:
        r = messaging.send_email(conn, to=principal.user["email"], subject=f"{s.agency_name} CRM: test email", text=text, html=None, kind="test", tenant_id=principal.user["tenant_id"], user_id=principal.user_id)
    if not r.ok:
        return RedirectResponse(f"/account?error={quote('Could not send: ' + (r.error or 'no error from the provider'))}", status_code=303)
    if s.email_provider not in ("smtp", "postmark", "sendgrid"):
        return RedirectResponse(f"/account?notice={quote(f'Logged, but EMAIL_PROVIDER is {s.email_provider} on this server, so nothing was really sent.')}", status_code=303)
    return RedirectResponse(f"/account?notice={quote('Test email sent to ' + principal.user['email'] + '. Check spam if it is not there within a minute.')}", status_code=303)


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
