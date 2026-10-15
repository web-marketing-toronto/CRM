-- 0003_messaging.sql
-- Outbound and inbound messages, and the lead columns the follow-up engine needs.

CREATE TABLE message_log (
    id           bigserial PRIMARY KEY,
    tenant_id    uuid REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id      uuid REFERENCES lead(id) ON DELETE SET NULL,
    user_id      uuid REFERENCES app_user(id) ON DELETE SET NULL,
    reminder_id  uuid REFERENCES reminder(id) ON DELETE SET NULL,
    direction    text NOT NULL,              -- out | in
    channel      text NOT NULL,              -- sms | email
    kind         text NOT NULL,              -- alert | reminder | digest | escalation | auto_reply | lead_text | inbound | confirm
    to_addr      text NOT NULL,
    from_addr    text,
    subject      text,
    body         text NOT NULL,
    provider     text NOT NULL,
    provider_id  text,
    status       text NOT NULL DEFAULT 'queued',  -- queued | sent | delivered | failed | received
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX message_log_lead_idx ON message_log (lead_id, created_at DESC);
CREATE INDEX message_log_tenant_idx ON message_log (tenant_id, created_at DESC);
CREATE INDEX message_log_provider_idx ON message_log (provider_id) WHERE provider_id IS NOT NULL;

ALTER TABLE message_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_log FORCE ROW LEVEL SECURITY;
CREATE POLICY message_log_isolation ON message_log
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());
GRANT SELECT, INSERT, UPDATE, DELETE ON message_log TO crm_app;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE message_log_id_seq TO crm_app;

ALTER TABLE lead
    ADD COLUMN appointment_at timestamptz,
    ADD COLUMN sms_opt_out    boolean NOT NULL DEFAULT false,
    ADD COLUMN last_reply_at  timestamptz,
    ADD COLUMN cadence_base   timestamptz,
    ADD COLUMN snoozed_until  timestamptz;

-- Phone numbers that texted STOP to a client's number; the CRM must never text them again.
CREATE TABLE sms_opt_out (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    phone_e164 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, phone_e164)
);
ALTER TABLE sms_opt_out ENABLE ROW LEVEL SECURITY;
ALTER TABLE sms_opt_out FORCE ROW LEVEL SECURITY;
CREATE POLICY sms_opt_out_isolation ON sms_opt_out
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());
GRANT SELECT, INSERT, UPDATE, DELETE ON sms_opt_out TO crm_app;

ALTER TYPE activity_type ADD VALUE IF NOT EXISTS 'appointment';
ALTER TYPE activity_type ADD VALUE IF NOT EXISTS 'escalation';
