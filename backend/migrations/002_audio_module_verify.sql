SELECT e.enumlabel
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typname IN ('channelmode', 'contenttype')
ORDER BY t.typname, e.enumsortorder;

SELECT typname
FROM pg_type
WHERE typname IN (
    'audioplaylistkind',
    'audiospotrotationmode',
    'audionormalizationstatus',
    'audioplaybackentrykind'
)
ORDER BY typname;

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'audio_playlists',
    'audio_playlist_items',
    'audio_assignments',
    'audio_playback_events'
)
ORDER BY table_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'audio_assignments'
ORDER BY ordinal_position;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'audio_assignments'
ORDER BY indexname;
