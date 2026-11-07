-- Local-QA approved playback scopes. No Channel, PlayerDevice, token or code writes.
-- Existing wall publications retain a NULL group; no automatic playback backfill.
BEGIN;
ALTER TABLE videowalls ADD COLUMN playback_groups_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE videowall_nodes ADD COLUMN layout_enabled boolean NOT NULL DEFAULT true;
CREATE TABLE videowall_playback_groups (
    id varchar(36) PRIMARY KEY,
    client_id varchar(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    branch_id varchar(36) NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    wall_id varchar(36) NOT NULL REFERENCES videowalls(id) ON DELETE CASCADE,
    name varchar(120) NOT NULL,
    mode varchar(24) NOT NULL,
    campaign_id varchar(36) NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    active_publication_id varchar(36),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_vw_playback_group_mode CHECK (mode IN ('INDIVIDUAL','SYNCHRONIZED'))
);
CREATE TABLE videowall_playback_group_members (
    id varchar(36) PRIMARY KEY,
    group_id varchar(36) NOT NULL REFERENCES videowall_playback_groups(id) ON DELETE CASCADE,
    node_id varchar(36) NOT NULL REFERENCES videowall_nodes(id) ON DELETE RESTRICT,
    position_index integer NOT NULL,
    row_index integer NOT NULL,
    column_index integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_vw_group_current_node UNIQUE (node_id),
    CONSTRAINT uq_vw_group_local_position UNIQUE (group_id, position_index),
    CONSTRAINT ck_vw_group_member_position CHECK (position_index > 0 AND row_index >= 0 AND column_index >= 0)
);
ALTER TABLE videowall_publications ADD COLUMN playback_group_id varchar(36)
    REFERENCES videowall_playback_groups(id) ON DELETE RESTRICT;
ALTER TABLE videowall_playback_groups ADD CONSTRAINT fk_vw_playback_group_active_publication
    FOREIGN KEY (active_publication_id) REFERENCES videowall_publications(id) ON DELETE SET NULL;
ALTER TABLE videowall_publications DROP CONSTRAINT ck_vw_publications_asset_count;
ALTER TABLE videowall_publications ADD CONSTRAINT ck_vw_publications_asset_count CHECK (expected_asset_count > 0);
ALTER TABLE videowall_publication_assets DROP CONSTRAINT ck_vw_publication_assets_position;
ALTER TABLE videowall_publication_assets ADD CONSTRAINT ck_vw_publication_assets_position CHECK (position_index > 0);
COMMIT;
-- Rollback only before group use: remove new FKs/tables/columns and restore the
-- old checks after verifying every existing asset/publication satisfies them.
-- After group publications exist, restore the pre-migration QA snapshot instead
-- of deleting publications/assets or silently discarding playback history.
