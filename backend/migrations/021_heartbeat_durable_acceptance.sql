-- Additive heartbeat receipts; execute only after explicit deployment approval.
-- Legacy BLUE ignores the new table and nullable history envelope column.
-- No business FK: administrative deletes cannot erase/block pending reports.
-- Usage: psql -X -v ON_ERROR_STOP=1 -v expected_database=EXACT_DB -f THIS_FILE
\set ON_ERROR_STOP on
\if :{?expected_database}
\else
  DO $guard$ BEGIN RAISE EXCEPTION 'MIGRATION_021_EXPECTED_DATABASE_REQUIRED'; END; $guard$;
\endif
SELECT current_database() = :'expected_database' AS migration_021_correct_database \gset
\if :migration_021_correct_database
\else
  DO $guard$ BEGIN RAISE EXCEPTION 'MIGRATION_021_DATABASE_MISMATCH'; END; $guard$;
\endif

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;
DO $precondition$
BEGIN
    IF current_setting('server_version_num')::integer / 10000 <> 16 THEN
        RAISE EXCEPTION 'MIGRATION_021_REQUIRES_POSTGRESQL_16';
    END IF;
    IF to_regclass('public.player_heartbeat_inbox') IS NOT NULL OR EXISTS (
        SELECT 1 FROM pg_attribute WHERE attrelid = 'public.player_heartbeats'::regclass
        AND attname = 'report_json' AND NOT attisdropped) THEN
        RAISE EXCEPTION 'MIGRATION_021_TARGET_ALREADY_EXISTS';
    END IF;
    IF to_regclass('public.player_devices') IS NULL OR to_regclass('public.channels') IS NULL
       OR to_regclass('public.clients') IS NULL OR to_regclass('public.branches') IS NULL THEN
        RAISE EXCEPTION 'MIGRATION_021_REQUIRED_PARENT_MISSING';
    END IF;
END;
$precondition$;

ALTER TABLE public.player_heartbeats ADD COLUMN report_json json NULL;
CREATE TABLE public.player_heartbeat_inbox (
    player_device_id varchar(36) NOT NULL,
    channel_id varchar(36) NOT NULL,
    client_id varchar(36) NOT NULL,
    branch_id varchar(36) NOT NULL,
    payload_json json NOT NULL,
    payload_bytes integer NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status varchar(8) NOT NULL DEFAULT 'PENDING',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error_code varchar(80),
    id varchar(36) PRIMARY KEY,
    CONSTRAINT ck_player_heartbeat_inbox_status CHECK (status IN ('PENDING', 'FAILED')),
    CONSTRAINT ck_player_heartbeat_inbox_payload_bytes CHECK (payload_bytes > 0),
    CONSTRAINT ck_player_heartbeat_inbox_attempts CHECK (attempts >= 0)
);
CREATE INDEX ix_player_heartbeat_inbox_available
    ON public.player_heartbeat_inbox (status, available_at, accepted_at, id);
CREATE INDEX ix_player_heartbeat_inbox_device_order
    ON public.player_heartbeat_inbox (player_device_id, accepted_at, id);
COMMIT;
\echo 'MIGRATION_021_HEARTBEAT_DURABLE_ACCEPTANCE=PASS'
