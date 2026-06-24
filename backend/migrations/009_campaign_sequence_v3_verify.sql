SELECT column_name
FROM information_schema.columns
WHERE table_name = 'campaigns' AND column_name = 'playback_mode';

SELECT table_name
FROM information_schema.tables
WHERE table_name = 'campaign_sequence_items';

SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_name = 'campaign_sequence_items'
  AND constraint_name IN (
    'uq_campaign_sequence_sort_order',
    'ck_campaign_sequence_item_type',
    'ck_campaign_sequence_item_ref'
  )
ORDER BY constraint_name;

SELECT indexname
FROM pg_indexes
WHERE tablename = 'campaign_sequence_items'
  AND indexname IN (
    'ix_campaign_sequence_items_campaign_id',
    'ix_campaign_sequence_items_content_id',
    'ix_campaign_sequence_items_layout_id'
  )
ORDER BY indexname;

SELECT campaign_id, COUNT(*) AS legacy_count
FROM campaign_playlist_items
GROUP BY campaign_id
ORDER BY campaign_id;

SELECT campaign_id, COUNT(*) AS sequence_count
FROM campaign_sequence_items
GROUP BY campaign_id
ORDER BY campaign_id;
