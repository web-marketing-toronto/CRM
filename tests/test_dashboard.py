"""The agency dashboard, the Team page, the Account test-send buttons and the scheduler heartbeat."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pyotp

from app.auth import crypto
from tests.conftest import csrf_from


def agency_login(client, make_user, admin_conn, email="jeff@agency.test", name="Jeff Fidler", mobile=None):
    u = make_user(email, role="agency_admin", totp_enabled=True, name=name, mobile=mobile)
    secret = crypto.new_totp_secret()
    admin_conn.execute(
        "UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"])
    )
    client.post("/login", data={"email": email, "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    return u


def _intake(client, tenant, **fields):
    data = {"name": "Lead Person", "phone": "7055550001", "message": "Need a quote", **fields}
    r = client.post(f"/v1/intake/{tenant['slug']}/{tenant['intake_key']}", json=data)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_agency_lands_on_dashboard_with_attention_and_setup_gaps(client, make_tenant, make_user, admin_conn):
    ready = make_tenant(
        "ready",
        name="Ready Roofing",
        twilio_number="+12495550100",
        service_area={"center": [45.0, -79.3], "radius_km": 60},
    )
    owner = make_user("dan@ready.test", tenant_id=ready["id"], role="owner", mobile="+17055550199")
    admin_conn.execute("UPDATE app_user SET last_login_at = now() WHERE id = %s", (owner["id"],))
    make_tenant("bare", name="Bare Paving")
    _intake(client, ready, name="Fresh Lead", phone="7055550001")
    stale = _intake(client, ready, name="Stale Lead", phone="7055550002")
    admin_conn.execute(
        "UPDATE lead SET created_at = now() - interval '30 hours' WHERE id = %s", (stale["lead_id"],)
    )
    agency_login(client, make_user, admin_conn)

    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200, page.text
    html = page.text
    # The stale lead is listed as needing attention; the fresh one is not (the client still has time).
    assert "Stale Lead" in html and "New lead, nobody has called" in html
    assert html.count("Fresh Lead") == 1  # only in Latest leads
    # Setup column: the wired client is ready, the bare one lists what is missing.
    assert "Ready" in html
    assert "No owner login" in html and "No texting number" in html and "No service area" in html
    assert "Bare Paving" in html and "Ready Roofing" in html
    # Pipes and the nav are there.
    assert "Scheduler" in html and "never ran" in html
    assert 'href="/admin/team"' in html and 'href="/dashboard"' in html


def test_dashboard_lists_leads_waiting_on_a_reply(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha", twilio_number="+12495550100")
    make_user("dan@alpha.test", tenant_id=t["id"], role="owner", mobile="+17055550199")
    lead = _intake(client, t, name="Replied Lead", phone="7055550003")
    three_hours_ago = datetime.now(UTC) - timedelta(hours=3)
    admin_conn.execute(
        "UPDATE lead SET status = 'contacted', last_touch_at = %s, last_reply_at = %s, created_at = %s WHERE id = %s",
        (
            three_hours_ago - timedelta(hours=1),
            three_hours_ago,
            three_hours_ago - timedelta(hours=2),
            lead["lead_id"],
        ),
    )
    agency_login(client, make_user, admin_conn)
    html = client.get("/dashboard").text
    assert "Replied Lead" in html and "Lead replied, no answer yet" in html


def test_heartbeat_recorded_by_tick_and_shown(client, make_user, admin_conn):
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200
    row = admin_conn.execute("SELECT * FROM job_run WHERE name = 'tick'").fetchone()
    assert row is not None and row["finished_at"] is not None
    agency_login(client, make_user, admin_conn)
    html = client.get("/dashboard").text
    assert "last ran" in html and "never ran" not in html


def test_team_page_adds_member_and_guards_self_disable(client, make_user, admin_conn):
    me = agency_login(client, make_user, admin_conn)
    csrf = csrf_from(client)
    page = client.get("/admin/team")
    assert page.status_code == 200 and "Jeff Fidler" in page.text and "you" in page.text

    r = client.post(
        "/admin/team",
        data={
            "name": "Justin Then",
            "email": "justin@agency.test",
            "mobile": "705 555 0150",
            "csrf_token": csrf,
        },
    )
    assert r.status_code == 200 and "Temporary password" in r.text and "justin@agency.test" in r.text
    temp = re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
    new = admin_conn.execute("SELECT * FROM app_user WHERE email = 'justin@agency.test'").fetchone()
    assert (
        new["role"] == "agency_admin"
        and new["tenant_id"] is None
        and new["mobile_e164"] == "+17055550150"
        and new["must_set_password"]
    )

    # Duplicate email is refused.
    r = client.post("/admin/team", data={"name": "Again", "email": "Justin@agency.test", "csrf_token": csrf})
    assert r.status_code == 400 and "already has a login" in r.text

    # The new member signs in with the temp password and is pushed to set their own, then enrol an authenticator.
    fresh = client.__class__(client.app, base_url="http://testserver")
    with fresh:
        r = fresh.post(
            "/login", data={"email": "justin@agency.test", "password": temp}, follow_redirects=False
        )
        assert r.status_code == 303
        assert fresh.get("/dashboard", follow_redirects=False).headers["location"] in (
            "/login/setup-authenticator",
            "/login/set-password",
        )

    # I can't disable myself; I can disable (and re-enable) a colleague, and the redirect comes back to Team.
    r = client.post(
        f"/admin/users/{me['id']}/toggle-active", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and "can't disable your own" in unquote(r.headers["location"])
    assert admin_conn.execute("SELECT is_active FROM app_user WHERE id = %s", (me["id"],)).fetchone()[
        "is_active"
    ]
    r = client.post(
        f"/admin/users/{new['id']}/toggle-active", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/team"
    assert not admin_conn.execute("SELECT is_active FROM app_user WHERE id = %s", (new["id"],)).fetchone()[
        "is_active"
    ]
    client.post(f"/admin/users/{new['id']}/toggle-active", data={"csrf_token": csrf}, follow_redirects=False)
    assert admin_conn.execute("SELECT is_active FROM app_user WHERE id = %s", (new["id"],)).fetchone()[
        "is_active"
    ]

    # Resetting a team member's password renders the Team page (not a client page) with the new temp password.
    r = client.post(f"/admin/users/{new['id']}/reset-password", data={"csrf_token": csrf})
    assert r.status_code == 200 and "Temporary password" in r.text and "<h1>Team</h1>" in r.text


def test_client_users_cannot_reach_team_or_dashboard(client, make_tenant, make_user):
    t = make_tenant("alpha")
    make_user("meg@alpha.test", tenant_id=t["id"], role="member")
    client.post("/login", data={"email": "meg@alpha.test", "password": "correct-horse-battery"})
    assert client.get("/", follow_redirects=False).headers["location"] == "/leads"
    assert client.get("/dashboard").status_code == 403
    assert client.get("/admin/team").status_code == 403


def test_account_test_text_and_email(client, make_tenant, make_user, outbox):
    t = make_tenant("alpha")
    make_user("meg@alpha.test", tenant_id=t["id"], role="member", mobile="+17055550177")
    client.post("/login", data={"email": "meg@alpha.test", "password": "correct-horse-battery"})
    csrf = csrf_from(client)
    page = client.get("/account")
    assert "Send me a test text" in page.text and "Send me a test email" in page.text

    r = client.post("/account/test-text", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert len(outbox.sms) == 1 and outbox.sms[0]["to"] == "+17055550177" and outbox.sms[0]["kind"] == "test"
    assert outbox.sms[0]["from"] == "+14165550000"  # the agency number
    assert "Reply ?" in outbox.sms[0]["body"]

    r = client.post("/account/test-email", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert (
        len(outbox.email) == 1
        and outbox.email[0]["to"] == "meg@alpha.test"
        and outbox.email[0]["kind"] == "test"
    )


def test_account_test_text_needs_a_mobile(client, make_tenant, make_user, outbox):
    t = make_tenant("alpha")
    make_user("meg@alpha.test", tenant_id=t["id"], role="member")
    client.post("/login", data={"email": "meg@alpha.test", "password": "correct-horse-battery"})
    csrf = csrf_from(client)
    r = client.post("/account/test-text", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert outbox.sms == []


def test_dashboard_lists_failed_uploads_with_reason(client, make_tenant, make_user, admin_conn):
    """A conversion that gave up shows on the dashboard with its reason and a retry button."""
    import re

    import pyotp

    from app.auth import crypto

    t = make_tenant("roofco", name="North Ridge Roofing", settings={"google_ads": {"customer_id": "1234567890"}, "conversions": {"enabled": True}})
    url = f"/v1/intake/{t['slug']}/{t['intake_key']}"
    lead_id = client.post(url, json={"name": "G One", "phone": "7055550001", "gclid": "a"}).json()["lead_id"]
    admin_conn.execute(
        "INSERT INTO conversion_event (tenant_id, lead_id, platform, action, status, last_error, attempted_at) VALUES (%s, %s, 'google_ads', 'qualified', 'failed', %s, now())",
        (t["id"], lead_id, "No Google conversion action set for 'qualified' on North Ridge Roofing"),
    )
    u = make_user("boss@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "boss@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    html = client.get("/dashboard").text
    assert "Uploads that gave up" in html and "No Google conversion action set for 'qualified'" in html and "Retry now" in html
    assert "Meta: <strong>no token</strong>" in html or "Meta: token loaded" in html
