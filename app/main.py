"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import web
from app.auth.deps import LoginRequired, redirect_for
from app.config import get_settings
from app.db import close_pool, init_pool
from app.routers import account, admin, auth, cron, health, intake, leads

log = logging.getLogger("crm")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="WMT CRM", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(intake.router)
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(leads.router)
    app.include_router(admin.router)
    app.include_router(cron.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            resp = JSONResponse({"detail": "login required"}, status_code=401)
            resp.headers["HX-Redirect"] = redirect_for(exc).headers["location"]
            return resp
        return redirect_for(exc)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/v1/", "/internal/", "/health")) or request.headers.get("hx-request"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 409 and exc.detail == "Pick a client first":
            return RedirectResponse("/admin", status_code=303)
        return web.error_page(request, exc.status_code, str(exc.detail))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if settings.is_prod:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


app = create_app()
