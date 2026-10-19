"""Test fixtures: a real Postgres, migrated once per session, truncated between tests.

Local default: the Postgres started by `make db` (socket /tmp, port 5433, superuser 'crm').
CI: a postgres:16 service; see .github/workflows/ci.yml for the variables.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import psycopg
import pytest

PG_HOST = os.environ.get("TEST_PG_HOST", "/tmp")
PG_PORT = os.environ.get("TEST_PG_PORT", "5433")
PG_ADMIN_USER = os.environ.get("TEST_PG_ADMIN_USER", "crm")
PG_ADMIN_PASSWORD = os.environ.get("TEST_PG_ADMIN_PASSWORD", "")
PG_DB = os.environ.get("TEST_PG_DB", "crm_test")

ADMIN_URL = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_ADMIN_USER} password={PG_ADMIN_PASSWORD}"
APP_URL = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user=crm_app password=crm_app"

os.environ["ENV"] = "test"
os.environ["DATABASE_URL"] = APP_URL
os.environ["MIGRATE_DATABASE_URL"] = ADMIN_URL
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-test-secret-key"
os.environ["BASE_URL"] = "http://testserver"
os.environ["CRON_SECRET"] = "test-cron"
os.environ["SMS_PROVIDER"] = "fake"
os.environ["EMAIL_PROVIDER"] = "fake"
os.environ["TWILIO_ACCOUNT_SID"] = "ACtest"
os.environ["TWILIO_AUTH_TOKEN"] = "test-twilio-token"
os.environ["TWILIO_AGENCY_FROM"] = "+14165550000"
os.environ["META_APP_SECRET"] = "meta-secret"
os.environ["META_VERIFY_TOKEN"] = "verify-me"
os.environ["META_ACCESS_TOKEN"] = "EAAtest"
os.environ["ADS_PROVIDER"] = "fake"
os.environ.pop("GOOGLE_MAPS_API_KEY", None)

from app import db as dbmod  # noqa: E402
from app.migrate import migrate  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(ADMIN_URL, quiet=True)
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("ALTER ROLE crm_app LOGIN PASSWORD 'crm_app'")
    dbmod.init_pool(APP_URL)
    yield
    dbmod.close_pool()


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    yield
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE tenant, app_user, session, lead, lead_source, activity, reminder, conversion_event, ad_spend, audit, message_log, sms_opt_out, import_batch, job_run, attribution_beacon, inbound_email RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def outbox():
    from app.services import adplatforms, messaging

    messaging.outbox.clear()
    adplatforms.outbox.clear()
    yield messaging.outbox
    messaging.outbox.clear()
    adplatforms.outbox.clear()


@pytest.fixture
def admin_conn() -> Iterator[psycopg.Connection]:
    """Superuser connection for test setup and assertions that bypass RLS on purpose."""
    with psycopg.connect(ADMIN_URL, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        yield conn


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def make_tenant(admin_conn):
    def _make(slug: str = "alpha", name: str | None = None, **cols):
        cols.setdefault("job_types", ["Roof replacement", "Repair"])
        keys = ", ".join(["slug", "name"] + list(cols))
        placeholders = ", ".join(["%s"] * (2 + len(cols)))
        row = admin_conn.execute(
            f"INSERT INTO tenant ({keys}) VALUES ({placeholders}) RETURNING *",
            [slug, name or slug.title()] + [psycopg.types.json.Jsonb(v) if isinstance(v, dict) else v for v in cols.values()],
        ).fetchone()
        return row

    return _make


@pytest.fixture
def make_user(admin_conn):
    from app.auth import crypto

    def _make(email: str, *, tenant_id=None, role: str = "member", name: str | None = None, password: str = "correct-horse-battery", totp_enabled: bool = False, must_set_password: bool = False, mobile: str | None = None, notify_sms: bool = True, notify_email: bool = True):
        row = admin_conn.execute(
            """
            INSERT INTO app_user (tenant_id, email, name, role, password_hash, totp_enabled, must_set_password, mobile_e164, notify_sms, notify_email)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (tenant_id, email, name or email.split("@")[0].title(), role, crypto.hash_password(password), totp_enabled, must_set_password, mobile, notify_sms, notify_email),
        ).fetchone()
        return row

    return _make


@pytest.fixture
def login(client):
    def _login(email: str, password: str = "correct-horse-battery"):
        r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
        assert r.status_code == 303, r.text
        return r

    return _login


def csrf_from(client) -> str:
    """Read the CSRF token out of any rendered page."""
    r = client.get("/account")
    m = re.search(r'name="csrf-token" content="([0-9a-f]+)"', r.text)
    assert m, "no csrf token on page"
    return m.group(1)
