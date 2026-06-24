SELECT column_name
FROM information_schema.columns
WHERE table_name = 'users'
  AND column_name IN ('status', 'last_login_at')
ORDER BY column_name;

SELECT enumlabel
FROM pg_enum
WHERE enumtypid = 'userrole'::regtype
  AND enumlabel IN ('STAFF_ADMIN', 'STAFF_OPERATOR', 'CLIENT_OPERATOR')
ORDER BY enumlabel;

SELECT indexname
FROM pg_indexes
WHERE tablename = 'users'
  AND indexname IN ('ix_users_client_id', 'ix_users_role', 'ix_users_status')
ORDER BY indexname;
