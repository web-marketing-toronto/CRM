import re
from decimal import Decimal

import pyotp

from app.auth import crypto
from app.services import importer

CSV = """Name,Cell Phone,Email,Job Address,Service,Stage,Amount,Source,Date,Notes
Mike Reynolds,(705) 555-0142,mike@example.com,"12 Birch Lane, Bracebridge",Roof replacement,Closed Won,"$8,500",Google Ads,2026-09-02,Metal roof
Dana Whitfield,705-555-0177,,48 Lakeshore Rd,Repair,Quote sent,,Facebook,09/14/2026,
Priya Sandhu,,priya@example.com,Huntsville,Repair,Contacted,,Referral,2026-10-01,Called twice
No Contact,,,Somewhere,Repair,New,,,,
Mike Again,705 555 0142,mike2@example.com,Dup row,Repair,New,,,,
Weird Status,705-555-0199,,Port Carling,Skylight,Thinking about it,,Website,2026-10-10,
"""


def test_parse_and_guess_mapping():
    parsed = importer.parse(CSV.encode("utf-8"), "leads.csv")
    assert parsed.headers[:3] == ["Name", "Cell Phone", "Email"]
    assert len(parsed.rows) == 6
    m = importer.guess_map(parsed.headers)
    assert m["Name"] == "contact_name"
    assert m["Cell Phone"] == "phone"
    assert m["Email"] == "email"
    assert m["Job Address"] == "job_address"
    assert m["Service"] == "job_type"
    assert m["Stage"] == "status"
    assert m["Amount"] == "deal_value"
    assert m["Source"] == "source"
    assert m["Date"] == "created_at"
    assert m["Notes"] in ("message", "notes2")


def test_status_guesses_and_helpers():
    assert importer.guess_status("Closed Won") == "won"
    assert importer.guess_status("Quote sent") == "quoted"
    assert importer.guess_status("Contacted") == "contacted"
    assert importer.guess_status("New") == "new"
    assert importer.guess_status("Thinking about it") == "review"
    assert importer.parse_money("$8,500") == Decimal("8500")
    assert importer.parse_money("n/a") is None
    assert importer.parse_date("09/14/2026").month == 9
    assert importer.parse_date("2026-10-01", "America/Toronto").day == 1
    assert importer.parse_date("2026-10-01", "America/Toronto").hour == 12
    assert importer.platform_from_text("Google Ads").value == "google_ads"
    assert importer.platform_from_text("Facebook").value == "meta_ads"


def test_excel_semicolon_and_cp1252():
    data = "Nom;Téléphone\nJosé;705 555 0101\n".encode("cp1252")
    parsed = importer.parse(data)
    assert parsed.headers == ["Nom", "Téléphone"] and parsed.rows[0]["Nom"] == "José"


def _agency_login(client, admin_conn, make_user):
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    page = client.get("/admin")
    return re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.text).group(1)


def test_import_flow_end_to_end(client, make_tenant, make_user, admin_conn):
    t = make_tenant("alpha")
    # One lead already exists with Mike's number, created recently: the import should merge into it.
    client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike R (web)", "phone": "705 555 0142"})
    csrf = _agency_login(client, admin_conn, make_user)

    r = client.post(f"/admin/tenants/{t['id']}/import", data={"csrf_token": csrf}, files={"file": ("leads.csv", CSV.encode(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303
    batch_url = r.headers["location"]
    page = client.get(batch_url)
    assert page.status_code == 200
    assert "Closed Won" in page.text and "Thinking about it" in page.text
    # Dry run on the page: Mike merges into the existing lead, "No Contact" and the duplicate row are skipped.
    assert "no usable phone number or email" in page.text
    assert "duplicate of an earlier row" in page.text

    form = {"csrf_token": csrf, "action": "commit"}
    for h, target in importer.guess_map(importer.parse(CSV.encode()).headers).items():
        form[f"map::{h}"] = target
    form.update({"status::Closed Won": "won", "status::Quote sent": "quoted", "status::Contacted": "contacted", "status::New": "new", "status::Thinking about it": "review"})
    r = client.post(batch_url, data=form, follow_redirects=False)
    assert r.status_code == 303 and "Imported+3+leads" in r.headers["location"]

    rows = admin_conn.execute("SELECT contact_name, status, deal_value, flags, channel, platform, created_at FROM lead WHERE tenant_id = %s ORDER BY ref_code", (t["id"],)).fetchall()
    by_name = {r["contact_name"]: r for r in rows}
    assert len(rows) == 4  # the web lead + 3 imported
    assert by_name["Dana Whitfield"]["status"] == "quoted" and by_name["Dana Whitfield"]["channel"] == "import"
    assert by_name["Dana Whitfield"]["platform"] == "meta_ads"
    assert by_name["Priya Sandhu"]["status"] == "contacted" and by_name["Priya Sandhu"]["created_at"].month == 10
    assert by_name["Weird Status"]["flags"].get("needs_review") is True and by_name["Weird Status"]["status"] == "contacted"
    # Mike merged: a note on the existing lead, no new row, existing name kept.
    assert "Mike Reynolds" not in by_name and by_name["Mike R (web)"]["status"] == "new"
    notes = admin_conn.execute("SELECT count(*) AS n FROM activity WHERE type = 'import' AND tenant_id = %s", (t["id"],)).fetchone()["n"]
    assert notes == 4  # 3 created + 1 merge note
    batch = admin_conn.execute("SELECT status, raw_text, result FROM import_batch").fetchone()
    assert batch["status"] == "committed" and batch["raw_text"] is None and batch["result"]["created"] == 3


def test_imported_leads_sit_in_review_not_needs_action(client, make_tenant, make_user, admin_conn):
    t = make_tenant("beta")
    csrf = _agency_login(client, admin_conn, make_user)
    small = "Name,Phone,Status\nPat Lee,705 555 0111,Hmm\n"
    r = client.post(f"/admin/tenants/{t['id']}/import", data={"csrf_token": csrf}, files={"file": ("x.csv", small.encode(), "text/csv")}, follow_redirects=False)
    batch_url = r.headers["location"]
    client.post(batch_url, data={"csrf_token": csrf, "action": "commit", "map::Name": "contact_name", "map::Phone": "phone", "map::Status": "status", "status::Hmm": "review"}, follow_redirects=False)
    client.post("/switch-tenant", data={"tenant_id": str(t["id"]), "csrf_token": csrf})
    needs = client.get("/leads?view=needs_action").text
    review = client.get("/leads?view=imported").text
    assert "Pat Lee" not in needs and "Pat Lee" in review and "To review" in review
    lead_id = re.search(r'href="/leads/([0-9a-f-]{36})"', review).group(1)
    client.post(f"/leads/{lead_id}/status", data={"status": "quoted", "csrf_token": csrf}, headers={"HX-Request": "true"})
    flags = admin_conn.execute("SELECT flags FROM lead WHERE id = %s", (lead_id,)).fetchone()["flags"]
    assert "needs_review" not in flags
