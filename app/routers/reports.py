"""Reports: the client's own numbers, the agency's cross-client view, CSV export and the monthly PDF."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import web
from app.auth.deps import require_agency, require_tenant
from app.auth.service import Principal
from app.config import get_settings
from app.db import db
from app.services import reports

router = APIRouter(tags=["reports"])

PERIODS = [("this_month", "This month"), ("last_month", "Last month"), ("last_30", "Last 30 days"), ("last_90", "Last 90 days"), ("year", "Year to date"), ("custom", "Custom")]


def _period(request: Request, tz: str) -> reports.Period:
    q = request.query_params
    return reports.period(q.get("period", "this_month"), tz, q.get("start"), q.get("end"))


def _show_spend(tenant: dict, principal: Principal) -> bool:
    return principal.is_agency or bool(((tenant.get("settings") or {}).get("reports") or {}).get("show_spend"))


@router.get("/reports")
def client_report(request: Request, principal: Annotated[Principal, Depends(require_tenant)]):
    with db(principal.tenant_id, principal.db_role) as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        p = _period(request, tenant["timezone"])
        summary = reports.tenant_summary(conn, tenant, p)
        months = reports.months_available(conn, tenant["id"], tenant["timezone"])
    return web.render(request, "reports/client.html", summary=summary, periods=PERIODS, period=p, show_spend=_show_spend(tenant, principal), months=months, tenant_row=tenant, query=dict(request.query_params))


@router.get("/reports/leads.csv")
def client_csv(request: Request, principal: Annotated[Principal, Depends(require_tenant)]):
    with db(principal.tenant_id, principal.db_role) as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        p = _period(request, tenant["timezone"])
        body = reports.leads_csv(conn, p, tenant_id=tenant["id"], tz_name=tenant["timezone"])
    name = f"{tenant['slug']}-leads-{p.start.date()}-to-{p.end.date()}.csv"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/monthly.pdf")
def client_monthly_pdf(request: Request, principal: Annotated[Principal, Depends(require_tenant)], month: str | None = None):
    with db(principal.tenant_id, principal.db_role) as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (principal.tenant_id,)).fetchone()
        try:
            y, m = (int(x) for x in (month or datetime.now(UTC).strftime("%Y-%m")).split("-"))
            p = reports.month_period(y, m, tenant["timezone"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="month should look like 2026-09") from exc
        summary = reports.tenant_summary(conn, tenant, p)
    pdf = reports.monthly_pdf(tenant, summary, show_spend=_show_spend(tenant, principal), agency_name=get_settings().agency_name)
    name = f"{tenant['slug']}-{y:04d}-{m:02d}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/admin/reports")
def agency_report(request: Request, principal: Annotated[Principal, Depends(require_agency)]):
    p = _period(request, "America/Toronto")
    with db(None, "agency_admin") as conn:
        rows, total, extras = reports.agency_summary(conn, p)
    return web.render(request, "admin/reports.html", rows=rows, total=total, extras=extras, periods=PERIODS, period=p, query=dict(request.query_params))


@router.get("/admin/reports.csv")
def agency_csv(request: Request, principal: Annotated[Principal, Depends(require_agency)], tenant_id: UUID | None = None):
    p = _period(request, "America/Toronto")
    with db(None, "agency_admin") as conn:
        body = reports.leads_csv(conn, p, tenant_id=tenant_id)
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="leads-{p.start.date()}-to-{p.end.date()}.csv"'})
