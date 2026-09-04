BEGIN;

DO $$
DECLARE
    orientation_udt text;
    has_uppercase_enum_values boolean;
BEGIN
    IF to_regclass('public.device_outputs') IS NULL THEN
        RAISE EXCEPTION 'device_outputs table is missing; apply migration 013 first';
    END IF;

    SELECT udt_name
    INTO orientation_udt
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'device_outputs'
      AND column_name = 'orientation';

    IF orientation_udt IS NULL THEN
        RAISE EXCEPTION 'device_outputs.orientation column is missing';
    END IF;

    /* Older databases may have a lowercase native enum. Move only this
       column to the varchar shape used by migration 013 so uppercase values
       can be persisted without dropping any row or enum data. */
    SELECT EXISTS (
        SELECT 1
        FROM pg_enum enum_value
        JOIN pg_type enum_type ON enum_type.oid = enum_value.enumtypid
        JOIN pg_namespace enum_schema ON enum_schema.oid = enum_type.typnamespace
        WHERE enum_schema.nspname = 'public'
          AND enum_type.typname = orientation_udt
          AND enum_value.enumlabel IN ('LANDSCAPE', 'PORTRAIT')
    )
    INTO has_uppercase_enum_values;

    IF EXISTS (
        SELECT 1
        FROM pg_type enum_type
        JOIN pg_namespace enum_schema ON enum_schema.oid = enum_type.typnamespace
        WHERE enum_schema.nspname = 'public'
          AND enum_type.typname = orientation_udt
          AND enum_type.typtype = 'e'
    ) AND NOT has_uppercase_enum_values THEN
        ALTER TABLE device_outputs
            DROP CONSTRAINT IF EXISTS ck_device_outputs_orientation;
        ALTER TABLE device_outputs
            ALTER COLUMN orientation DROP DEFAULT;
        ALTER TABLE device_outputs
            ALTER COLUMN orientation TYPE character varying(32)
            USING orientation::text;
    END IF;
END
$$;

UPDATE device_outputs
SET orientation = 'LANDSCAPE'
WHERE orientation::text = 'landscape';

UPDATE device_outputs
SET orientation = 'PORTRAIT'
WHERE orientation::text = 'portrait';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM device_outputs
        WHERE orientation IS NOT NULL
          AND orientation::text NOT IN ('LANDSCAPE', 'PORTRAIT')
    ) THEN
        RAISE EXCEPTION 'device_outputs.orientation contains values outside LANDSCAPE/PORTRAIT';
    END IF;
END
$$;

ALTER TABLE device_outputs
    ALTER COLUMN orientation SET DEFAULT 'LANDSCAPE';

ALTER TABLE device_outputs
    DROP CONSTRAINT IF EXISTS ck_device_outputs_orientation;

ALTER TABLE device_outputs
    ADD CONSTRAINT ck_device_outputs_orientation
    CHECK (orientation::text IN ('LANDSCAPE', 'PORTRAIT'));

COMMIT;
