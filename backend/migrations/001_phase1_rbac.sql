DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'userrole' AND e.enumlabel = 'CLIENT_ADMIN'
    ) THEN
        ALTER TYPE userrole ADD VALUE 'CLIENT_ADMIN';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'userrole' AND e.enumlabel = 'BRANCH_MANAGER'
    ) THEN
        ALTER TYPE userrole ADD VALUE 'BRANCH_MANAGER';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = 'userrole' AND e.enumlabel = 'OPERATOR'
    ) THEN
        ALTER TYPE userrole ADD VALUE 'OPERATOR';
    END IF;
END $$;

ALTER TABLE users
ADD COLUMN IF NOT EXISTS branch_id character varying(36);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'users_branch_id_fkey'
    ) THEN
        ALTER TABLE users
        ADD CONSTRAINT users_branch_id_fkey
        FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_users_branch_id ON users (branch_id);

