"""FastAPI dependencies: who is asking, and are they allowed."""

from __future__ import annotations

import hmac
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.service import Principal, load_principal
from app.config import get_settings


class LoginRequired(Exception):
    def __init__(self, next_url: str, reason: str = "login"):
        self.next_url = next_url
        self.reason = reason  # 'login' | 'mfa' | 'enrol' | 'password'


def redirect_for(exc: LoginRequired) -> RedirectResponse:
    target = {
        "login": "/login",
        "mfa": "/login/mfa",
        "enrol": "/login/setup-authenticator",
        "password": "/login/set-password",
    }[exc.reason]
    if exc.reason == "login" and exc.next_url and exc.next_url != "/":
        target += "?next=" + quote(exc.next_url, safe="")
    return RedirectResponse(target, status_code=303)


def _request_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def get_principal(request: Request) -> Principal | None:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    principal = load_principal(request.cookies.get(get_settings().cookie_name))
    request.state.principal = principal
    return principal


def require_user(request: Request, principal: Annotated[Principal | None, Depends(get_principal)]) -> Principal:
    """A fully authenticated user: password, then MFA where enabled or required, then password reset if forced."""
    if principal is None:
        raise LoginRequired(_request_path(request), "login")
    if principal.needs_mfa_challenge:
        raise LoginRequired(_request_path(request), "mfa")
    if principal.needs_mfa_enrolment:
        raise LoginRequired(_request_path(request), "enrol")
    if principal.user["must_set_password"]:
        raise LoginRequired(_request_path(request), "password")
    return principal


def require_password_only(request: Request, principal: Annotated[Principal | None, Depends(get_principal)]) -> Principal:
    """Logged in with a password; used by the MFA and set-password screens themselves."""
    if principal is None:
        raise LoginRequired(_request_path(request), "login")
    return principal


def require_agency(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    if not principal.is_agency:
        raise HTTPException(status_code=403, detail="Agency access only")
    return principal


def require_tenant(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    """A user with a tenant in scope: a client user, or an agency admin who picked a client."""
    if principal.tenant is None:
        raise HTTPException(status_code=409, detail="Pick a client first")
    return principal


def require_owner(principal: Annotated[Principal, Depends(require_tenant)]) -> Principal:
    if principal.role.value not in ("owner", "agency_admin"):
        raise HTTPException(status_code=403, detail="Owner access only")
    return principal


async def check_csrf(request: Request, principal: Annotated[Principal | None, Depends(get_principal)]) -> None:
    """Double-submit CSRF for state-changing browser requests. API routes don't use this."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if principal is None:
        return
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        sent = form.get("csrf_token")  # type: ignore[assignment]
    expected = principal.session["csrf_token"]
    if not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Form expired, reload the page and try again")
