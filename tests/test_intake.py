from datetime import timedelta


def _url(tenant):
    return f"/v1/intake/{tenant['slug']}/{tenant['intake_key']}"


def test_json_intake_creates_lead_with_source(client, make_tenant, admin_conn):
    t = make_tenant("alpha")
    r = client.post(_url(t), json={
        "name": "Mike Reynolds", "phone": "(705) 555-0142", "email": "Mike@Example.com",
        "job_address": "12 Birch Lane, Bracebridge, ON P1L 1A1", "job_type": "Roof replacement",
        "message": "Need a quote", "gclid": "Cj0-demo", "utm_campaign": "Roofing - Muskoka",
        "landing_page": "https://example.com/roofing",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["platform"] == "google_ads" and body["ref"] == "A001"
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (body["lead_id"],)).fetchone()
    assert lead["phone_e164"] == "+17055550142"
    assert lead["email"] == "Mike@example.com"
    assert lead["status"] == "new" and lead["channel"] == "website_form" and lead["platform"] == "google_ads"
    assert lead["campaign_name"] == "Roofing - Muskoka"
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["gclid"] == "Cj0-demo" and src["landing_page"] == "https://example.com/roofing"
    assert src["raw_payload"]["name"] == "Mike Reynolds"
    types = [a["type"] for a in admin_conn.execute("SELECT type FROM activity WHERE lead_id = %s", (lead["id"],))]
    assert "created" in types


def test_form_encoded_intake_elementor_style(client, make_tenant, admin_conn):
    t = make_tenant("beta")
    r = client.post(_url(t), data={
        "form[name]": "Quote form", "fields[name][value]": "Dana W", "fields[name][title]": "Name",
        "fields[phone][value]": "705 555 0177", "fields[message][value]": "Leaking roof",
        "meta[page_url][value]": "https://beta.example/contact?utm_source=facebook&utm_medium=paid",
        "fbclid": "IwAR-demo",
    })
    assert r.status_code == 201, r.text
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()
    assert lead["contact_name"] == "Dana W" and lead["platform"] == "meta_ads"
    src = admin_conn.execute("SELECT form_name, fbclid FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["form_name"] == "Quote form" and src["fbclid"] == "IwAR-demo"


def test_key_in_header_and_bad_key(client, make_tenant):
    t = make_tenant("gamma")
    ok = client.post(f"/v1/intake/{t['slug']}", json={"name": "A", "phone": "7055550100"}, headers={"X-Intake-Key": t["intake_key"]})
    assert ok.status_code == 201
    bad = client.post(f"/v1/intake/{t['slug']}/wrongkey", json={"name": "A", "phone": "7055550100"})
    assert bad.status_code == 404
    unknown = client.post("/v1/intake/nobody/key", json={"name": "A", "phone": "7055550100"})
    assert unknown.status_code == 404


def test_empty_submission_rejected(client, make_tenant):
    t = make_tenant("delta")
    r = client.post(_url(t), json={"message": "hello"})
    assert r.status_code == 422


def test_duplicate_within_window_attaches_to_existing(client, make_tenant, admin_conn):
    t = make_tenant("eps")
    first = client.post(_url(t), json={"name": "Priya", "phone": "705-555-0199"}).json()
    second = client.post(_url(t), json={"name": "Priya Sandhu", "email": "priya@example.com", "phone": "+1 705 555 0199", "message": "Following up"})
    assert second.status_code == 200
    body = second.json()
    assert body["created"] is False and body["lead_id"] == first["lead_id"] and body["ref"] == "A001"
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (first["lead_id"],)).fetchone()
    assert lead["email"] == "priya@example.com"  # enriched blank
    assert lead["contact_name"] == "Priya"       # client's existing value kept
    assert admin_conn.execute("SELECT count(*) AS n FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()["n"] == 2
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'duplicate_submission'", (lead["id"],)).fetchone()["n"] == 1


def test_duplicate_reopens_closed_lead(client, make_tenant, admin_conn):
    t = make_tenant("zeta")
    first = client.post(_url(t), json={"name": "Tom", "phone": "7055550123"}).json()
    admin_conn.execute("UPDATE lead SET status = 'lost', lost_reason = 'timing', lost_at = now() WHERE id = %s", (first["lead_id"],))
    r = client.post(_url(t), json={"name": "Tom", "phone": "7055550123"})
    assert r.json()["reopened"] is True
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (first["lead_id"],)).fetchone()["status"] == "new"


def test_duplicate_after_window_is_new_lead_linked(client, make_tenant, admin_conn):
    t = make_tenant("eta")
    first = client.post(_url(t), json={"name": "Carla", "phone": "7055550111"}).json()
    admin_conn.execute("UPDATE lead SET created_at = now() - %s WHERE id = %s", (timedelta(days=120), first["lead_id"]))
    r = client.post(_url(t), json={"name": "Carla", "phone": "7055550111"})
    assert r.status_code == 201 and r.json()["created"] is True and r.json()["ref"] == "A002"
    new = admin_conn.execute("SELECT previous_lead_id FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()
    assert str(new["previous_lead_id"]) == first["lead_id"]


def test_default_assignee_and_ref_sequence(client, make_tenant, make_user, admin_conn):
    t = make_tenant("theta")
    owner = make_user("owner@theta.test", tenant_id=t["id"], role="owner")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    refs = [client.post(_url(t), json={"name": f"L{i}", "phone": f"705555{1000 + i:04d}"}).json()["ref"] for i in range(3)]
    assert refs == ["A001", "A002", "A003"]
    assigned = admin_conn.execute("SELECT count(*) AS n FROM lead WHERE tenant_id = %s AND assigned_to = %s", (t["id"], owner["id"])).fetchone()["n"]
    assert assigned == 3


def test_service_area_flags_out_of_area_by_fsa_without_geocoder(client, make_tenant, admin_conn):
    t = make_tenant("iota", service_area={"fsas": ["P1L", "P1P"]})
    inside = client.post(_url(t), json={"name": "In", "phone": "7055550001", "job_address": "1 Main St, Bracebridge ON P1L 1A1"}).json()
    outside = client.post(_url(t), json={"name": "Out", "phone": "7055550002", "job_address": "9 Pine St, Toronto ON M4E 1A1"}).json()
    unknown = client.post(_url(t), json={"name": "Unk", "phone": "7055550003", "job_address": "Somewhere"}).json()
    assert inside["area"] == "in" and outside["area"] == "out" and unknown["area"] == "unknown"
    lead = admin_conn.execute("SELECT flags FROM lead WHERE id = %s", (outside["lead_id"],)).fetchone()
    assert lead["flags"].get("possibly_out_of_area") is True  # FSA match is never 'precise', so it flags rather than declines


def test_tenants_are_isolated_in_intake(client, make_tenant, admin_conn):
    a = make_tenant("isoa")
    b = make_tenant("isob")
    client.post(_url(a), json={"name": "Same Person", "phone": "7055550777"})
    r = client.post(_url(b), json={"name": "Same Person", "phone": "7055550777"})
    assert r.status_code == 201 and r.json()["created"] is True
    assert admin_conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 2


def test_inactive_tenant_rejects(client, make_tenant):
    t = make_tenant("kappa", is_active=False)
    assert client.post(_url(t), json={"name": "X", "phone": "7055550100"}).status_code == 404


def test_rejection_names_the_fields_it_saw(client, make_tenant):
    t = make_tenant("lam")
    r = client.post(_url(t), json={"Full Nmae": "Typo Field", "cellphone_no": "7055550100"})
    assert r.status_code == 422
    assert "Fields received: cellphone_no, full_nmae" in r.json()["detail"]
    r = client.post(_url(t), json={})
    assert r.status_code == 422 and "no fields at all" in r.json()["detail"]
