-- Read-only post-migration inspection; does not repair or seed anything.
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND ((table_name = 'videowalls' AND column_name IN ('media_delivery','active_publication_id'))
       OR table_name IN ('videowall_publications','videowall_publication_assets'))
ORDER BY table_name, ordinal_position;

SELECT conrelid::regclass AS table_name, conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid IN ('videowalls'::regclass, 'videowall_publications'::regclass,
                  'videowall_publication_assets'::regclass)
ORDER BY conrelid::regclass::text, conname;

SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = current_schema()
  AND tablename IN ('videowall_publications','videowall_publication_assets')
ORDER BY tablename, indexname;

SELECT media_delivery, count(*) AS wall_count FROM videowalls GROUP BY media_delivery ORDER BY media_delivery;
SELECT count(*) AS invalid_active_pointer_count
FROM videowalls wall
LEFT JOIN videowall_publications publication ON publication.id = wall.active_publication_id
WHERE wall.active_publication_id IS NOT NULL
  AND (publication.id IS NULL OR publication.wall_id != wall.id
       OR publication.client_id != wall.client_id OR publication.status != 'ACTIVE');
