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
    email_provider: str = "log"            # 'smtp' | 'postmark' | 'sendgrid' | 'fake' | 'log'
    postmark_token: str | None = None
    sendgrid_api_key: str | None = None
    # SMTP, e.g. a Google Workspace mailbox with an app password: smtp.gmail.com:587, user = the mailbox.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None           # defaults to email_from
    smtp_password: str | None = None
    email_from: str = "leads@web-marketing-toronto.com"
    email_from_name: str = "WMT CRM"

    # Lead sources. CallRail signs webhooks with the signing key shown next to the webhook in its settings
    # (optional; the per-client key in the URL is required regardless). Meta needs the app secret (to
    # verify X-Hub-Signature-256), the verify token you type into the webhook subscription, and a
    # system-user access token with leads_retrieval + pages_manage_ads on every client Page.
    callrail_signing_key: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_access_token: str | None = None

    # Ad platforms, for conversion uploads and spend. Google: either the path to a google-ads.yaml (the
    # Campaign Manager's file, mounted from Secret Manager) or the four values from it. 'ads_provider'
    # is 'live' | 'fake' (tests) | 'log' (dev).
    ads_provider: str = "live"
    google_ads_yaml: str | None = None
    google_ads_sa_key: str | None = None  # service-account JSON key, when the yaml uses json_key_file_path
    google_ads_api_version: str | None = None  # e.g. v25; Google retires each version ~1 year after release
    google_ads_developer_token: str | None = None
    google_ads_client_id: str | None = None
    google_ads_client_secret: str | None = None
    google_ads_refresh_token: str | None = None
    google_ads_login_customer_id: str | None = None

    # Shared secret for Cloud Scheduler -> /internal/cron/tick when OIDC is not used.
    cron_secret: str | None = None

    agency_name: str = "Web Marketing Toronto"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
