-- 0001_initial.sql
-- Core schema for the WMT contractor CRM.
--
-- Every tenant-scoped table carries tenant_id and is protected by row-level security.
-- The app sets two session variables per request:
--   app.tenant_id  -> the tenant the current user belongs to (or is acting as)
--   app.role       -> 'agency_admin' | 'owner' | 'member'
-- Policies allow a row when tenant_id matches, or when the role is agency_admin.
-- With nothing set, current_setting(..., true) is NULL and no rows are visible.
--
-- FORCE ROW LEVEL SECURITY makes the policies apply to the table owner too, so the
-- isolation does not depend on which database user happened to run the migrations.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------
CREATE TYPE user_role AS ENUM ('agency_admin', 'owner', 'member');

CREATE TYPE lead_status AS ENUM ('new', 'contacted', 'no_response', 'quoted', 'won', 'lost');

CREATE TYPE lead_channel AS ENUM (
    'website_form', 'call', 'meta_lead_ad', 'google_lead_form', 'inbound_text', 'manual', 'import'
);

CREATE TYPE lead_platform AS ENUM ('google_ads', 'meta_ads', 'organic', 'referral', 'direct', 'unknown');

CREATE TYPE area_status AS ENUM ('in', 'near', 'out', 'unknown');

CREATE TYPE activity_type AS ENUM (
    'created', 'call_attempt', 'text_sent', 'email_sent', 'lead_replied', 'note',
    'status_change', 'assignment', 'auto_message', 'reminder_sent', 'duplicate_submission',
    'conversion_sent', 'import'
);

CREATE TYPE lost_reason AS ENUM (
    'price', 'timing', 'out_of_area', 'went_elsewhere', 'no_response', 'spam', 'other'
);

