SELECT table_name
FROM information_schema.tables
WHERE table_name = 'layout_revisions';

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'layout_revisions'
ORDER BY ordinal_position;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'layout_revisions'
ORDER BY indexname;
