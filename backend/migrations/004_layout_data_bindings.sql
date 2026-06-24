DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'layoutbindingpreset') THEN
        CREATE TYPE layoutbindingpreset AS ENUM ('AUTOBUSES', 'AEROPUERTO', 'MENU', 'TURNERO');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS layout_data_bindings (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    layout_id character varying(36) NOT NULL REFERENCES layouts(id) ON DELETE CASCADE,
    dataset_id character varying(36) NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    preset_key layoutbindingpreset NOT NULL,
    zone_key character varying(80) NULL,
    sort_order integer DEFAULT 1 NOT NULL,
    max_rows integer DEFAULT 8 NOT NULL,
    options_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);

CREATE TABLE IF NOT EXISTS layout_binding_fields (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    binding_id character varying(36) NOT NULL REFERENCES layout_data_bindings(id) ON DELETE CASCADE,
    target_field character varying(80) NOT NULL,
    column_key character varying(120) NULL,
    display_label character varying(120) NULL,
    fallback_value character varying(255) NULL,
    format_hint character varying(40) NULL,
    position_index integer DEFAULT 0 NOT NULL,
    is_required boolean DEFAULT true NOT NULL,
    options_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT uq_layout_binding_target_field UNIQUE (binding_id, target_field)
);

CREATE INDEX IF NOT EXISTS ix_layout_data_bindings_layout_id ON layout_data_bindings (layout_id);
CREATE INDEX IF NOT EXISTS ix_layout_data_bindings_dataset_id ON layout_data_bindings (dataset_id);
CREATE INDEX IF NOT EXISTS ix_layout_data_bindings_sort_order ON layout_data_bindings (sort_order);
CREATE INDEX IF NOT EXISTS ix_layout_binding_fields_binding_id ON layout_binding_fields (binding_id);
CREATE INDEX IF NOT EXISTS ix_layout_binding_fields_position_index ON layout_binding_fields (position_index);
