CREATE TABLE IF NOT EXISTS content_folders (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    parent_id character varying(36) NULL REFERENCES content_folders(id),
    name character varying(160) NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT uq_content_folder_parent_name UNIQUE (client_id, parent_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_content_folder_root_name
    ON content_folders (client_id, name)
    WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_content_folders_client_id ON content_folders (client_id);
CREATE INDEX IF NOT EXISTS ix_content_folders_parent_id ON content_folders (parent_id);

ALTER TABLE contents
    ADD COLUMN IF NOT EXISTS folder_id character varying(36) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_contents_folder_id'
          AND table_name = 'contents'
    ) THEN
        ALTER TABLE contents
            ADD CONSTRAINT fk_contents_folder_id
            FOREIGN KEY (folder_id) REFERENCES content_folders(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_contents_folder_id ON contents (folder_id);
