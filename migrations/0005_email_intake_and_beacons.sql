-- 0005_email_intake_and_beacons.sql
-- Two ways in for sites that can't post to a webhook (RealtyNinja, Wix, Squarespace):
--   * leads by email: the site BCCs its lead notification to a per-client address in the CRM mailbox;
--   * attribution beacons: wmt.js reports the ad details at the moment a form is submitted, keyed by
--     the email/phone typed into it, and the lead that arrives later (by any route) picks them up.

-- A public token for the beacon URL, safe to put in page source (it can't create leads).
ALTER TABLE tenant ADD COLUMN public_key text NOT NULL DEFAULT encode(gen_random_bytes(8), 'hex');
CREATE UNIQUE INDEX tenant_public_key_idx ON tenant (public_key);

CREATE TABLE attribution_beacon (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    email_lower text,
    phone_e164  text,
    name        text,
    attribution jsonb NOT NULL DEFAULT '{}'::jsonb,
    page_url    text,
    ip          inet,
    user_agent  text,
    lead_id     uuid REFERENCES lead(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attribution_beacon_email_idx ON attribution_beacon (tenant_id, email_lower, created_at DESC) WHERE email_lower IS NOT NULL;
CREATE INDEX attribution_beacon_phone_idx ON attribution_beacon (tenant_id, phone_e164, created_at DESC) WHERE phone_e164 IS NOT NULL;
ALTER TABLE attribution_beacon ENABLE ROW LEVEL SECURITY;
ALTER TABLE attribution_beacon FORCE ROW LEVEL SECURITY;
CREATE POLICY attribution_beacon_isolation ON attribution_beacon
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());
GRANT SELECT, INSERT, UPDATE, DELETE ON attribution_beacon TO crm_app;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE attribution_beacon_id_seq TO crm_app;

-- Every message the mailbox poller looked at, so a message is never turned into two leads.
CREATE TABLE inbound_email (
    message_id  text PRIMARY KEY,
    tenant_id   uuid REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id     uuid REFERENCES lead(id) ON DELETE SET NULL,
    from_addr   text,
    to_addr     text,
    subject     text,
    status      text NOT NULL,              -- lead | duplicate | ignored | rejected | error
    error       text,
    received_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX inbound_email_tenant_idx ON inbound_email (tenant_id, created_at DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON inbound_email TO crm_app;
