"""Twilio webhooks: signature checks, reply codes, lead replies, STOP, and new leads by text."""

from app.routers.twilio import twilio_signature

TOKEN = "test-twilio-token"
URL = "http://testserver/v1/twilio/sms"


def _post(client, params, *, url=URL, sign=True, path="/v1/twilio/sms"):
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = twilio_signature(url, params, TOKEN)
    return client.post(path, data=params, headers=headers)


def _setup(make_tenant, make_user, admin_conn, twilio_number="+17055550000"):
    t = make_tenant("roofco", twilio_number=twilio_number)
    owner = make_user("owner@roofco.test", tenant_id=t["id"], role="owner", name="Dave Owner", mobile="+17055551000")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    return admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone(), owner


def test_bad_or_missing_signature_is_rejected(client, make_tenant, make_user, admin_conn):
    _setup(make_tenant, make_user, admin_conn)
    params = {"From": "+17055551000", "To": "+17055550000", "Body": "1", "MessageSid": "SM1"}
    assert _post(client, params, sign=False).status_code == 403
    r = client.post("/v1/twilio/sms", data=params, headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403
    # Twilio signs the public https URL; we accept that even though the test client speaks http.
    r = _post(client, params, url="https://testserver/v1/twilio/sms")
    assert r.status_code == 200


def test_owner_reply_code_logs_a_touch_and_confirms(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    r = _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "2", "MessageSid": "SM2"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert "<Message>Logged: texted Mike (A001)" in r.text
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "contacted"
    inbound = admin_conn.execute("SELECT * FROM message_log WHERE direction = 'in'").fetchone()
    assert inbound["from_addr"] == "+17055551000" and inbound["user_id"] == owner["id"]


def test_lead_reply_is_logged_and_client_alerted(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    r = _post(client, {"From": "+17055550142", "To": "+17055550000", "Body": "Sure, Tuesday works", "MessageSid": "SM3"})
    assert r.status_code == 200 and "<Message>" not in r.text  # nothing automatic back to the lead
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'lead_replied'", (lead_id,)).fetchone()["n"] == 1
    assert any('Mike (A001) replied: "Sure, Tuesday works"' in m["body"] and m["to"] == "+17055551000" for m in outbox.sms)
    assert admin_conn.execute("SELECT last_reply_at FROM lead WHERE id = %s", (lead_id,)).fetchone()["last_reply_at"] is not None


def test_stop_opts_the_number_out(client, make_tenant, make_user, admin_conn):
    t, _ = _setup(make_tenant, make_user, admin_conn)
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    r = _post(client, {"From": "+17055550142", "To": "+17055550000", "Body": "STOP", "MessageSid": "SM4"})
    assert r.status_code == 200
    assert admin_conn.execute("SELECT 1 FROM sms_opt_out WHERE tenant_id = %s AND phone_e164 = '+17055550142'", (t["id"],)).fetchone()
    assert admin_conn.execute("SELECT sms_opt_out FROM lead WHERE id = %s", (lead_id,)).fetchone()["sms_opt_out"] is True
    _post(client, {"From": "+17055550142", "To": "+17055550000", "Body": "START", "MessageSid": "SM5"})
    assert admin_conn.execute("SELECT sms_opt_out FROM lead WHERE id = %s", (lead_id,)).fetchone()["sms_opt_out"] is False


def test_unknown_number_becomes_a_lead(client, make_tenant, make_user, admin_conn, outbox):
    t, _ = _setup(make_tenant, make_user, admin_conn)
    r = _post(client, {"From": "+17055550177", "To": "+17055550000", "Body": "Hi, do you do metal roofs?", "MessageSid": "SM6"})
    assert r.status_code == 200
    lead = admin_conn.execute("SELECT * FROM lead WHERE tenant_id = %s", (t["id"],)).fetchone()
    assert lead["channel"] == "inbound_text" and lead["phone_e164"] == "+17055550177" and lead["message"] == "Hi, do you do metal roofs?"
    assert any("New lead" in m["body"] and "via Unknown" in m["body"] for m in outbox.sms)


def test_text_to_agency_number_from_stranger(client, make_tenant, make_user, admin_conn):
    _setup(make_tenant, make_user, admin_conn)
    r = _post(client, {"From": "+16475550000", "To": "+14165550000", "Body": "hello", "MessageSid": "SM7"})
    assert r.status_code == 200 and "Reply codes only work" in r.text
    assert admin_conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0


def test_delivery_status_updates_the_log(client, make_tenant, make_user, admin_conn, outbox):
    t, _ = _setup(make_tenant, make_user, admin_conn)
    client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"})
    row = admin_conn.execute("SELECT provider_id FROM message_log WHERE channel = 'sms' LIMIT 1").fetchone()
    params = {"MessageSid": row["provider_id"], "MessageStatus": "delivered"}
    r = client.post("/v1/twilio/status", data=params, headers={"X-Twilio-Signature": twilio_signature("http://testserver/v1/twilio/status", params, TOKEN)})
    assert r.status_code == 204
    assert admin_conn.execute("SELECT status FROM message_log WHERE provider_id = %s", (row["provider_id"],)).fetchone()["status"] == "delivered"
