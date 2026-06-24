DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'userrole') THEN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_enum
            WHERE enumtypid = 'userrole'::regtype
              AND enumlabel = 'STAFF_ADMIN'
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumtypid = 'userrole'::regtype
                  AND enumlabel = 'staff_admin'
            ) THEN
                ALTER TYPE userrole RENAME VALUE 'staff_admin' TO 'STAFF_ADMIN';
            ELSE
                ALTER TYPE userrole ADD VALUE 'STAFF_ADMIN';
            END IF;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_enum
            WHERE enumtypid = 'userrole'::regtype
              AND enumlabel = 'STAFF_OPERATOR'
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumtypid = 'userrole'::regtype
                  AND enumlabel = 'staff_operator'
            ) THEN
                ALTER TYPE userrole RENAME VALUE 'staff_operator' TO 'STAFF_OPERATOR';
            ELSE
                ALTER TYPE userrole ADD VALUE 'STAFF_OPERATOR';
            END IF;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_enum
            WHERE enumtypid = 'userrole'::regtype
              AND enumlabel = 'CLIENT_OPERATOR'
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumtypid = 'userrole'::regtype
                  AND enumlabel = 'client_operator'
            ) THEN
                ALTER TYPE userrole RENAME VALUE 'client_operator' TO 'CLIENT_OPERATOR';
            ELSE
                ALTER TYPE userrole ADD VALUE 'CLIENT_OPERATOR';
            END IF;
        END IF;
    END IF;
END $$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS status character varying(32);

UPDATE users
SET status = CASE
    WHEN COALESCE(is_active, true) THEN 'active'
    ELSE 'suspended'
END
WHERE status IS NULL OR btrim(status) = '';

ALTER TABLE users
    ALTER COLUMN status SET DEFAULT 'active';

ALTER TABLE users
    ALTER COLUMN status SET NOT NULL;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_login_at timestamp with time zone NULL;

CREATE INDEX IF NOT EXISTS ix_users_client_id ON users (client_id);
CREATE INDEX IF NOT EXISTS ix_users_role ON users (role);
CREATE INDEX IF NOT EXISTS ix_users_status ON users (status);
