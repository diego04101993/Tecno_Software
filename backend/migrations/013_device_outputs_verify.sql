SELECT column_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'channels'
  AND column_name IN ('device_mode', 'canvas_width', 'canvas_height')
ORDER BY column_name;

SELECT column_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'device_outputs'
  AND column_name IN (
      'root_channel_id',
      'name',
      'display_order',
      'display_uid',
      'channel_id',
      'x',
      'y',
      'width',
      'height',
      'source_x',
      'source_y',
      'source_width',
      'source_height',
      'rotation',
      'orientation',
      'sync_group',
      'enabled'
  )
ORDER BY column_name;

SELECT conname
FROM pg_constraint
WHERE conname IN (
    'ck_channels_device_mode',
    'uq_device_outputs_root_display_order',
    'uq_device_outputs_root_name',
    'ck_device_outputs_rotation',
    'ck_device_outputs_orientation'
)
ORDER BY conname;

SELECT
    channel.id,
    channel.name,
    channel.mode,
    channel.device_mode,
    channel.screen_count,
    channel.canvas_width,
    channel.canvas_height,
    COUNT(device_output.id) AS outputs_count
FROM channels AS channel
LEFT JOIN device_outputs AS device_output
    ON device_output.root_channel_id = channel.id
GROUP BY
    channel.id,
    channel.name,
    channel.mode,
    channel.device_mode,
    channel.screen_count,
    channel.canvas_width,
    channel.canvas_height
ORDER BY channel.created_at DESC
LIMIT 25;
