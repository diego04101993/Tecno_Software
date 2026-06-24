SELECT typname
FROM pg_type
WHERE typname IN (
    'datasourcetype',
    'datasourcestatus',
    'datasetstatus',
    'datasetimportstatus',
    'datasetcolumntype'
)
ORDER BY typname;

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'data_sources',
    'datasets',
    'dataset_imports',
    'dataset_columns',
    'dataset_rows'
)
ORDER BY table_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'datasets'
ORDER BY ordinal_position;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('data_sources', 'datasets', 'dataset_imports', 'dataset_columns', 'dataset_rows')
ORDER BY tablename, indexname;
