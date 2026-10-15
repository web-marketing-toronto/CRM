import re

import pyotp

from app.auth import crypto
from tests.conftest import csrf_from


def _intake(client, tenant, **fields):
    return client.post(f"/v1/intake/{tenant['slug']}/{tenant['intake_key']}", json=fields).json()


def test_login_redirects_and_sets_cookie(client, make_tenant, make_user):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"], role="member")
    r = client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "wmt_crm_session" in r.cookies
    home = client.get("/", follow_redirects=False)
    assert home.headers["location"] == "/leads"


def test_bad_password_and_lockout(client, make_tenant, make_user):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"])
    for _ in range(8):
        r = client.post("/login", data={"email": "dan@alpha.test", "password": "nope-nope-nope"})
        assert r.status_code == 401
    r = client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    assert r.status_code == 401 and "Too many" in r.text


def test_unknown_email_same_message_as_bad_password(client):
    r = client.post("/login", data={"email": "nobody@x.test", "password": "whatever-whatever"})
    assert r.status_code == 401 and "password don&#39;t match" in r.text


def test_owner_must_enrol_mfa_then_can_use_app(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    make_user("owner@alpha.test", tenant_id=t["id"], role="owner")
    client.post("/login", data={"email": "owner@alpha.test", "password": "correct-horse-battery"})
    r = client.get("/leads", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login/setup-authenticator"
    page = client.get("/login/setup-authenticator")
    assert page.status_code == 200
    secret = re.search(r'<code class="secret">([A-Z2-7]+)</code>', page.text).group(1)
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    code = pyotp.TOTP(secret).now()
    r = client.post("/login/setup-authenticator", data={"code": code, "csrf_token": csrf})
    assert r.status_code == 200 and "backup codes" in r.text.lower()
    assert client.get("/leads").status_code == 200
    user = admin_conn.execute("SELECT totp_enabled, backup_codes FROM app_user WHERE email = 'owner@alpha.test'").fetchone()
    assert user["totp_enabled"] is True and len(user["backup_codes"]) == 8


def test_mfa_challenge_blocks_until_code_and_rejects_replay(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    u = make_user("owner@alpha.test", tenant_id=t["id"], role="owner", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "owner@alpha.test", "password": "correct-horse-battery"})
    assert client.get("/leads", follow_redirects=False).headers["location"] == "/login/mfa"
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    assert client.post("/login/mfa", data={"code": "000000", "csrf_token": csrf}).status_code == 401
    code = pyotp.TOTP(secret).now()
    assert client.post("/login/mfa", data={"code": code, "csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/leads").status_code == 200
    # Same code can't be used again on a fresh session (replay protection).
    client.post("/logout", data={"csrf_token": csrf})
    client.post("/login", data={"email": "owner@alpha.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf2 = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    assert client.post("/login/mfa", data={"code": code, "csrf_token": csrf2}).status_code == 401


def test_csrf_required_on_actions(client, make_tenant, make_user):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"])
    lead = _intake(client, t, name="Mike", phone="7055550142")
    client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    r = client.post(f"/leads/{lead['lead_id']}/touch", data={"type": "call"})
    assert r.status_code == 403


def test_member_sees_only_own_tenant(client, make_tenant, make_user):
    a = make_tenant("alpha")
    b = make_tenant("beta")
    make_user("dan@alpha.test", tenant_id=a["id"])
    mine = _intake(client, a, name="Mine", phone="7055550001")
    theirs = _intake(client, b, name="Theirs", phone="7055550002")
    client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    page = client.get("/leads?view=all")
    assert "Mine" in page.text and "Theirs" not in page.text
    assert client.get(f"/leads/{mine['lead_id']}").status_code == 200
    assert client.get(f"/leads/{theirs['lead_id']}").status_code == 404


def test_touch_moves_new_to_contacted_and_status_flow(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"])
    lead = _intake(client, t, name="Mike", phone="7055550142")
    client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    csrf = csrf_from(client)
    lid = lead["lead_id"]

    r = client.post(f"/leads/{lid}/touch", data={"type": "call", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="lead"' in r.text
    row = admin_conn.execute("SELECT status, first_contact_at, last_touch_at FROM lead WHERE id = %s", (lid,)).fetchone()
    assert row["status"] == "contacted" and row["first_contact_at"] is not None and row["last_touch_at"] is not None

    # Won without a value is refused with a message, not a 500.
    r = client.post(f"/leads/{lid}/status", data={"status": "won", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "deal value" in r.text.lower()
    r = client.post(f"/leads/{lid}/status", data={"status": "won", "deal_value": "$8,500", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    row = admin_conn.execute("SELECT status, deal_value, won_at FROM lead WHERE id = %s", (lid,)).fetchone()
    assert row["status"] == "won" and float(row["deal_value"]) == 8500 and row["won_at"] is not None

    r = client.post(f"/leads/{lid}/status", data={"status": "lost", "lost_reason": "price", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    row = admin_conn.execute("SELECT status, deal_value, lost_reason FROM lead WHERE id = %s", (lid,)).fetchone()
    assert row["status"] == "lost" and row["lost_reason"] == "price" and row["deal_value"] is None

    changes = admin_conn.execute("SELECT detail FROM activity WHERE lead_id = %s AND type = 'status_change' ORDER BY id", (lid,)).fetchall()
    assert [c["detail"]["to"] for c in changes] == ["contacted", "won", "lost"]


def test_notes_and_manual_lead(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"])
    client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    csrf = csrf_from(client)
    r = client.post("/leads/new", data={"contact_name": "Walk In", "phone": "705 555 0999", "job_type": "Repair", "platform": "referral", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    lid = r.headers["location"].rsplit("/", 1)[1]
    row = admin_conn.execute("SELECT channel, platform, phone_e164 FROM lead WHERE id = %s", (lid,)).fetchone()
    assert row["channel"] == "manual" and row["platform"] == "referral" and row["phone_e164"] == "+17055550999"
    r = client.post(f"/leads/{lid}/note", data={"body": "Left voicemail", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Left voicemail" in r.text


def test_needs_action_view_and_counts(client, make_tenant, make_user):
    t = make_tenant("alpha")
    make_user("dan@alpha.test", tenant_id=t["id"])
    _intake(client, t, name="Fresh", phone="7055550010")
    client.post("/login", data={"email": "dan@alpha.test", "password": "correct-horse-battery"})
    page = client.get("/leads")
    assert "Fresh" in page.text and 'class="n">1<' in page.text


def test_agency_admin_switches_tenants_and_sees_dashboard(client, make_tenant, make_user, admin_conn):
    a = make_tenant("alpha")
    b = make_tenant("beta")
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    _intake(client, a, name="Alpha Lead", phone="7055550001")
    _intake(client, b, name="Beta Lead", phone="7055550002")
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    assert client.get("/", follow_redirects=False).headers["location"] == "/admin"
    dash = client.get("/admin")
    assert dash.status_code == 200 and "Alpha" in dash.text and "Beta" in dash.text
    csrf = csrf_from(client)
    client.post("/switch-tenant", data={"tenant_id": str(b["id"]), "csrf_token": csrf})
    page = client.get("/leads?view=all")
    assert "Beta Lead" in page.text and "Alpha Lead" not in page.text


def test_agency_creates_tenant_and_user(client, make_user, admin_conn):
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    csrf = csrf_from(client)
    r = client.post("/admin/tenants/new", data={"name": "North Ridge Roofing", "job_types": "Roof replacement\nRepair", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    tid = r.headers["location"].rsplit("/", 1)[1]
    tenant = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (tid,)).fetchone()
    assert tenant["slug"] == "north-ridge-roofing" and tenant["job_types"] == ["Roof replacement", "Repair"]
    r = client.post(f"/admin/tenants/{tid}/users", data={"name": "Dan Mercer", "email": "dan@nr.test", "role": "owner", "mobile": "705 555 0100", "csrf_token": csrf})
    assert r.status_code == 200 and "Temporary password" in r.text
    temp = re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
    # The new owner logs in with the temp password and is sent to set their own.
    fresh = client.__class__(client.app, base_url="http://testserver")
    with fresh:
        r = fresh.post("/login", data={"email": "dan@nr.test", "password": temp}, follow_redirects=False)
        assert r.status_code == 303
        assert fresh.get("/leads", follow_redirects=False).headers["location"] == "/login/setup-authenticator"


def test_cron_tick_requires_secret(client):
    assert client.post("/internal/cron/tick").status_code == 403
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_healthz(client):
    assert client.get("/health").json() == {"ok": True, "db": True}


def test_business_hours_parse():
    from app.routers.admin import parse_hours

    assert parse_hours("7:00", "7:00", ["1", "2", "3", "4", "5", "6"]) == {"start": "07:00", "end": "19:00", "days": [1, 2, 3, 4, 5, 6]}
    assert parse_hours("8", "6:30", []) == {"start": "08:00", "end": "18:30", "days": [1, 2, 3, 4, 5, 6]}
    assert parse_hours("12:00", "12:00", ["7"]) == {"start": "00:00", "end": "12:00", "days": [7]}
    import pytest

    with pytest.raises(ValueError):
        parse_hours("", "7", [])
    with pytest.raises(ValueError):
        parse_hours("13:00", "7", [])


def test_agency_click_through_to_lead(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    lead = _intake(client, t, name="Click Me", phone="7055550123")
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    r = client.get(f"/admin/tenants/{t['id']}/leads/{lead['lead_id']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/leads/{lead['lead_id']}"
    assert "Click Me" in client.get(f"/leads/{lead['lead_id']}").text
    settings_page = client.get(f"/admin/tenants/{t['id']}")
    assert 'name="hours_start"' in settings_page.text and f"/leads/{lead['lead_id']}" in settings_page.text


def test_only_agency_can_delete_lead(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    owner = make_user("boss@alpha.test", tenant_id=t["id"], role="owner", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), owner["id"]))
    lid = _intake(client, t, name="Test Lead", phone="7055550123")["lead_id"]

    # Client owner: no button, request refused.
    client.post("/login", data={"email": "boss@alpha.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    csrf = csrf_from(client)
    assert "Delete lead" not in client.get(f"/leads/{lid}").text
    assert client.post(f"/leads/{lid}/delete", data={"csrf_token": csrf}).status_code == 403
    client.post("/logout", data={"csrf_token": csrf})

    # Agency admin: button present, delete works, history gone, audit written.
    admin = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), admin["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    csrf = csrf_from(client)
    client.post("/switch-tenant", data={"tenant_id": str(t["id"]), "csrf_token": csrf})
    assert "Delete lead" in client.get(f"/leads/{lid}").text
    r = client.post(f"/leads/{lid}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "notice=Lead+deleted" in r.headers["location"]
    assert admin_conn.execute("SELECT count(*) AS n FROM lead WHERE id = %s", (lid,)).fetchone()["n"] == 0
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s", (lid,)).fetchone()["n"] == 0
    assert admin_conn.execute("SELECT count(*) AS n FROM audit WHERE action = 'lead_deleted'").fetchone()["n"] == 1
