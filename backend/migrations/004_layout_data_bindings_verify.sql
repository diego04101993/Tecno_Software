SELECT typname
FROM pg_type
WHERE typname = 'layoutbindingpreset';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('layout_data_bindings', 'layout_binding_fields')
ORDER BY table_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'layout_data_bindings'
ORDER BY ordinal_position;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('layout_data_bindings', 'layout_binding_fields')
ORDER BY tablename, indexname;
