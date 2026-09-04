BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS device_mode character varying(32);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'channels'::regclass
          AND conname = 'ck_channels_device_mode'
          AND position('SINGLE' IN pg_get_constraintdef(oid)) = 0
    ) THEN
        ALTER TABLE channels
            DROP CONSTRAINT ck_channels_device_mode;
    END IF;
END
$$;

UPDATE channels
SET device_mode = CASE
    WHEN mode::text = 'EXPANDED' THEN 'EXTENDED'
    ELSE 'SINGLE'
END
WHERE device_mode IS NULL
   OR btrim(device_mode) = ''
   OR upper(device_mode) NOT IN ('SINGLE', 'MIRROR', 'EXTENDED', 'INDEPENDENT')
   OR device_mode <> upper(device_mode);

ALTER TABLE channels
    ALTER COLUMN device_mode SET DEFAULT 'SINGLE';

ALTER TABLE channels
    ALTER COLUMN device_mode SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_channels_device_mode'
    ) THEN
            ALTER TABLE channels
            ADD CONSTRAINT ck_channels_device_mode
            CHECK (device_mode IN ('SINGLE', 'MIRROR', 'EXTENDED', 'INDEPENDENT'));
    END IF;
END
$$;

ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS canvas_width integer;

UPDATE channels
SET canvas_width = CASE
    WHEN mode::text = 'EXPANDED' THEN GREATEST(1, resolution_width) * GREATEST(1, screen_count)
    ELSE GREATEST(1, resolution_width)
END
WHERE canvas_width IS NULL OR canvas_width <= 0;

ALTER TABLE channels
    ALTER COLUMN canvas_width SET DEFAULT 1920;

ALTER TABLE channels
    ALTER COLUMN canvas_width SET NOT NULL;

ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS canvas_height integer;

UPDATE channels
SET canvas_height = GREATEST(1, resolution_height)
WHERE canvas_height IS NULL OR canvas_height <= 0;

ALTER TABLE channels
    ALTER COLUMN canvas_height SET DEFAULT 1080;

ALTER TABLE channels
    ALTER COLUMN canvas_height SET NOT NULL;

CREATE TABLE IF NOT EXISTS device_outputs (
    id character varying(36) PRIMARY KEY,
    root_channel_id character varying(36) NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    display_order integer NOT NULL DEFAULT 0,
    display_uid character varying(160),
    channel_id character varying(36) REFERENCES channels(id) ON DELETE SET NULL,
    x integer NOT NULL DEFAULT 0,
    y integer NOT NULL DEFAULT 0,
    width integer NOT NULL DEFAULT 1920,
    height integer NOT NULL DEFAULT 1080,
    source_x integer NOT NULL DEFAULT 0,
    source_y integer NOT NULL DEFAULT 0,
    source_width integer NOT NULL DEFAULT 1920,
    source_height integer NOT NULL DEFAULT 1080,
    rotation integer NOT NULL DEFAULT 0,
    orientation character varying(32) NOT NULL DEFAULT 'LANDSCAPE',
    sync_group character varying(120) DEFAULT 'main',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_outputs_root_display_order UNIQUE (root_channel_id, display_order),
    CONSTRAINT uq_device_outputs_root_name UNIQUE (root_channel_id, name),
    CONSTRAINT ck_device_outputs_rotation CHECK (rotation IN (0, 90, 180, 270)),
    CONSTRAINT ck_device_outputs_orientation CHECK (lower(orientation::text) IN ('landscape', 'portrait'))
);

CREATE INDEX IF NOT EXISTS ix_device_outputs_root_channel_id
    ON device_outputs(root_channel_id);

