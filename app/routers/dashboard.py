"""The agency home screen: every client at a glance, what needs attention, and whether the pipes are up."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app import web
from app.auth.deps import require_agency
from app.auth.service import Principal
from app.db import db
from app.services import dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def home(request: Request, principal: Annotated[Principal, Depends(require_agency)]):
    with db(None, "agency_admin") as conn:
        ctx = dashboard.build(conn)
    return web.render(request, "admin/home.html", **ctx)
