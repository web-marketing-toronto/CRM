"""Application settings, read from environment variables (or a .env file in development)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="dev", description="dev | test | prod")
    base_url: str = "http://localhost:8000"

    # Connection string for the application role (crm_app). Must not be a superuser or
    # table owner, otherwise row-level security is bypassed.
    database_url: str = "postgresql://crm_app@/crm?host=/tmp&port=5433"
    # Connection string used only by `python -m app.migrate`. May be the owner/admin role.
    migrate_database_url: str | None = None
    db_pool_min: int = 1
    db_pool_max: int = 8

    # Signs session cookies and encrypts TOTP secrets at rest. 32+ random bytes, base64 or hex.
    secret_key: str = "dev-only-secret-change-me-dev-only-secret-change-me"
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool = False
    cookie_name: str = "wmt_crm_session"

    # Optional integrations. Empty means the feature degrades gracefully.
    google_maps_api_key: str | None = None

    # Messaging. Providers: 'twilio' | 'fake' (records only; tests) | 'log' (prints; local dev).
    sms_provider: str = "log"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_agency_from: str | None = None  # E.164 number used for alerts to agency staff and clients without their own number
    email_provider: str = "log"            # 'postmark' | 'sendgrid' | 'fake' | 'log'
    postmark_token: str | None = None
    sendgrid_api_key: str | None = None
    email_from: str = "leads@web-marketing-toronto.com"
    email_from_name: str = "WMT Leads"

    # Lead sources. CallRail signs webhooks with the signing key shown next to the webhook in its settings
    # (optional; the per-client key in the URL is required regardless). Meta needs the app secret (to
    # verify X-Hub-Signature-256), the verify token you type into the webhook subscription, and a
    # system-user access token with leads_retrieval + pages_manage_ads on every client Page.
    callrail_signing_key: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_access_token: str | None = None

    # Shared secret for Cloud Scheduler -> /internal/cron/tick when OIDC is not used.
    cron_secret: str | None = None

    agency_name: str = "Web Marketing Toronto"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
