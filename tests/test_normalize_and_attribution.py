from app.models import Channel, Platform
from app.services import attribution
from app.services.normalize import format_phone_display, normalize_email, normalize_phone, postal_fsa


def test_phone_variants_normalize_to_e164():
    for raw in ["(705) 555-0142", "705.555.0142", "705 555 0142 ext 2", "+1 705 555 0142", "17055550142", "+17055550142"]:
        assert normalize_phone(raw) == "+17055550142", raw


def test_phone_junk_rejected():
    assert normalize_phone("call me") is None
    assert normalize_phone("123") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_phone_display():
    assert format_phone_display("+17055550142") == "(705) 555-0142"


def test_email_normalize():
    assert normalize_email("  Mike.R@Example.COM ") == "Mike.R@example.com"
    assert normalize_email("<mike@example.com>") == "mike@example.com"
    assert normalize_email("not an email") is None


def test_postal_fsa():
    assert postal_fsa("12 Birch Lane, Bracebridge ON P1L 1A1") == "P1L"
    assert postal_fsa("Bracebridge, ON p1l1a1") == "P1L"
    assert postal_fsa("no code here") is None


def test_flatten_elementor_advanced_shape():
    raw = {
        "form": {"id": "a1b2", "name": "Quote request"},
        "fields": {
            "name": {"id": "name", "type": "text", "title": "Name", "value": "Mike Reynolds", "raw_value": "Mike Reynolds"},
            "email": {"id": "email", "type": "email", "title": "Email", "value": "mike@example.com"},
            "phone": {"value": "705 555 0142"},
            "message": {"value": "Need a quote"},
            "gclid": {"value": "Cj0KCQ-demo"},
        },
        "meta": {"page_url": {"title": "Page URL", "value": "https://example.com/roofing?utm_source=google&utm_medium=cpc"}, "remote_ip": {"value": "1.2.3.4"}},
    }
    f = attribution.interpret(raw)
    assert f.contact_name == "Mike Reynolds"
    assert f.email == "mike@example.com"
    assert f.phone_raw == "705 555 0142"
    assert f.message == "Need a quote"
    assert f.form_name == "Quote request"
    assert f.attribution["gclid"] == "Cj0KCQ-demo"
    assert f.attribution["landing_page"].startswith("https://example.com/roofing")
    assert f.attribution["ip"] == "1.2.3.4"
    assert f.platform == Platform.GOOGLE_ADS


def test_flatten_bracket_keys_from_form_encoded():
    raw = {"fields[name][value]": "Dana", "fields[name][title]": "Name", "fields[tel][value]": "7055550177", "form[name]": "Contact"}
    f = attribution.interpret(raw)
    assert f.contact_name == "Dana"
    assert f.phone_raw == "7055550177"
    assert f.form_name == "Contact"


def test_flatten_wpforms_list_shape():
    raw = {"fields": [{"name": "Name", "value": "Priya S"}, {"name": "Phone", "value": "705-555-0199"}, {"label": "Service Needed", "value": "Repair"}]}
    f = attribution.interpret(raw)
    assert f.contact_name == "Priya S"
    assert f.phone_raw == "705-555-0199"
    assert f.job_type == "Repair"


def test_first_last_name_join_and_city_postal_append():
    f = attribution.interpret({"first_name": "Tom", "last_name": "Bellamy", "address": "9 Pine St", "city": "Toronto", "province": "ON", "postal_code": "M4E 1A1"})
    assert f.contact_name == "Tom Bellamy"
    assert f.contact_address == "9 Pine St, Toronto, ON M4E 1A1"


def test_tenant_field_map_overrides():
    f = attribution.interpret({"your-cell": "705 555 0100", "where": "Huntsville"}, field_map={"your-cell": "phone", "where": "job_address"})
    assert f.phone_raw == "705 555 0100"
    assert f.job_address == "Huntsville"


def test_wmt_attr_blob_is_merged():
    f = attribution.interpret({"name": "C", "phone": "7055550111", "wmt_attr": '{"fbclid": "abc", "utm_source": "facebook", "utm_medium": "paid"}'})
    assert f.attribution["fbclid"] == "abc"
    assert f.platform == Platform.META_ADS


def test_platform_inference_table():
    cases = [
        ({"gclid": "x"}, Channel.WEBSITE_FORM, Platform.GOOGLE_ADS),
        ({"wbraid": "x"}, Channel.WEBSITE_FORM, Platform.GOOGLE_ADS),
        ({"fbclid": "x"}, Channel.WEBSITE_FORM, Platform.META_ADS),
        ({"utm_source": "google", "utm_medium": "cpc"}, Channel.WEBSITE_FORM, Platform.GOOGLE_ADS),
        ({"utm_source": "facebook", "utm_medium": "paid_social"}, Channel.WEBSITE_FORM, Platform.META_ADS),
        ({"utm_source": "instagram"}, Channel.WEBSITE_FORM, Platform.REFERRAL),
        ({"referrer": "https://www.google.com/"}, Channel.WEBSITE_FORM, Platform.ORGANIC),
        ({"referrer": "https://www.yellowpages.ca/"}, Channel.WEBSITE_FORM, Platform.REFERRAL),
        ({"landing_page": "https://example.com/"}, Channel.WEBSITE_FORM, Platform.DIRECT),
        ({}, Channel.WEBSITE_FORM, Platform.UNKNOWN),
        ({}, Channel.META_LEAD_AD, Platform.META_ADS),
        ({}, Channel.GOOGLE_LEAD_FORM, Platform.GOOGLE_ADS),
        ({}, Channel.CALL, Platform.DIRECT),
    ]
    for attr, channel, expected in cases:
        assert attribution.infer_platform(channel, attr) == expected, (attr, channel)


def test_channel_from_payload():
    f = attribution.interpret({"channel": "call", "phone": "+17055550177", "callrail_call_id": "CR1"})
    assert f.channel == Channel.CALL
    assert f.attribution["callrail_call_id"] == "CR1"
    assert f.platform == Platform.DIRECT
