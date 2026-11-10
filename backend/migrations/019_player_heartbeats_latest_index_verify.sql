-- 019 standalone verifier, PostgreSQL/psql 16; catalog reads only.
-- Usage: psql -X -v expected_database=THE_EXACT_DATABASE -f 019_player_heartbeats_latest_index_verify.sql
-- No migration or index build is executed by this verifier.
\set ON_ERROR_STOP on
\set AUTOCOMMIT on
\if :{?expected_database}
\else
  \echo 'STOP: supply expected_database explicitly'
  DO $migration_019_identity$ BEGIN RAISE EXCEPTION 'MIGRATION_019_EXPECTED_DATABASE_REQUIRED'; END; $migration_019_identity$;
\endif
SELECT current_database() = :'expected_database' AS migration_019_correct_database \gset
\if :migration_019_correct_database
\else
  \echo 'STOP: unexpected database'
  DO $migration_019_identity$ BEGIN RAISE EXCEPTION 'MIGRATION_019_DATABASE_MISMATCH'; END; $migration_019_identity$;
\endif
DO $migration_019_version$
BEGIN
    IF current_setting('server_version_num')::integer / 10000 <> 16 THEN
        RAISE EXCEPTION 'MIGRATION_019_REQUIRES_POSTGRESQL_16';
    END IF;
END
$migration_019_version$;

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL search_path = pg_catalog;
DO $migration_019_catalog$
DECLARE
    table_oid oid := pg_catalog.to_regclass('public.player_heartbeats');
    index_oid oid := pg_catalog.to_regclass('public.ix_player_heartbeats_channel_received_at_id');
    channel_att smallint;
    received_att smallint;
    id_att smallint;
    expected_definition constant text := 'CREATE INDEX ix_player_heartbeats_channel_received_at_id ON public.player_heartbeats USING btree (channel_id, received_at DESC, id DESC)';
BEGIN
    IF table_oid IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class WHERE oid = table_oid AND relkind = 'r'
    ) THEN
        RAISE EXCEPTION 'MIGRATION_019_REQUIRED_TABLE_MISSING_OR_UNSUPPORTED';
    END IF;
    SELECT attnum INTO channel_att FROM pg_catalog.pg_attribute
        WHERE attrelid = table_oid AND attname = 'channel_id' AND attnum > 0 AND NOT attisdropped;
    SELECT attnum INTO received_att FROM pg_catalog.pg_attribute
        WHERE attrelid = table_oid AND attname = 'received_at' AND attnum > 0 AND NOT attisdropped;
    SELECT attnum INTO id_att FROM pg_catalog.pg_attribute
        WHERE attrelid = table_oid AND attname = 'id' AND attnum > 0 AND NOT attisdropped;
    IF channel_att IS NULL OR received_att IS NULL OR id_att IS NULL THEN
        RAISE EXCEPTION 'MIGRATION_019_REQUIRED_COLUMN_MISSING';
    END IF;
    IF index_oid IS NULL THEN
        RAISE EXCEPTION 'MIGRATION_019_REQUIRED_INDEX_MISSING';
    END IF;
    IF index_oid IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_catalog.pg_namespace ns ON ns.oid = idx.relnamespace
        JOIN pg_catalog.pg_am am ON am.oid = idx.relam
        JOIN pg_catalog.pg_attribute channel_col ON channel_col.attrelid = table_oid AND channel_col.attnum = channel_att
        JOIN pg_catalog.pg_attribute received_col ON received_col.attrelid = table_oid AND received_col.attnum = received_att
        JOIN pg_catalog.pg_attribute id_col ON id_col.attrelid = table_oid AND id_col.attnum = id_att
        JOIN pg_catalog.pg_opclass channel_class ON channel_class.oid = i.indclass[0]
        JOIN pg_catalog.pg_opclass received_class ON received_class.oid = i.indclass[1]
        JOIN pg_catalog.pg_opclass id_class ON id_class.oid = i.indclass[2]
        WHERE i.indexrelid = index_oid AND i.indrelid = table_oid
          AND ns.nspname = 'public' AND idx.relkind = 'i' AND am.amname = 'btree'
          AND i.indisvalid AND i.indisready AND i.indislive
          AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
          AND NOT i.indnullsnotdistinct
          AND i.indnkeyatts = 3 AND i.indnatts = 3
          AND i.indkey[0] = channel_att AND i.indkey[1] = received_att AND i.indkey[2] = id_att
          AND i.indoption[0] = 0 AND i.indoption[1] = 3 AND i.indoption[2] = 3
          AND i.indcollation[0] = channel_col.attcollation
          AND i.indcollation[1] = received_col.attcollation
          AND i.indcollation[2] = id_col.attcollation
          AND channel_class.opcname = 'text_ops' AND channel_class.opcdefault
          AND channel_class.opcnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND received_class.opcname = 'timestamptz_ops' AND received_class.opcdefault
          AND received_class.opcnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND id_class.opcname = 'text_ops' AND id_class.opcdefault
          AND id_class.opcnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND i.indexprs IS NULL AND i.indpred IS NULL
          AND idx.reloptions IS NULL AND idx.reltablespace = 0
          AND pg_catalog.regexp_replace(pg_catalog.pg_get_indexdef(index_oid), '[[:space:]]+', ' ', 'g') = expected_definition
    ) THEN
        RAISE EXCEPTION 'MIGRATION_019_INDEX_NAME_DEFINITION_OR_STATE_CONFLICT';
    END IF;
END
$migration_019_catalog$;
SELECT pg_catalog.pg_get_indexdef('public.ix_player_heartbeats_channel_received_at_id'::pg_catalog.regclass) AS verified_index_definition;
COMMIT;
\echo 'MIGRATION_019_READONLY_VERIFIER=PASS'