CREATE INDEX IF NOT EXISTS ix_device_outputs_channel_id
    ON device_outputs(channel_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'device_outputs_root_channel_id_fkey'
    ) THEN
        ALTER TABLE device_outputs
            ADD CONSTRAINT device_outputs_root_channel_id_fkey
            FOREIGN KEY (root_channel_id) REFERENCES channels(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'device_outputs_channel_id_fkey'
    ) THEN
        ALTER TABLE device_outputs
            ADD CONSTRAINT device_outputs_channel_id_fkey
            FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'uq_device_outputs_root_display_order'
    ) THEN
        ALTER TABLE device_outputs
            ADD CONSTRAINT uq_device_outputs_root_display_order
            UNIQUE (root_channel_id, display_order);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'uq_device_outputs_root_name'
    ) THEN
        ALTER TABLE device_outputs
            ADD CONSTRAINT uq_device_outputs_root_name
            UNIQUE (root_channel_id, name);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'ck_device_outputs_rotation'
    ) THEN
        ALTER TABLE device_outputs
            ADD CONSTRAINT ck_device_outputs_rotation
            CHECK (rotation IN (0, 90, 180, 270));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'device_outputs'::regclass
          AND conname = 'ck_device_outputs_orientation'
    ) THEN
            ALTER TABLE device_outputs
            ADD CONSTRAINT ck_device_outputs_orientation
            CHECK (lower(orientation::text) IN ('landscape', 'portrait'));
    END IF;
END
$$;

DO $$
DECLARE
    orientation_expression text;
BEGIN
    SELECT CASE
        WHEN udt_name = 'deviceoutputorientation'
            THEN $expr$CASE WHEN channel.orientation::text = 'VERTICAL' THEN 'PORTRAIT'::deviceoutputorientation ELSE 'LANDSCAPE'::deviceoutputorientation END$expr$
        ELSE $expr$CASE WHEN channel.orientation::text = 'VERTICAL' THEN 'portrait' ELSE 'landscape' END$expr$
    END
    INTO orientation_expression
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'device_outputs'
      AND column_name = 'orientation';

    IF orientation_expression IS NULL THEN
        RAISE EXCEPTION 'device_outputs.orientation column is missing';
    END IF;

    EXECUTE format($migration$
        INSERT INTO device_outputs (
            id,
            root_channel_id,
            name,
            display_order,
            display_uid,
            channel_id,
            x,
            y,
            width,
            height,
            source_x,
            source_y,
            source_width,
            source_height,
            rotation,
            orientation,
            sync_group,
            enabled
        )
        SELECT
            gen_random_uuid()::text,
            channel.id,
            'Salida ' || (output_position.position_index + 1)::text,
            output_position.position_index,
            NULL,
            CASE
                WHEN channel.device_mode = 'INDEPENDENT' THEN NULL
                ELSE channel.id
            END,
            CASE
                WHEN channel.device_mode = 'SINGLE' THEN 0
                ELSE GREATEST(1, channel.resolution_width) * output_position.position_index
            END,
            0,
            GREATEST(1, channel.resolution_width),
            GREATEST(1, channel.resolution_height),
            CASE
                WHEN channel.device_mode = 'EXTENDED'
                    THEN GREATEST(1, channel.resolution_width) * output_position.position_index
                ELSE 0
            END,
            0,
            CASE
                WHEN channel.device_mode = 'MIRROR' THEN GREATEST(1, channel.canvas_width)
                WHEN channel.device_mode = 'EXTENDED' THEN GREATEST(1, channel.resolution_width)
                ELSE GREATEST(1, channel.resolution_width)
            END,
            CASE
                WHEN channel.device_mode = 'MIRROR' THEN GREATEST(1, channel.canvas_height)
                WHEN channel.device_mode = 'EXTENDED' THEN GREATEST(1, channel.canvas_height)
                ELSE GREATEST(1, channel.resolution_height)
            END,
            0,
            %s,
            CASE
                WHEN channel.device_mode = 'INDEPENDENT' THEN NULL
                ELSE 'main'
            END,
            true
        FROM channels AS channel
        CROSS JOIN LATERAL generate_series(
            0,
            CASE
                WHEN channel.device_mode = 'SINGLE' THEN 0
                ELSE GREATEST(1, channel.screen_count) - 1
            END
        ) AS output_position(position_index)
        WHERE NOT EXISTS (
            SELECT 1
            FROM device_outputs AS existing_output
            WHERE existing_output.root_channel_id = channel.id
              AND existing_output.display_order = output_position.position_index
        )
    $migration$, orientation_expression);
END
$$;

COMMIT;
