-- Opt-in Android1x3 publication authority. Execute only as an approved migration.
-- Legacy walls retain their behavior. No campaign, node, media or seed rows change.
BEGIN;

ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS media_delivery character varying(24) NOT NULL DEFAULT 'legacy';
ALTER TABLE videowalls
    ADD COLUMN IF NOT EXISTS active_publication_id character varying(36);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'videowalls'::regclass
                     AND conname = 'ck_videowalls_media_delivery') THEN
        ALTER TABLE videowalls ADD CONSTRAINT ck_videowalls_media_delivery
            CHECK (media_delivery IN ('legacy','node_slice'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS videowall_publications (
    id character varying(36) PRIMARY KEY,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id character varying(36) NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    wall_id character varying(36) NOT NULL REFERENCES videowalls(id) ON DELETE CASCADE,
    campaign_id character varying(36) NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    requested_by_user_id character varying(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status character varying(16) NOT NULL DEFAULT 'PENDING',
    program_snapshot_json JSON NOT NULL,
    geometry_snapshot_json JSON NOT NULL,
    geometry_signature character varying(64) NOT NULL,
    encoding_profile character varying(64) NOT NULL DEFAULT 'code302_h264_cfr_v1',
    expected_asset_count integer NOT NULL,
    loop_duration_ms bigint,
    loop_start_epoch_ms bigint,
    lease_token character varying(64),
    lease_owner character varying(120),
    lease_expires_at timestamp with time zone,
    attempt_count integer NOT NULL DEFAULT 0,
    error_code character varying(80),
    error_message character varying(500),
    activated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT ck_vw_publications_status CHECK (status IN ('PENDING','PROCESSING','READY','ACTIVE','FAILED')),
    CONSTRAINT ck_vw_publications_asset_count CHECK (expected_asset_count > 0 AND expected_asset_count % 3 = 0),
    CONSTRAINT ck_vw_publications_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_vw_publications_loop_duration CHECK (loop_duration_ms IS NULL OR loop_duration_ms > 0),
    CONSTRAINT ck_vw_publications_epoch CHECK (loop_start_epoch_ms IS NULL OR loop_start_epoch_ms >= 0),
    CONSTRAINT ck_vw_publications_ready_timeline CHECK (status NOT IN ('READY','ACTIVE') OR loop_duration_ms IS NOT NULL),
    CONSTRAINT ck_vw_publications_active_epoch CHECK (status != 'ACTIVE' OR (loop_start_epoch_ms IS NOT NULL AND activated_at IS NOT NULL)),
    CONSTRAINT ck_vw_publications_encoding_profile CHECK (encoding_profile = 'code302_h264_cfr_v1')
);

CREATE TABLE IF NOT EXISTS videowall_publication_assets (
    id character varying(36) PRIMARY KEY,
    publication_id character varying(36) NOT NULL REFERENCES videowall_publications(id) ON DELETE CASCADE,
    node_id character varying(36) NOT NULL REFERENCES videowall_nodes(id) ON DELETE RESTRICT,
    master_content_id character varying(36) NOT NULL REFERENCES contents(id) ON DELETE RESTRICT,
    derived_content_id character varying(36) REFERENCES contents(id) ON DELETE RESTRICT,
    logical_item_id character varying(36) NOT NULL,
    position_index integer NOT NULL,
    sort_order integer NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    duration_ms bigint,
    mime_type character varying(120),
    checksum character varying(64),
    size_bytes bigint,
    probe_json JSON,
    validated boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT uq_vw_publication_item_node UNIQUE (publication_id, logical_item_id, position_index),
    CONSTRAINT uq_vw_publication_derived_content UNIQUE (derived_content_id),
    CONSTRAINT ck_vw_publication_assets_position CHECK (position_index BETWEEN 1 AND 3),
    CONSTRAINT ck_vw_publication_assets_geometry CHECK (sort_order >= 1 AND width > 0 AND height > 0),
    CONSTRAINT ck_vw_publication_assets_duration CHECK (duration_ms IS NULL OR duration_ms > 0),
    CONSTRAINT ck_vw_publication_assets_size CHECK (size_bytes IS NULL OR size_bytes > 0),
    CONSTRAINT ck_vw_publication_assets_validated CHECK (NOT validated OR
        (derived_content_id IS NOT NULL AND duration_ms IS NOT NULL AND mime_type IS NOT NULL
         AND checksum IS NOT NULL AND size_bytes IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_vw_publications_claim
    ON videowall_publications (status, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS ix_vw_publications_wall_created
    ON videowall_publications (wall_id, created_at);
CREATE INDEX IF NOT EXISTS ix_vw_publications_client_branch
    ON videowall_publications (client_id, branch_id);
CREATE INDEX IF NOT EXISTS ix_vw_publication_assets_master
    ON videowall_publication_assets (master_content_id);
CREATE INDEX IF NOT EXISTS ix_vw_publication_assets_node
    ON videowall_publication_assets (node_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'videowalls'::regclass
                     AND conname = 'fk_videowalls_active_publication') THEN
        ALTER TABLE videowalls ADD CONSTRAINT fk_videowalls_active_publication
            FOREIGN KEY (active_publication_id) REFERENCES videowall_publications(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'videowalls'
                     AND column_name = 'media_delivery' AND data_type = 'character varying'
                     AND character_maximum_length = 24 AND is_nullable = 'NO'
                     AND column_default LIKE '%legacy%') THEN
        RAISE EXCEPTION 'Unexpected videowalls.media_delivery schema';
    END IF;
END $$;

COMMIT;
