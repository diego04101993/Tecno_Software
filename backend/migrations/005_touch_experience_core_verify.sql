SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('touch_experiences', 'touch_experience_assignments', 'touch_locations', 'touch_maps')
ORDER BY table_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'kiosk_screens'
  AND column_name IN ('experience_id', 'screen_kind', 'sort_order', 'metadata_json', 'idle_timeout_override')
ORDER BY column_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'kiosk_buttons'
  AND column_name IN ('style_json', 'action_payload_json', 'is_hotspot')
ORDER BY column_name;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('touch_experiences', 'touch_experience_assignments', 'touch_locations', 'touch_maps', 'kiosk_screens')
ORDER BY tablename, indexname;
