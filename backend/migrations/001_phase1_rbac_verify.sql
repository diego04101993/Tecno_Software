SELECT e.enumlabel
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typname = 'userrole'
ORDER BY e.enumsortorder;

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'users'
  AND column_name IN ('role', 'client_id', 'branch_id')
ORDER BY column_name;

SELECT conname
FROM pg_constraint
WHERE conname = 'users_branch_id_fkey';

