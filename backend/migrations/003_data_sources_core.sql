DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'datasourcetype') THEN
        CREATE TYPE datasourcetype AS ENUM ('FILE_UPLOAD', 'GOOGLE_SHEETS', 'API');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'datasourcestatus') THEN
        CREATE TYPE datasourcestatus AS ENUM ('ACTIVE', 'PAUSED', 'ERROR');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'datasetstatus') THEN
        CREATE TYPE datasetstatus AS ENUM ('READY', 'PROCESSING', 'ERROR');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'datasetimportstatus') THEN
        CREATE TYPE datasetimportstatus AS ENUM ('PENDING', 'COMPLETED', 'FAILED');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'datasetcolumntype') THEN
        CREATE TYPE datasetcolumntype AS ENUM ('TEXT', 'NUMBER', 'DATETIME', 'BOOLEAN', 'EMPTY');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS data_sources (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    source_type datasourcetype NOT NULL,
    status datasourcestatus DEFAULT 'ACTIVE' NOT NULL,
    description text NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_id character varying(36) NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    data_source_id character varying(36) NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    name character varying(120) NOT NULL,
    slug character varying(120) NOT NULL,
    description text NULL,
    status datasetstatus DEFAULT 'READY' NOT NULL,
    current_import_id character varying(36) NULL,
    row_count integer DEFAULT 0 NOT NULL,
    column_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT uq_dataset_client_slug UNIQUE (client_id, slug)
);

CREATE TABLE IF NOT EXISTS dataset_imports (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dataset_id character varying(36) NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    source_filename character varying(255) NOT NULL,
    source_mime_type character varying(160) NULL,
    storage_path character varying(255) NOT NULL,
    import_status datasetimportstatus DEFAULT 'PENDING' NOT NULL,
    detected_sheet_name character varying(160) NULL,
    row_count integer DEFAULT 0 NOT NULL,
    column_count integer DEFAULT 0 NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    summary_json jsonb DEFAULT '{}'::jsonb NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'datasets'
          AND constraint_name = 'fk_datasets_current_import'
    ) THEN
        ALTER TABLE datasets
        ADD CONSTRAINT fk_datasets_current_import
        FOREIGN KEY (current_import_id) REFERENCES dataset_imports(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS dataset_columns (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dataset_id character varying(36) NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    import_id character varying(36) NOT NULL REFERENCES dataset_imports(id) ON DELETE CASCADE,
    column_key character varying(120) NOT NULL,
    display_name character varying(160) NOT NULL,
    source_name character varying(160) NOT NULL,
    data_type datasetcolumntype DEFAULT 'TEXT' NOT NULL,
    position_index integer DEFAULT 0 NOT NULL,
    sample_value character varying(255) NULL,
    is_visible boolean DEFAULT true NOT NULL,
    CONSTRAINT uq_dataset_import_column_key UNIQUE (import_id, column_key)
);

CREATE TABLE IF NOT EXISTS dataset_rows (
    id character varying(36) PRIMARY KEY,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dataset_id character varying(36) NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    import_id character varying(36) NOT NULL REFERENCES dataset_imports(id) ON DELETE CASCADE,
    row_index integer DEFAULT 0 NOT NULL,
    row_data_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    row_hash character varying(64) NULL,
    CONSTRAINT uq_dataset_import_row_index UNIQUE (import_id, row_index)
);

CREATE INDEX IF NOT EXISTS ix_data_sources_client_id ON data_sources (client_id);
CREATE INDEX IF NOT EXISTS ix_datasets_client_id ON datasets (client_id);
CREATE INDEX IF NOT EXISTS ix_datasets_data_source_id ON datasets (data_source_id);
CREATE INDEX IF NOT EXISTS ix_datasets_current_import_id ON datasets (current_import_id);
CREATE INDEX IF NOT EXISTS ix_dataset_imports_dataset_id ON dataset_imports (dataset_id);
CREATE INDEX IF NOT EXISTS ix_dataset_imports_imported_at ON dataset_imports (imported_at);
CREATE INDEX IF NOT EXISTS ix_dataset_columns_dataset_id ON dataset_columns (dataset_id);
CREATE INDEX IF NOT EXISTS ix_dataset_columns_import_id ON dataset_columns (import_id);
CREATE INDEX IF NOT EXISTS ix_dataset_rows_dataset_id ON dataset_rows (dataset_id);
CREATE INDEX IF NOT EXISTS ix_dataset_rows_import_id ON dataset_rows (import_id);
