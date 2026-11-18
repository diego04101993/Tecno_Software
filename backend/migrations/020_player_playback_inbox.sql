-- 020: bounded durable playback receipts. Manual approval required to execute.
-- Additive only. Do not drop this table while acknowledged receipts remain.
-- Scope IDs are immutable historical evidence, deliberately NOT foreign keys:
-- legacy BLUE may delete parents without deleting or blocking durable receipts.
-- The authenticated GREEN guard validates/locks live scope before acceptance.
-- Usage: psql -X -v ON_ERROR_STOP=1 -v expected_database=EXACT_DB -f THIS_FILE
\set ON_ERROR_STOP on
\if :{?expected_database}
\else
  DO $guard$ BEGIN RAISE EXCEPTION 'MIGRATION_020_EXPECTED_DATABASE_REQUIRED'; END; $guard$;
\endif
SELECT current_database() = :'expected_database' AS migration_020_correct_database \gset
\if :migration_020_correct_database
\else
  DO $guard$ BEGIN RAISE EXCEPTION 'MIGRATION_020_DATABASE_MISMATCH'; END; $guard$;
\endif

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, public;

DO $precondition$
BEGIN
    IF current_setting('server_version_num')::integer / 10000 <> 16 THEN
        RAISE EXCEPTION 'MIGRATION_020_REQUIRES_POSTGRESQL_16';
    END IF;
    IF to_regclass('public.player_playback_inbox') IS NOT NULL THEN
        RAISE EXCEPTION 'MIGRATION_020_TARGET_ALREADY_EXISTS';
    END IF;
    IF to_regclass('public.player_devices') IS NULL OR to_regclass('public.channels') IS NULL
       OR to_regclass('public.clients') IS NULL OR to_regclass('public.branches') IS NULL THEN
        RAISE EXCEPTION 'MIGRATION_020_REQUIRED_PARENT_MISSING';
    END IF;
END;
$precondition$;

CREATE TABLE public.player_playback_inbox (
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
    CONSTRAINT ck_player_playback_inbox_status CHECK (status IN ('PENDING', 'FAILED')),
    CONSTRAINT ck_player_playback_inbox_payload_bytes CHECK (payload_bytes > 0),
    CONSTRAINT ck_player_playback_inbox_attempts CHECK (attempts >= 0)
);
CREATE INDEX ix_player_playback_inbox_available
    ON public.player_playback_inbox (status, available_at, accepted_at, id);
CREATE INDEX ix_player_playback_inbox_player_device_id ON public.player_playback_inbox (player_device_id);
CREATE INDEX ix_player_playback_inbox_channel_id ON public.player_playback_inbox (channel_id);
CREATE INDEX ix_player_playback_inbox_client_id ON public.player_playback_inbox (client_id);
CREATE INDEX ix_player_playback_inbox_branch_id ON public.player_playback_inbox (branch_id);
COMMIT;
\echo 'MIGRATION_020_PLAYER_PLAYBACK_INBOX=PASS'