-- ---------------------------------------------------------------------------
-- Tenants and users
-- ---------------------------------------------------------------------------
CREATE TABLE tenant (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,40}$'),
    name                text NOT NULL,
    timezone            text NOT NULL DEFAULT 'America/Toronto',
    intake_key          text NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(24), 'hex'),
    field_map           jsonb NOT NULL DEFAULT '{}'::jsonb,
    service_area        jsonb NOT NULL DEFAULT '{}'::jsonb,
    business_hours      jsonb NOT NULL DEFAULT '{"start": "07:00", "end": "19:00", "days": [1,2,3,4,5,6]}'::jsonb,
    job_types           text[] NOT NULL DEFAULT '{}',
    default_assignee_id uuid,
    twilio_number       text,
    settings            jsonb NOT NULL DEFAULT '{}'::jsonb,
    lead_seq            integer NOT NULL DEFAULT 0,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid REFERENCES tenant(id) ON DELETE CASCADE,
    email             text NOT NULL,
    email_lower       text GENERATED ALWAYS AS (lower(email)) STORED,
    name              text NOT NULL,
    mobile_e164       text,
    role              user_role NOT NULL,
    password_hash     text NOT NULL,
    totp_secret_enc   text,
    totp_enabled      boolean NOT NULL DEFAULT false,
    totp_last_counter bigint,
    backup_codes      jsonb NOT NULL DEFAULT '[]'::jsonb,
    notify_sms        boolean NOT NULL DEFAULT true,
    notify_email      boolean NOT NULL DEFAULT true,
    see_all_leads     boolean NOT NULL DEFAULT true,
    is_active         boolean NOT NULL DEFAULT true,
    must_set_password boolean NOT NULL DEFAULT false,
    failed_logins     integer NOT NULL DEFAULT 0,
    locked_until      timestamptz,
    last_login_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agency_admin_has_no_tenant CHECK (
        (role = 'agency_admin' AND tenant_id IS NULL) OR (role <> 'agency_admin' AND tenant_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX app_user_email_lower_idx ON app_user (email_lower);
CREATE INDEX app_user_tenant_idx ON app_user (tenant_id);

ALTER TABLE tenant
    ADD CONSTRAINT tenant_default_assignee_fk
    FOREIGN KEY (default_assignee_id) REFERENCES app_user(id) ON DELETE SET NULL;

CREATE TABLE session (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    tenant_id   uuid REFERENCES tenant(id) ON DELETE CASCADE,
    csrf_token  text NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
    mfa_passed  boolean NOT NULL DEFAULT false,
    ip          inet,
    user_agent  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz
);
CREATE INDEX session_user_idx ON session (user_id);

-- ---------------------------------------------------------------------------
-- Leads
-- ---------------------------------------------------------------------------
CREATE TABLE lead (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    ref_code         text NOT NULL,
    previous_lead_id uuid REFERENCES lead(id) ON DELETE SET NULL,
    status           lead_status NOT NULL DEFAULT 'new',
    assigned_to      uuid REFERENCES app_user(id) ON DELETE SET NULL,
    contact_name     text,
    company          text,
    phone_e164       text,
    phone_raw        text,
    email            text,
    email_lower      text GENERATED ALWAYS AS (lower(email)) STORED,
    contact_address  text,
    job_address      text,
    job_lat          double precision,
    job_lng          double precision,
    area_status      area_status NOT NULL DEFAULT 'unknown',
    job_type         text,
    message          text,
    channel          lead_channel NOT NULL,
    platform         lead_platform NOT NULL DEFAULT 'unknown',
    campaign_name    text,
    deal_value       numeric(12, 2),
    lost_reason      lost_reason,
    flags            jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_contact_at timestamptz,
    last_touch_at    timestamptz,
    next_action_at   timestamptz,
    quoted_at        timestamptz,
    won_at           timestamptz,
    lost_at          timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT won_needs_value CHECK (status <> 'won' OR deal_value IS NOT NULL),
    CONSTRAINT lost_needs_reason CHECK (status <> 'lost' OR lost_reason IS NOT NULL),
    UNIQUE (tenant_id, ref_code)
);
CREATE INDEX lead_tenant_status_idx ON lead (tenant_id, status, created_at DESC);
CREATE INDEX lead_tenant_phone_idx ON lead (tenant_id, phone_e164) WHERE phone_e164 IS NOT NULL;
CREATE INDEX lead_tenant_email_idx ON lead (tenant_id, email_lower) WHERE email_lower IS NOT NULL;
CREATE INDEX lead_assigned_idx ON lead (tenant_id, assigned_to);
CREATE INDEX lead_next_action_idx ON lead (tenant_id, next_action_at) WHERE status NOT IN ('won', 'lost');

-- Immutable capture of where the lead came from. Written once at intake, never edited.
CREATE TABLE lead_source (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id          uuid NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    channel          lead_channel NOT NULL,
    platform         lead_platform NOT NULL DEFAULT 'unknown',
    gclid            text,
    gbraid           text,
    wbraid           text,
    fbclid           text,
    fbc              text,
    fbp              text,
    meta_lead_id     text,
    google_lead_id   text,
    callrail_call_id text,
    utm_source       text,
    utm_medium       text,
    utm_campaign     text,
    utm_term         text,
    utm_content      text,
    campaign_id      text,
    adset_id         text,
    ad_id            text,
    landing_page     text,
    referrer         text,
    form_name        text,
    ip               inet,
    user_agent       text,
    raw_payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lead_source_lead_idx ON lead_source (lead_id);
CREATE INDEX lead_source_meta_lead_idx ON lead_source (tenant_id, meta_lead_id) WHERE meta_lead_id IS NOT NULL;
CREATE INDEX lead_source_callrail_idx ON lead_source (tenant_id, callrail_call_id) WHERE callrail_call_id IS NOT NULL;

-- Append-only record of everything that happened on a lead.
CREATE TABLE activity (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id     uuid NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    type        activity_type NOT NULL,
    actor_id    uuid REFERENCES app_user(id) ON DELETE SET NULL,
    actor_label text NOT NULL DEFAULT 'system',
    body        text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_lead_idx ON activity (lead_id, occurred_at DESC);
CREATE INDEX activity_tenant_time_idx ON activity (tenant_id, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Reminders, conversions, spend, audit
-- ---------------------------------------------------------------------------
CREATE TABLE reminder (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id         uuid NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    user_id         uuid REFERENCES app_user(id) ON DELETE SET NULL,
    rule            text NOT NULL,
    channel         text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    due_at          timestamptz NOT NULL,
    sent_at         timestamptz,
    acknowledged_at timestamptz,
    escalated_at    timestamptz,
    cancelled_at    timestamptz,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reminder_due_idx ON reminder (due_at) WHERE sent_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX reminder_lead_idx ON reminder (lead_id);

CREATE TABLE conversion_event (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    lead_id     uuid NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    platform    text NOT NULL,
    action      text NOT NULL,
    value       numeric(12, 2),
    currency    text NOT NULL DEFAULT 'CAD',
    status      text NOT NULL DEFAULT 'pending',
    attempts    integer NOT NULL DEFAULT 0,
    last_error  text,
    sent_at     timestamptz,
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lead_id, platform, action)
);
CREATE INDEX conversion_event_pending_idx ON conversion_event (status, created_at) WHERE status = 'pending';

CREATE TABLE ad_spend (
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    platform      lead_platform NOT NULL,
    campaign_id   text NOT NULL,
    campaign_name text,
    date          date NOT NULL,
    spend         numeric(12, 2) NOT NULL DEFAULT 0,
    clicks        integer NOT NULL DEFAULT 0,
    impressions   integer NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, platform, campaign_id, date)
);

CREATE TABLE audit (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid REFERENCES tenant(id) ON DELETE CASCADE,
    user_id     uuid REFERENCES app_user(id) ON DELETE SET NULL,
    action      text NOT NULL,
    target_type text,
    target_id   text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip          inet,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_tenant_time_idx ON audit (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tenant_updated_at BEFORE UPDATE ON tenant FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER app_user_updated_at BEFORE UPDATE ON app_user FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER lead_updated_at BEFORE UPDATE ON lead FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid AS $$
    SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
$$ LANGUAGE sql STABLE;

-- 'agency_admin' is a logged-in agency user; 'system' is the app itself running
-- intake, the reminder worker or the auth layer, where no tenant is known yet.
CREATE OR REPLACE FUNCTION app_is_privileged() RETURNS boolean AS $$
    SELECT coalesce(current_setting('app.role', true), '') IN ('agency_admin', 'system')
$$ LANGUAGE sql STABLE;

-- Tenant rows: a tenant user sees only their own tenant; privileged contexts see all.
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant
    USING (id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (id = app_tenant_id() OR app_is_privileged());

-- Users: tenant users see users in their tenant; privileged contexts see everyone.
ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY app_user_isolation ON app_user
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());

-- Sessions are only ever touched by the auth layer, which runs as 'system'.
ALTER TABLE session ENABLE ROW LEVEL SECURITY;
ALTER TABLE session FORCE ROW LEVEL SECURITY;
CREATE POLICY session_system_only ON session
    USING (app_is_privileged())
    WITH CHECK (app_is_privileged());

ALTER TABLE audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_isolation ON audit
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());

-- Tenant-scoped data tables share one policy shape.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['lead', 'lead_source', 'activity', 'reminder', 'conversion_event', 'ad_spend']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (tenant_id = app_tenant_id() OR app_is_privileged()) '
            'WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged())',
            t || '_isolation', t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Application role: the web app connects as crm_app, which owns nothing and
-- is therefore fully subject to the policies above. Login credentials for the
-- role are created by deploy/setup_gcp.sh (production) or tests/conftest.py.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') THEN
        CREATE ROLE crm_app NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO crm_app;
