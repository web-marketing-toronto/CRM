"""Login, two-factor, forced password set, logout, and the agency's client switcher."""

from __future__ import annotations

import io
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import web
from app.auth import service
from app.auth.deps import check_csrf, get_principal, require_password_only, require_user
from app.auth.service import AuthError, Principal
from app.config import get_settings
from app.db import db

router = APIRouter(tags=["auth"])


def _safe_next(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _set_cookie(response: Response, value: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.cookie_name, value, max_age=s.session_ttl_hours * 3600, httponly=True, secure=s.cookie_secure,
        samesite="lax", path="/",
    )


def _next_step(principal: Principal, next_url: str) -> str:
    if principal.needs_mfa_challenge:
        return "/login/mfa"
    if principal.needs_mfa_enrolment:
        return "/login/setup-authenticator"
    if principal.user["must_set_password"]:
        return "/login/set-password"
    return next_url


@router.get("/login")
def login_form(request: Request, next: str | None = None, principal: Annotated[Principal | None, Depends(get_principal)] = None):
    if principal is not None:
        return RedirectResponse(_next_step(principal, _safe_next(next)), status_code=303)
    return web.render(request, "auth/login.html", next=_safe_next(next), error=None, email="")


@router.post("/login")
def login_submit(request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    try:
        user, cookie = service.login(email, password, request)
    except AuthError as exc:
        return web.render(request, "auth/login.html", status_code=401, next=_safe_next(next), error=str(exc), email=email)
    request.state.principal = None
    principal = service.load_principal(cookie)
    target = _next_step(principal, _safe_next(next)) if principal else "/"
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, cookie)
    return response


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, principal: Annotated[Principal | None, Depends(get_principal)]):
    if principal is not None:
        service.logout(principal.session["id"])
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().cookie_name, path="/")
    return response


# --- MFA challenge -----------------------------------------------------------------------

@router.get("/login/mfa")
def mfa_form(request: Request, principal: Annotated[Principal, Depends(require_password_only)]):
    if not principal.needs_mfa_challenge:
        return RedirectResponse(_next_step(principal, "/"), status_code=303)
    return web.render(request, "auth/mfa.html", error=None)


@router.post("/login/mfa", dependencies=[Depends(check_csrf)])
def mfa_submit(request: Request, principal: Annotated[Principal, Depends(require_password_only)], code: Annotated[str, Form()]):
    try:
        service.verify_mfa(principal.user_id, principal.session["id"], code)
    except AuthError as exc:
        return web.render(request, "auth/mfa.html", status_code=401, error=str(exc))
    request.state.principal = None
    return RedirectResponse(_next_step(service.load_principal(request.cookies.get(get_settings().cookie_name)) or principal, "/"), status_code=303)


# --- MFA enrolment --------------------------------------------------------------------------

@router.get("/login/setup-authenticator")
def enrol_form(request: Request, principal: Annotated[Principal, Depends(require_password_only)]):
    if principal.user["totp_enabled"] and not request.query_params.get("again"):
        return RedirectResponse(_next_step(principal, "/"), status_code=303)
    secret, uri = service.start_totp_enrolment(principal.user_id)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return web.render(request, "auth/enrol.html", secret=secret, qr_svg=svg, error=None, required=principal.mfa_required)


@router.post("/login/setup-authenticator", dependencies=[Depends(check_csrf)])
def enrol_submit(request: Request, principal: Annotated[Principal, Depends(require_password_only)], code: Annotated[str, Form()]):
    try:
        codes = service.confirm_totp_enrolment(principal.user_id, principal.session["id"], code)
    except AuthError as exc:
        secret, uri = service.start_totp_enrolment(principal.user_id)
        img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
        buf = io.BytesIO()
        img.save(buf)
        return web.render(request, "auth/enrol.html", status_code=400, secret=secret, qr_svg=buf.getvalue().decode(), error=str(exc), required=principal.mfa_required)
    request.state.principal = None
    refreshed = service.load_principal(request.cookies.get(get_settings().cookie_name)) or principal
    return web.render(request, "auth/backup_codes.html", codes=codes, next=_next_step(refreshed, "/"))


@router.post("/login/skip-authenticator", dependencies=[Depends(check_csrf)])
def enrol_skip(request: Request, principal: Annotated[Principal, Depends(require_password_only)]):
    if principal.mfa_required:
        return RedirectResponse("/login/setup-authenticator", status_code=303)
    return RedirectResponse(_next_step(principal, "/"), status_code=303)


# --- Forced password set ------------------------------------------------------------------

@router.get("/login/set-password")
def set_password_form(request: Request, principal: Annotated[Principal, Depends(require_password_only)]):
    return web.render(request, "auth/set_password.html", error=None, forced=principal.user["must_set_password"])


@router.post("/login/set-password", dependencies=[Depends(check_csrf)])
def set_password_submit(
    request: Request,
    principal: Annotated[Principal, Depends(require_password_only)],
    password: Annotated[str, Form()],
    password2: Annotated[str, Form()],
):
    if password != password2:
        return web.render(request, "auth/set_password.html", status_code=400, error="The two passwords don't match.", forced=principal.user["must_set_password"])
    try:
        with db(None, "system") as conn:
            service.set_password(conn, principal.user_id, password)
            service.audit(conn, action="password_set", user_id=principal.user_id, tenant_id=principal.user["tenant_id"])
    except AuthError as exc:
        return web.render(request, "auth/set_password.html", status_code=400, error=str(exc), forced=principal.user["must_set_password"])
    request.state.principal = None
    refreshed = service.load_principal(request.cookies.get(get_settings().cookie_name)) or principal
    return RedirectResponse(_next_step(refreshed, "/"), status_code=303)


# --- Agency: switch the client in scope ---------------------------------------------------

@router.post("/switch-tenant", dependencies=[Depends(check_csrf)])
def switch_tenant(request: Request, principal: Annotated[Principal, Depends(require_user)], tenant_id: Annotated[str, Form()] = "", next: Annotated[str, Form()] = ""):
    if not principal.is_agency:
        return RedirectResponse("/leads", status_code=303)
    tid = UUID(tenant_id) if tenant_id else None
    if tid is not None:
        with db(None, "agency_admin") as conn:
            if conn.execute("SELECT 1 FROM tenant WHERE id = %s", (tid,)).fetchone() is None:
                return RedirectResponse("/admin", status_code=303)
    service.set_acting_tenant(principal.session["id"], tid, user_id=principal.user_id, ip=None)
    target = next if next.startswith("/") and not next.startswith("//") else ("/leads" if tid else "/admin")
    return RedirectResponse(target, status_code=303)
