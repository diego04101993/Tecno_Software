CREATE TABLE IF NOT EXISTS touch_experiences (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    slug character varying(120) NOT NULL,
    description text NULL,
    attract_screen_id character varying(36) NULL,
    home_screen_id character varying(36) NULL,
    default_idle_timeout_seconds integer DEFAULT 30 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT uq_touch_experience_client_slug UNIQUE (client_id, slug)
);

ALTER TABLE kiosk_screens
    ADD COLUMN IF NOT EXISTS experience_id character varying(36) NULL,
    ADD COLUMN IF NOT EXISTS screen_kind character varying(40) DEFAULT 'custom' NOT NULL,
    ADD COLUMN IF NOT EXISTS sort_order integer DEFAULT 1 NOT NULL,
    ADD COLUMN IF NOT EXISTS metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    ADD COLUMN IF NOT EXISTS idle_timeout_override integer NULL;

ALTER TABLE kiosk_buttons
    ADD COLUMN IF NOT EXISTS style_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    ADD COLUMN IF NOT EXISTS action_payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    ADD COLUMN IF NOT EXISTS is_hotspot boolean DEFAULT false NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'kiosk_screens' AND constraint_name = 'fk_kiosk_screens_experience_id'
    ) THEN
        ALTER TABLE kiosk_screens
            ADD CONSTRAINT fk_kiosk_screens_experience_id
            FOREIGN KEY (experience_id) REFERENCES touch_experiences(id) ON DELETE SET NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'touch_experiences' AND constraint_name = 'fk_touch_experiences_attract_screen'
    ) THEN
        ALTER TABLE touch_experiences
            ADD CONSTRAINT fk_touch_experiences_attract_screen
            FOREIGN KEY (attract_screen_id) REFERENCES kiosk_screens(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'touch_experiences' AND constraint_name = 'fk_touch_experiences_home_screen'
    ) THEN
        ALTER TABLE touch_experiences
            ADD CONSTRAINT fk_touch_experiences_home_screen
            FOREIGN KEY (home_screen_id) REFERENCES kiosk_screens(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS touch_experience_assignments (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    experience_id character varying(36) NOT NULL REFERENCES touch_experiences(id) ON DELETE CASCADE,
    branch_id character varying(36) NULL REFERENCES branches(id) ON DELETE CASCADE,
    channel_id character varying(36) NULL REFERENCES channels(id) ON DELETE CASCADE,
    sort_order integer DEFAULT 1 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS touch_locations (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    experience_id character varying(36) NOT NULL REFERENCES touch_experiences(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    category character varying(80) NULL,
    description text NULL,
    floor_zone character varying(80) NULL,
    suite character varying(60) NULL,
    image_url character varying(255) NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);

CREATE TABLE IF NOT EXISTS touch_maps (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    experience_id character varying(36) NOT NULL REFERENCES touch_experiences(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    floor_zone character varying(80) NULL,
    background_url character varying(255) NULL,
    overlay_url character varying(255) NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_touch_experiences_client_id ON touch_experiences (client_id);
CREATE INDEX IF NOT EXISTS ix_touch_experiences_attract_screen_id ON touch_experiences (attract_screen_id);
CREATE INDEX IF NOT EXISTS ix_touch_experiences_home_screen_id ON touch_experiences (home_screen_id);
CREATE INDEX IF NOT EXISTS ix_kiosk_screens_experience_id ON kiosk_screens (experience_id);
CREATE INDEX IF NOT EXISTS ix_touch_experience_assignments_client_id ON touch_experience_assignments (client_id);
CREATE INDEX IF NOT EXISTS ix_touch_experience_assignments_experience_id ON touch_experience_assignments (experience_id);
CREATE INDEX IF NOT EXISTS ix_touch_experience_assignments_branch_id ON touch_experience_assignments (branch_id);
CREATE INDEX IF NOT EXISTS ix_touch_experience_assignments_channel_id ON touch_experience_assignments (channel_id);
CREATE INDEX IF NOT EXISTS ix_touch_locations_experience_id ON touch_locations (experience_id);
CREATE INDEX IF NOT EXISTS ix_touch_maps_experience_id ON touch_maps (experience_id);
