SELECT column_name
FROM information_schema.columns
WHERE table_name = 'channels' AND column_name = 'channel_code';

SELECT table_name
FROM information_schema.tables
WHERE table_name IN ('player_devices', 'player_heartbeats', 'player_playback_events')
ORDER BY table_name;

SELECT indexname
FROM pg_indexes
WHERE tablename = 'player_devices'
ORDER BY indexname;
