-- 0002_import_batches.sql
-- CSV imports: the uploaded file is kept in the database between the mapping step and the
-- commit, because Cloud Run instances share no disk. The text is dropped once the batch is
-- committed or discarded.

CREATE TABLE import_batch (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    created_by  uuid REFERENCES app_user(id) ON DELETE SET NULL,
    filename    text NOT NULL,
    raw_text    text,
    headers     text[] NOT NULL DEFAULT '{}',
    row_count   integer NOT NULL DEFAULT 0,
    mapping     jsonb NOT NULL DEFAULT '{}'::jsonb,
    status_map  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status      text NOT NULL DEFAULT 'uploaded',   -- uploaded | committed | discarded
    result      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    committed_at timestamptz
);
CREATE INDEX import_batch_tenant_idx ON import_batch (tenant_id, created_at DESC);

ALTER TABLE import_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batch FORCE ROW LEVEL SECURITY;
CREATE POLICY import_batch_isolation ON import_batch
    USING (tenant_id = app_tenant_id() OR app_is_privileged())
    WITH CHECK (tenant_id = app_tenant_id() OR app_is_privileged());

ALTER TABLE lead ADD COLUMN import_batch_id uuid REFERENCES import_batch(id) ON DELETE SET NULL;
CREATE INDEX lead_import_batch_idx ON lead (import_batch_id) WHERE import_batch_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON import_batch TO crm_app;
