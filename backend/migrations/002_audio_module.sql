DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'channelmode' AND e.enumlabel = 'AUDIO'
    ) THEN
        ALTER TYPE channelmode ADD VALUE 'AUDIO';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'contenttype' AND e.enumlabel = 'AUDIO'
    ) THEN
        ALTER TYPE contenttype ADD VALUE 'AUDIO';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audioplaylistkind') THEN
        CREATE TYPE audioplaylistkind AS ENUM ('MUSIC', 'SPOT');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audiospotrotationmode') THEN
        CREATE TYPE audiospotrotationmode AS ENUM ('SEQUENTIAL', 'RANDOM');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audionormalizationstatus') THEN
        CREATE TYPE audionormalizationstatus AS ENUM ('PENDING', 'NORMALIZED', 'SKIPPED');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audioplaybackentrykind') THEN
        CREATE TYPE audioplaybackentrykind AS ENUM ('MUSIC', 'SPOT');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS audio_playlists (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    kind audioplaylistkind NOT NULL,
    description text NULL,
    is_active boolean DEFAULT true NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_playlist_items (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    playlist_id character varying(36) NOT NULL REFERENCES audio_playlists(id) ON DELETE CASCADE,
    content_id character varying(36) NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    sort_order integer DEFAULT 1 NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    CONSTRAINT uq_audio_playlist_sort_order UNIQUE (playlist_id, sort_order)
);

CREATE TABLE IF NOT EXISTS audio_assignments (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NULL REFERENCES branches(id) ON DELETE CASCADE,
    channel_id character varying(36) UNIQUE NULL REFERENCES channels(id) ON DELETE CASCADE,
    music_playlist_id character varying(36) NULL REFERENCES audio_playlists(id) ON DELETE SET NULL,
    spot_playlist_id character varying(36) NULL REFERENCES audio_playlists(id) ON DELETE SET NULL,
    songs_between_spots integer DEFAULT 3 NOT NULL,
    spots_per_break integer DEFAULT 1 NOT NULL,
    spot_rotation_mode audiospotrotationmode DEFAULT 'SEQUENTIAL' NOT NULL,
    avoid_consecutive_spots boolean DEFAULT true NOT NULL,
    volume_normalization_enabled boolean DEFAULT false NOT NULL,
    volume_normalization_status audionormalizationstatus DEFAULT 'PENDING' NOT NULL,
    target_lufs integer DEFAULT -14 NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_playback_events (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NULL REFERENCES branches(id) ON DELETE SET NULL,
    channel_id character varying(36) NULL REFERENCES channels(id) ON DELETE SET NULL,
    playlist_id character varying(36) NULL REFERENCES audio_playlists(id) ON DELETE SET NULL,
    content_id character varying(36) NULL REFERENCES contents(id) ON DELETE SET NULL,
    entry_kind audioplaybackentrykind NOT NULL,
    played_at timestamp with time zone DEFAULT now() NOT NULL
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'audio_assignments_branch_id_key'
    ) THEN
        ALTER TABLE audio_assignments DROP CONSTRAINT audio_assignments_branch_id_key;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_audio_playlists_client_id ON audio_playlists (client_id);
CREATE INDEX IF NOT EXISTS ix_audio_playlist_items_playlist_id ON audio_playlist_items (playlist_id);
CREATE INDEX IF NOT EXISTS ix_audio_assignments_client_id ON audio_assignments (client_id);
CREATE INDEX IF NOT EXISTS ix_audio_assignments_branch_id ON audio_assignments (branch_id);
CREATE INDEX IF NOT EXISTS ix_audio_assignments_channel_id ON audio_assignments (channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audio_assignments_branch_default ON audio_assignments (branch_id) WHERE channel_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_audio_playback_events_client_id ON audio_playback_events (client_id);
CREATE INDEX IF NOT EXISTS ix_audio_playback_events_branch_id ON audio_playback_events (branch_id);
CREATE INDEX IF NOT EXISTS ix_audio_playback_events_channel_id ON audio_playback_events (channel_id);
CREATE INDEX IF NOT EXISTS ix_audio_playback_events_played_at ON audio_playback_events (played_at);
