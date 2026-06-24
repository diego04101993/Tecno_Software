CREATE TABLE IF NOT EXISTS layout_revisions (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    layout_id character varying(36) NOT NULL REFERENCES layouts(id) ON DELETE CASCADE,
    revision_number integer DEFAULT 1 NOT NULL,
    name character varying(160) NOT NULL,
    status character varying(32) DEFAULT 'draft' NOT NULL,
    notes text NULL,
    editor_state_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    preview_state_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    published_at timestamp with time zone NULL,
    created_by_user_id character varying(36) NULL REFERENCES users(id) ON DELETE SET NULL,
    is_current_draft boolean DEFAULT false NOT NULL,
    is_current_published boolean DEFAULT false NOT NULL,
    CONSTRAINT uq_layout_revision_number UNIQUE (layout_id, revision_number)
);

CREATE INDEX IF NOT EXISTS ix_layout_revisions_layout_id ON layout_revisions (layout_id);
CREATE INDEX IF NOT EXISTS ix_layout_revisions_layout_revision_number ON layout_revisions (layout_id, revision_number);
CREATE INDEX IF NOT EXISTS ix_layout_revisions_current_draft ON layout_revisions (layout_id, is_current_draft);
CREATE INDEX IF NOT EXISTS ix_layout_revisions_current_published ON layout_revisions (layout_id, is_current_published);
