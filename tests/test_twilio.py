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
    assert any("[A001] Mike: Sure, Tuesday works" in m["body"] and m["to"] == "+17055551000" and m["from"] == "+17055550000" for m in outbox.sms)
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


def test_lead_text_is_relayed_to_the_assignees_phone_and_answers_go_back(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    # Lead texts the client's number.
    _post(client, {"From": "+17055550142", "To": "+17055550000", "Body": "Sure, Tuesday works", "MessageSid": "R1"})
    relayed = [m for m in outbox.sms if m["to"] == "+17055551000"]
    assert len(relayed) == 1 and relayed[0]["from"] == "+17055550000" and relayed[0]["kind"] == "relay"
    assert relayed[0]["body"].startswith("[A001] Mike: Sure, Tuesday works")
    assert not [m for m in outbox.sms if m["to"] == "+17055551000" and m["from"] == "+14165550000"]  # no second alert from the agency number
    # Owner answers from their phone, no ref needed: it goes to Mike from the client's number.
    outbox.clear()
    r = _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "Great, see you Tuesday at 10", "MessageSid": "R2"})
    assert r.status_code == 200 and "<Message>" not in r.text
    out = [m for m in outbox.sms if m["to"] == "+17055550142"]
    assert len(out) == 1 and out[0]["from"] == "+17055550000" and out[0]["body"] == "Great, see you Tuesday at 10"
    act = admin_conn.execute("SELECT body, detail FROM activity WHERE lead_id = %s AND type = 'text_sent' ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    assert act["body"] == "Great, see you Tuesday at 10" and act["detail"]["via"] == "phone"
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "contacted"
    # Codes still work on the same number, and a ref prefix picks a different lead.
    r = _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "4", "MessageSid": "R3"})
    assert "Snoozed Mike" in r.text
    other = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Sara", "phone": "7055550155"}).json()["lead_id"]
    outbox.clear()
    _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "A002 Hi Sara, this is Dan. When suits you?", "MessageSid": "R4"})
    out = [m for m in outbox.sms if m["to"] == "+17055550155"]
    assert len(out) == 1 and out[0]["body"] == "Hi Sara, this is Dan. When suits you?"
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'text_sent'", (other,)).fetchone()["n"] == 1


def test_relay_without_context_asks_for_a_ref(client, make_tenant, make_user, admin_conn, outbox):
    _setup(make_tenant, make_user, admin_conn)
    r = _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "Hello there", "MessageSid": "R5"})
    assert "Which lead is that for" in r.text and not outbox.sms


def test_relay_off_means_agency_alert_and_codes_only(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    admin_conn.execute("""UPDATE tenant SET settings = settings || '{"sms": {"relay_replies": false}}' WHERE id = %s""", (t["id"],))
    client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"})
    outbox.clear()
    _post(client, {"From": "+17055550142", "To": "+17055550000", "Body": "Yes please", "MessageSid": "R6"})
    alert = [m for m in outbox.sms if m["to"] == "+17055551000"]
    assert len(alert) == 1 and alert[0]["from"] == "+14165550000" and "replied" in alert[0]["body"]
    r = _post(client, {"From": "+17055551000", "To": "+17055550000", "Body": "Great, see you Tuesday", "MessageSid": "R7"})
    assert r.text.count("<Message>") == 1 and "Codes:" in r.text  # treated as a (bad) code, nothing relayed


def test_calls_to_the_texting_number_ring_the_client(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    params = {"From": "+17055550142", "To": "+17055550000", "CallSid": "CA1", "CallStatus": "ringing"}
    r = client.post("/v1/twilio/voice", data=params, headers={"X-Twilio-Signature": twilio_signature("http://testserver/v1/twilio/voice", params, TOKEN)})
    assert r.status_code == 200 and '<Dial callerId="+17055550142" timeout="25">+17055551000</Dial>' in r.text
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'lead_replied'", (lead_id,)).fetchone()["n"] == 1
    assert not [m for m in outbox.sms if m["to"] == "+17055551000"]  # their phone rang; no extra alert
    # An explicit forwarding number wins over the owner's mobile.
    admin_conn.execute("""UPDATE tenant SET settings = settings || '{"sms": {"forward_calls_to": "+17055559000"}}' WHERE id = %s""", (t["id"],))
    r = client.post("/v1/twilio/voice", data=params, headers={"X-Twilio-Signature": twilio_signature("http://testserver/v1/twilio/voice", params, TOKEN)})
    assert ">+17055559000</Dial>" in r.text
    # Unknown number: a polite message, no crash.
    p2 = {"From": "+16475550000", "To": "+17055558888", "CallSid": "CA2"}
    r = client.post("/v1/twilio/voice", data=p2, headers={"X-Twilio-Signature": twilio_signature("http://testserver/v1/twilio/voice", p2, TOKEN)})
    assert r.status_code == 200 and "<Say" in r.text and "<Dial" not in r.text


def test_click_to_call_rings_the_client_then_dials_the_lead_from_the_business_number(client, make_tenant, make_user, admin_conn, outbox, login):
    from app.services import messaging
    from tests.conftest import csrf_from

    t, owner = _setup(make_tenant, make_user, admin_conn)
    member = make_user("pat@roofco.test", tenant_id=t["id"], role="member", name="Pat", mobile="+17055552000")
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    login("pat@roofco.test")
    csrf = csrf_from(client)
    page = client.get(f"/leads/{lead_id}")
    assert f'action="/leads/{lead_id}/call"' in page.text and 'href="tel:' not in page.text  # no direct-dial from a personal phone
    assert 'href="sms:+17055550000?&body=A001' in page.text and 'href="sms:+17055550142"' not in page.text
    r = client.post(f"/leads/{lead_id}/call", data={"csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Called through the CRM" in r.text
    assert len(messaging.outbox.calls) == 1
    call = messaging.outbox.calls[0]
    assert call["to"] == "+17055552000" and call["from"] == "+17055550000" and "/v1/twilio/connect/" in call["url"]
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "contacted"
    # Twilio fetches the connect URL once Pat answers: dial Mike showing the business number.
    path = call["url"].replace("http://testserver", "")
    params = {"CallSid": "CA9", "CallStatus": "in-progress"}
    r = client.post(path, data=params, headers={"X-Twilio-Signature": twilio_signature("http://testserver" + path, params, TOKEN)})
    assert r.status_code == 200 and '<Dial callerId="+17055550000" timeout="30">+17055550142</Dial>' in r.text and "Connecting you to Mike" in r.text
    # Without a mobile on the account the button explains itself.
    nomobile = make_user("nm@roofco.test", tenant_id=t["id"], role="member", name="NoMobile")
    client.post("/logout", data={"csrf_token": csrf})
    login("nm@roofco.test")
    csrf = csrf_from(client)
    page = client.get(f"/leads/{lead_id}")
    assert 'href="tel:+17055550142"' in page.text  # falls back to a plain dial when we can't ring them
    assert member["id"] and nomobile["id"]


def test_alerts_point_at_the_app_not_the_raw_number_when_the_client_has_a_business_number(client, make_tenant, make_user, admin_conn, outbox):
    t, owner = _setup(make_tenant, make_user, admin_conn)
    client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"})
    alert = next(m for m in outbox.sms if m["to"] == "+17055551000")
    assert "Call or text from http://testserver/leads/" in alert["body"] and "(705) 555-0142" not in alert["body"]
