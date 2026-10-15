"""Template rendering with the request context every page needs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import get_settings
from app.models import (
    ACTIVITY_LABELS,
    CHANNEL_LABELS,
    LOST_REASON_LABELS,
    PLATFORM_LABELS,
    STATUS_LABELS,
    ActivityType,
    Channel,
    LeadStatus,
    LostReason,
    Platform,
)
from app.services.normalize import format_phone_display

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _relative(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    delta = now - dt
    s = int(delta.total_seconds())
    if s < 0:
        s = -s
        suffix = "from now"
    else:
        suffix = "ago"
    if s < 60:
        return "just now" if suffix == "ago" else "in under a minute"
    m = s // 60
    if m < 60:
        return f"{m} min {suffix}"
    h = m // 60
    if h < 24:
        return f"{h} h {suffix}"
    d = h // 24
    if d < 14:
        return f"{d} d {suffix}"
    w = d // 7
    if w < 9:
        return f"{w} w {suffix}"
    return dt.strftime("%b %-d, %Y")


def _local(dt: datetime | None, tz: str = "America/Toronto", fmt: str = "%b %-d, %-I:%M %p") -> str:
    if dt is None:
        return ""
    return dt.astimezone(ZoneInfo(tz)).strftime(fmt)


def _money(value: Decimal | float | int | None) -> str:
    if value is None:
        return ""
    return f"${Decimal(value):,.0f}"


def _label(value: str | None, table: dict) -> str:
    if value is None:
        return ""
    for enum_cls in (LeadStatus, Channel, Platform, ActivityType, LostReason):
        try:
            return table[enum_cls(value)]
        except (ValueError, KeyError):
            continue
    return str(value)


env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["relative"] = _relative
env.filters["local"] = _local
env.filters["money"] = _money
env.filters["phone"] = format_phone_display
env.filters["status_label"] = lambda v: _label(v, STATUS_LABELS)
env.filters["channel_label"] = lambda v: _label(v, CHANNEL_LABELS)
env.filters["platform_label"] = lambda v: _label(v, PLATFORM_LABELS)
env.filters["activity_label"] = lambda v: _label(v, ACTIVITY_LABELS)
env.filters["lost_label"] = lambda v: _label(v, LOST_REASON_LABELS)
env.globals["LeadStatus"] = LeadStatus
env.globals["LostReason"] = LostReason
env.globals["Platform"] = Platform
env.globals["STATUS_LABELS"] = STATUS_LABELS
env.globals["LOST_REASON_LABELS"] = LOST_REASON_LABELS
env.globals["PLATFORM_LABELS"] = PLATFORM_LABELS


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    settings = get_settings()
    principal = getattr(request.state, "principal", None)
    ctx: dict[str, Any] = {
        "request": request,
        "settings": settings,
        "principal": principal,
        "csrf_token": principal.session["csrf_token"] if principal else "",
        "tz": (principal.tenant or {}).get("timezone", "America/Toronto") if principal else "America/Toronto",
        "is_htmx": bool(request.headers.get("hx-request")),
    }
    ctx.update(context)
    return HTMLResponse(env.get_template(name).render(ctx), status_code=status_code)


def error_page(request: Request, status_code: int, detail: str) -> HTMLResponse:
    return render(request, "error.html", status_code=status_code, status=status_code, detail=detail)
