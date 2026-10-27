-- 0007_ad_creatives.sql
-- What the ad that produced a lead looked like. Keyed by the ad id the platforms hand us (Meta lead
-- forms carry it; website leads carry it when the campaign URLs include ad_id={{ad.id}} / {creative}).
-- The thumbnail bytes are kept here because the platforms' image URLs expire.

CREATE TABLE ad_creative (
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    platform      lead_platform NOT NULL,
    ad_id         text NOT NULL,
    name          text,
    adset_name    text,
    campaign_id   text,
    campaign_name text,
    headline      text,
    body          text,
    link_url      text,
    image_url     text,
    thumb         bytea,
    thumb_type    text,
    status        text NOT NULL DEFAULT 'pending',   -- ok | missing | error | pending
    error         text,
    fetched_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, platform, ad_id)
);
ALTER TABLE ad_creative ENABLE ROW LEVEL SECURITY;
ALTER TABLE ad_creative FORCE ROW LEVEL SECURITY;
CREATE POLICY ad_creative_isolation ON ad_creative
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());
GRANT SELECT, INSERT, UPDATE, DELETE ON ad_creative TO crm_app;

CREATE INDEX lead_source_ad_idx ON lead_source (tenant_id, platform, ad_id) WHERE ad_id IS NOT NULL;
