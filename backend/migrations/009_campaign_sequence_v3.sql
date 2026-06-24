CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS playback_mode character varying(32) DEFAULT 'sequential' NOT NULL;

CREATE TABLE IF NOT EXISTS campaign_sequence_items (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    campaign_id character varying(36) NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    item_type character varying(24) NOT NULL DEFAULT 'content',
    content_id character varying(36) NULL REFERENCES contents(id) ON DELETE CASCADE,
    layout_id character varying(36) NULL REFERENCES layouts(id) ON DELETE CASCADE,
    sort_order integer NOT NULL DEFAULT 1,
    duration_seconds integer NOT NULL DEFAULT 15,
    options_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_enabled boolean NOT NULL DEFAULT true,
    CONSTRAINT uq_campaign_sequence_sort_order UNIQUE (campaign_id, sort_order),
    CONSTRAINT ck_campaign_sequence_item_type CHECK (item_type IN ('content', 'layout')),
    CONSTRAINT ck_campaign_sequence_item_ref CHECK (
        (item_type = 'content' AND content_id IS NOT NULL AND layout_id IS NULL)
        OR (item_type = 'layout' AND layout_id IS NOT NULL AND content_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_campaign_sequence_items_campaign_id ON campaign_sequence_items (campaign_id);
CREATE INDEX IF NOT EXISTS ix_campaign_sequence_items_content_id ON campaign_sequence_items (content_id);
CREATE INDEX IF NOT EXISTS ix_campaign_sequence_items_layout_id ON campaign_sequence_items (layout_id);

INSERT INTO campaign_sequence_items (
    id,
    campaign_id,
    item_type,
    content_id,
    layout_id,
    sort_order,
    duration_seconds,
    options_json,
    is_enabled,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid()::text,
    playlist.campaign_id,
    'content',
    playlist.content_id,
    NULL,
    playlist.sort_order,
    playlist.duration_seconds,
    jsonb_build_object('zone_key', playlist.zone_key),
    true,
    playlist.created_at,
    playlist.updated_at
FROM campaign_playlist_items AS playlist
WHERE NOT EXISTS (
    SELECT 1
    FROM campaign_sequence_items AS sequence
    WHERE sequence.campaign_id = playlist.campaign_id
);
