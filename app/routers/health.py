from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import system_db

router = APIRouter()


@router.get("/health")
def healthz():
    try:
        with system_db() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "db": True}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "db": False, "error": str(exc)[:200]}, status_code=503)
