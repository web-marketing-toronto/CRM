-- 0008_next_step.sql
-- One visible "next step" per lead, set from the outcome of a call, text or email, instead of a
-- hidden reminder cadence. The step and its time drive the reminders and define Needs action.
--   next_step: call_back | appointment | send_quote | waiting   (NULL when nothing is planned)
--   next_step_at: when it is due (an appointment's time, a call-back time, the end of a wait)

ALTER TABLE lead ADD COLUMN next_step text;
ALTER TABLE lead ADD COLUMN next_step_at timestamptz;
ALTER TABLE lead ADD COLUMN next_step_note text;
CREATE INDEX lead_next_step_idx ON lead (tenant_id, next_step_at) WHERE next_step_at IS NOT NULL;

-- Existing appointments and snoozes become next steps so nothing in flight is lost.
UPDATE lead SET next_step = 'appointment', next_step_at = appointment_at WHERE appointment_at IS NOT NULL AND status NOT IN ('won', 'lost');
UPDATE lead SET next_step = 'waiting', next_step_at = snoozed_until WHERE next_step IS NULL AND snoozed_until IS NOT NULL AND snoozed_until > now() AND status NOT IN ('won', 'lost');

ALTER TYPE activity_type ADD VALUE IF NOT EXISTS 'outcome';
