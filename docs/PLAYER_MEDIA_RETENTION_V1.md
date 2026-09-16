# Player media retention v1

`GET /api/player/runtime-config` adds `media_retention` without changing `runtime_version=3` or any existing playlist, contents, scheduling, playback, or heartbeat field. The authenticated Player token determines the channel, and the route rejects any inconsistent client/branch linkage between PlayerDevice and channel. The administrative channel runtime endpoint returns the same additive field after its existing authorization check.

Complete response example (illustrative IDs and paths, not a production record):

```json
{
  "schema_version": 1,
  "complete": true,
  "channel_id": "channel-uuid",
  "revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "generated_at": "2026-06-16T18:00:00Z",
  "protected_campaign_ids": ["campaign-uuid"],
  "assets": [
    {
      "content_id": "content-uuid",
      "file_path": "/media/client-uuid/image.jpg",
      "download_url": "https://api.example.test/media/client-uuid/image.jpg"
    }
  ]
}
```

Incomplete response schema:

```json
{
  "schema_version": 1,
  "complete": false,
  "channel_id": "channel-uuid",
  "revision": null,
  "generated_at": "2026-06-16T18:00:00Z",
  "protected_campaign_ids": [],
  "assets": [],
  "reason": "unknown_media_reference"
}
```

`generated_at` is UTC RFC3339. `revision` is a 64-character lowercase SHA-256 hex equality token, not a sequence number or asset checksum. The `reason` enum is `snapshot_unavailable`, `internal_error`, `unknown_media_reference`, `cross_tenant_reference`, `invalid_timezone`, `missing_content`, or `invalid_media_path`. It contains no row contents or secrets.

Only `complete=true` authorizes the Player to consider the enumerated local media set complete for **this channel and this snapshot**. `complete=true` with empty arrays means the local-media set is empty. A missing field, parse error, wrong schema version/channel, `complete=false`, or request failure must never authorize local deletion. This field does not select playback, schedule offline execution, or instruct download/purge timing.

## Scope

The protected campaigns are the current selected campaign, every applicable active channel or branch schedule not definitively expired in its configured timezone (including future schedules and lower-priority schedules), and every channel assignment not expired by `active_until` (including future assignments). A branch schedule applies only when `channel_id` is null, matching the current scheduler. Inactive/cancelled schedules and definitely expired references are excluded unless another reference protects the same campaign. `starts_on`, weekday, time-window and priority do not exclude an otherwise applicable schedule. Recurrence, overnight behavior and DST are not reinterpreted here.

For each campaign, existing V3 sequence rows take precedence; if no V3 rows exist, legacy playlist rows are used. Enabled V3 content items or legacy items contribute local media paths. Disabled V3 items do not contribute. Distinct `content_id` values are retained separately even if they point to the same physical file path; repeated references to the same `content_id` are deduplicated. The Player should deduplicate physical files by normalized `file_path` only after preserving every alias. `file_path` is a logical `/media/<client_id>/...` URL path, never filesystem authority. External URL/streaming/text items without a local media path add no local asset.

Layouts (campaign or schedule layout, enabled layout sequence item), audio/touch channels or audio/touch assignments even on a normal channel, hidden `/media/` references in content/sequence metadata, unknown or invalid paths, missing or cross-client references, or unknown schedule timezone cause `complete=false`. This deliberately favors retaining extra files over falsely declaring the set complete. Dataset/background media reachable through a layout is not guessed. There is no pagination or silent truncation in v1.

## Snapshot and revision

On PostgreSQL, both runtime selection and retention enumeration use one dedicated `REPEATABLE READ` connection. The selected playlist and the retention field therefore see either the configuration before or after a concurrent committed publication/cancellation, not a mixture. The connection is closed without commit. Legacy sequence fallback can flush temporary V3 rows during runtime generation; those writes are rolled back with this connection. The global database isolation and publication paths are unchanged. If a coherent snapshot cannot be opened, the old runtime is built through the existing session and retention is `complete=false` with `snapshot_unavailable`. On non-PostgreSQL engines retention is also incomplete.

The canonical revision includes schema version, channel ID, sorted protected campaign IDs, sorted applicable nonexpired schedule rules (`id`, campaign/scope/layout, recurrence, sorted days, date/time windows, timezone, priority, active, looping), sorted nonexpired assignment rules (`id`, campaign/channel, priority, activation window), sorted effective enabled content references (campaign/content/order), and sorted content ID, type plus logical `file_path`. JSON is encoded with sorted keys, compact separators and UTF-8 before SHA-256. Request time, server/play timestamps, absolute URL host and response ordering are excluded. Rule/reference changes modify the equality token even when local assets happen to be unchanged. It is not a video integrity proof.

A publication or cancellation committed **after** the response can change the necessary set; the Player must poll again and avoid irreversible decisions based on an old snapshot. There is no promise that a complete snapshot reserves all future campaigns forever.

## Existing assignment policy

`PUT /api/channels/{channel_id}/campaign-assignment` replaces other channel assignments; A no longer protects assets after B replaces it, unless A is also scheduled or otherwise referenced. `POST /api/campaigns/{campaign_id}/assignments` appends an assignment without removing A; both A and B remain eligible according to current assignment priority/time rules, so retention protects both. This implementation changes neither publication path.
