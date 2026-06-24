ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS is_active boolean;

UPDATE schedules
SET is_active = true
WHERE is_active IS NULL;

ALTER TABLE schedules
    ALTER COLUMN is_active SET DEFAULT true;

ALTER TABLE schedules
    ALTER COLUMN is_active SET NOT NULL;

ALTER TABLE schedules
    ALTER COLUMN priority SET DEFAULT 100;

UPDATE schedules
SET priority = 100
WHERE priority IS NULL;

ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS output_mapping_json jsonb;

UPDATE channels
SET output_mapping_json = '{}'::jsonb
WHERE output_mapping_json IS NULL;

ALTER TABLE channels
    ALTER COLUMN output_mapping_json SET DEFAULT '{}'::jsonb;

ALTER TABLE channels
    ALTER COLUMN output_mapping_json SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_schedules_is_active ON schedules (is_active);
