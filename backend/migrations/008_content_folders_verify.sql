SELECT table_name
FROM information_schema.tables
WHERE table_name = 'content_folders';

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'contents' AND column_name = 'folder_id';

SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_name IN ('content_folders', 'contents')
  AND constraint_name IN ('uq_content_folder_parent_name', 'fk_contents_folder_id')
ORDER BY constraint_name;

SELECT indexname
FROM pg_indexes
WHERE tablename IN ('content_folders', 'contents')
  AND indexname IN ('uq_content_folder_root_name', 'ix_content_folders_client_id', 'ix_content_folders_parent_id', 'ix_contents_folder_id')
ORDER BY indexname;
