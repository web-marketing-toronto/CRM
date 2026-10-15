"""The one door every lead comes through.

    POST /v1/intake/{tenant_slug}/{intake_key}

Accepts JSON, form-encoded or multipart bodies, so WordPress form plugins, Zapier, Shopify
Flow and hand-written scripts can all post to it without custom headers. The key may also be
sent as `X-Intake-Key` with the path `/v1/intake/{tenant_slug}`.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import system_db
from app.models import Channel
from app.services import attribution, worker
from app.services.leads import LeadError, create_from_intake

log = logging.getLogger("crm.intake")
router = APIRouter(prefix="/v1/intake", tags=["intake"])

MAX_BODY = 256 * 1024


async def _read_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413, detail="payload too large")
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype or (raw[:1] in (b"{", b"[") and "form" not in ctype):
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        return data
    form = await request.form()
    data: dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        values = [v for v in values if isinstance(v, str)]
        if not values:
            continue
        data[key] = values[0] if len(values) == 1 else values
    if not data and raw:
        # Some plugins send JSON with a wrong content-type.
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, UnicodeDecodeError):
            pass
    return data


def _lookup_tenant(conn, slug: str, key: str | None) -> dict:
    tenant = conn.execute("SELECT * FROM tenant WHERE slug = %s AND is_active", (slug,)).fetchone()
    if tenant is None or not key or not hmac.compare_digest(tenant["intake_key"], key):
        # Same response for unknown slug and bad key, so the endpoint can't be used to enumerate clients.
        raise HTTPException(status_code=404, detail="unknown intake endpoint")
    return tenant


@router.post("/{slug}/{key}")
@router.post("/{slug}")
async def intake(slug: str, request: Request, background: BackgroundTasks, key: str | None = None):
    key = key or request.headers.get("x-intake-key") or request.query_params.get("key")
    body = await _read_body(request)
    settings = get_settings()
    fwd = request.headers.get("x-forwarded-for")
    ip = (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else None)
    user_agent = (request.headers.get("user-agent") or "")[:300]

    with system_db() as conn:
        tenant = _lookup_tenant(conn, slug, key)
        fields = attribution.interpret(body, tenant.get("field_map") or {}, default_channel=Channel.WEBSITE_FORM)
        try:
            result = create_from_intake(
                conn, tenant, fields, raw_payload=body, ip=ip, user_agent=user_agent, maps_api_key=settings.google_maps_api_key
            )
        except LeadError as exc:
            received = sorted(fields.flat.keys())[:40]
            log.info("intake rejected for %s: %s (fields: %s)", slug, exc, received)
            detail = str(exc)
            if received:
                detail += ". Fields received: " + ", ".join(received) + ". Expected one of: name, phone, email (see the field map in the client's settings)"
            else:
                detail += ". The request had no fields at all; check the Data section of the sending step"
            raise HTTPException(status_code=422, detail=detail) from exc

    log.info("intake %s ref=%s created=%s channel=%s platform=%s", slug, result.ref_code, result.created, fields.channel, fields.platform)
    background.add_task(worker.dispatch_lead_now, result.lead_id)
    return JSONResponse(
        {
            "ok": True,
            "lead_id": str(result.lead_id),
            "ref": result.ref_code,
            "created": result.created,
            "reopened": result.reopened,
            "area": result.area_status.value,
            "channel": fields.channel.value,
            "platform": fields.platform.value,
        },
        status_code=201 if result.created else 200,
    )


@router.get("/{slug}/{key}")
@router.get("/{slug}")
def intake_get(slug: str, key: str | None = None):
    return JSONResponse({"ok": True, "detail": "POST lead data to this URL as JSON or form fields."})
