DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'channelmode' AND e.enumlabel = 'TOUCH'
    ) THEN
        ALTER TYPE channelmode ADD VALUE 'TOUCH';
    END IF;
END $$;

ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS channel_code character varying(32);

UPDATE channels
SET channel_code = 'TC-' || SUBSTRING(UPPER(REPLACE(id, '-', '')) FROM 1 FOR 4) || '-' || SUBSTRING(UPPER(REPLACE(id, '-', '')) FROM 5 FOR 4)
WHERE channel_code IS NULL OR channel_code = '';

ALTER TABLE channels
    ALTER COLUMN channel_code SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_code ON channels (channel_code);

CREATE TABLE IF NOT EXISTS player_devices (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    channel_id character varying(36) NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    hardware_id character varying(160) NOT NULL,
    device_name character varying(160) NOT NULL,
    app_version character varying(64) NULL,
    player_token_hash character varying(128) NOT NULL,
    last_seen_at timestamp with time zone NULL,
    is_active boolean DEFAULT true NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS player_heartbeats (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    player_device_id character varying(36) NOT NULL REFERENCES player_devices(id) ON DELETE CASCADE,
    channel_id character varying(36) NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    status character varying(40) DEFAULT 'online' NOT NULL,
    current_content_id character varying(36) NULL REFERENCES contents(id) ON DELETE SET NULL,
    current_campaign_id character varying(36) NULL REFERENCES campaigns(id) ON DELETE SET NULL,
    current_layout_id character varying(36) NULL REFERENCES layouts(id) ON DELETE SET NULL,
    resolution_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    mode character varying(40) NULL,
    app_version character varying(64) NULL,
    cache_status_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    errors_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    local_time timestamp with time zone NULL,
    playback_position jsonb DEFAULT '{}'::jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS player_playback_events (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    player_device_id character varying(36) NOT NULL REFERENCES player_devices(id) ON DELETE CASCADE,
    channel_id character varying(36) NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    content_id character varying(36) NULL REFERENCES contents(id) ON DELETE SET NULL,
    campaign_id character varying(36) NULL REFERENCES campaigns(id) ON DELETE SET NULL,
    layout_id character varying(36) NULL REFERENCES layouts(id) ON DELETE SET NULL,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone NULL,
    duration_seconds integer NULL,
    status character varying(40) DEFAULT 'completed' NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_player_devices_channel_id ON player_devices (channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_player_devices_player_token_hash ON player_devices (player_token_hash);
CREATE INDEX IF NOT EXISTS ix_player_devices_client_id ON player_devices (client_id);
CREATE INDEX IF NOT EXISTS ix_player_devices_branch_id ON player_devices (branch_id);
CREATE INDEX IF NOT EXISTS ix_player_devices_hardware_id ON player_devices (hardware_id);
CREATE INDEX IF NOT EXISTS ix_player_heartbeats_player_device_id ON player_heartbeats (player_device_id);
CREATE INDEX IF NOT EXISTS ix_player_heartbeats_channel_id ON player_heartbeats (channel_id);
CREATE INDEX IF NOT EXISTS ix_player_heartbeats_received_at ON player_heartbeats (received_at);
CREATE INDEX IF NOT EXISTS ix_player_playback_events_player_device_id ON player_playback_events (player_device_id);
CREATE INDEX IF NOT EXISTS ix_player_playback_events_channel_id ON player_playback_events (channel_id);
CREATE INDEX IF NOT EXISTS ix_player_playback_events_started_at ON player_playback_events (started_at);
