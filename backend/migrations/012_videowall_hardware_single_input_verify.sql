SELECT column_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'videowalls'
  AND column_name IN ('render_mode', 'output_width', 'output_height', 'primary_channel_id')
ORDER BY column_name;

SELECT conname
FROM pg_constraint
WHERE conname IN (
    'uq_videowall_primary_channel',
    'fk_videowalls_primary_channel',
    'ck_videowalls_render_mode'
)
ORDER BY conname;
