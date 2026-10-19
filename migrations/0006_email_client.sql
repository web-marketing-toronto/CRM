-- 0006_email_client.sql
-- How each person wants the Email button on a lead to open: ask | mailto (the device's mail app) |
-- gmail (compose in the browser) | outlook (Outlook on the web).
ALTER TABLE app_user ADD COLUMN email_client text NOT NULL DEFAULT 'ask';
