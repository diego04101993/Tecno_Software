SELECT
    column_name,
    data_type,
    udt_name,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'device_outputs'
  AND column_name = 'orientation';

SELECT
    orientation::text AS orientation,
    COUNT(*) AS count
FROM device_outputs
GROUP BY orientation
ORDER BY orientation;

SELECT COUNT(*) AS invalid_orientation_count
FROM device_outputs
WHERE orientation IS NOT NULL
  AND orientation::text NOT IN ('LANDSCAPE', 'PORTRAIT');

SELECT
    conname,
    pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.device_outputs'::regclass
  AND conname = 'ck_device_outputs_orientation';
