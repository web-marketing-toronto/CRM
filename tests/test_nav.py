"""Navigation: clients get a bottom tab bar on phones, agency staff a menu."""

import re

import pyotp

from app.auth import crypto


def test_client_gets_tab_bar(client, make_tenant, make_user, login):
    t = make_tenant("roofco", name="North Ridge Roofing")
    make_user("pat@roofco.test", tenant_id=t["id"], role="member", name="Pat Lee")
    login("pat@roofco.test")
    html = client.get("/reports").text
    assert '<body class="nav-tabs"' in html and 'class="menu-toggle"' not in html
    assert '<nav class="tabbar"' in html and '<a href="/reports" class="current">' in html and 'href="/account" class=""' in html
    assert "nav-group" not in html  # group labels are only for the agency menu
    assert "Pat</a>" in html  # the top-bar links still exist for desktop


def test_agency_gets_menu_with_groups(client, make_user, make_tenant, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing")
    u = make_user("boss@agency.test", role="agency_admin", totp_enabled=True, name="Jeff Fidler")
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "boss@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    html = client.get("/admin/team").text
    assert '<body class="nav-menu"' in html and 'class="menu-toggle"' in html and 'aria-controls="mainnav"' in html
    assert '<span class="nav-group">Signed in as</span>' in html and '<a href="/admin/team" class="current">' in html
    assert 'class="tabbar"' not in html and "North Ridge Roofing</span>" not in html
    # Viewing a client adds that client's section to the menu.
    client.post("/switch-tenant", data={"tenant_id": str(t["id"]), "next": "/leads", "csrf_token": csrf})
    html = client.get("/leads").text
    assert '<span class="nav-group">North Ridge Roofing</span><a href="/leads" class="current">' in html and 'class="tabbar"' not in html
