"""The minute tick that Cloud Scheduler calls.

Phase 2 (follow-up engine) fills `run_tick` in app/services/worker.py. Until then the endpoint
exists so the scheduler job can be created and verified during setup.

Authentication: the service is public (clients use it from their phones), so Cloud Run does not
verify bearer tokens on our behalf. Cloud Scheduler therefore sends a shared secret in the
X-Cron-Secret header, set from Secret Manager in deploy/setup_gcp.sh.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings

log = logging.getLogger("crm.cron")
router = APIRouter(prefix="/internal", tags=["internal"])


def _authorized(request: Request) -> bool:
    secret = get_settings().cron_secret
    sent = request.headers.get("x-cron-secret", "")
    return bool(secret and sent and hmac.compare_digest(secret, sent))


@router.post("/cron/tick")
def tick(request: Request):
    if not _authorized(request):
        raise HTTPException(status_code=403, detail="forbidden")
    from app.services import worker

    result = worker.run_tick()
    log.info("tick %s", result)
    return {"ok": True, **result}
