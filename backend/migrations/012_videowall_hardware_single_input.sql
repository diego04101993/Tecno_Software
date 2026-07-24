ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS render_mode character varying(40);

UPDATE videowalls
SET render_mode = 'multi-node'
WHERE render_mode IS NULL OR btrim(render_mode) = '';

ALTER TABLE videowalls
    ALTER COLUMN render_mode SET DEFAULT 'multi-node';

ALTER TABLE videowalls
    ALTER COLUMN render_mode SET NOT NULL;

ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS output_width integer;

UPDATE videowalls
SET output_width = GREATEST(1, total_width / GREATEST(columns, 1))
WHERE output_width IS NULL;

ALTER TABLE videowalls
    ALTER COLUMN output_width SET DEFAULT 1920;

ALTER TABLE videowalls
    ALTER COLUMN output_width SET NOT NULL;

ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS output_height integer;

UPDATE videowalls
SET output_height = GREATEST(1, total_height / GREATEST(rows, 1))
WHERE output_height IS NULL;

ALTER TABLE videowalls
    ALTER COLUMN output_height SET DEFAULT 1080;

ALTER TABLE videowalls
    ALTER COLUMN output_height SET NOT NULL;

ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS primary_channel_id character varying(36);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_videowall_primary_channel'
    ) THEN
        ALTER TABLE videowalls
            ADD CONSTRAINT uq_videowall_primary_channel UNIQUE (primary_channel_id);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_videowalls_primary_channel'
    ) THEN
        ALTER TABLE videowalls
            ADD CONSTRAINT fk_videowalls_primary_channel
            FOREIGN KEY (primary_channel_id) REFERENCES channels(id) ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_videowalls_render_mode'
    ) THEN
        ALTER TABLE videowalls
            ADD CONSTRAINT ck_videowalls_render_mode
            CHECK (render_mode IN ('multi-node', 'hardware-single-input'));
    END IF;
END
$$;
