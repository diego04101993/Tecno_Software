SELECT column_name
FROM information_schema.columns
WHERE table_name = 'schedules'
  AND column_name IN ('is_active', 'priority')
ORDER BY column_name;

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'channels'
  AND column_name = 'output_mapping_json';

SELECT indexname
FROM pg_indexes
WHERE tablename = 'schedules'
  AND indexname = 'ix_schedules_is_active';
