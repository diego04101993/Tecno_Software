-- R5 PREDEPLOY INDEX PHASE (psql, NOT a transactional migration).
-- Never run with --single-transaction, inside BEGIN, or from app startup.
-- Explicit approval is still required before running this on production.
-- Example for an ISOLATED QA database:
--   psql ... -v r5_expected_database=security_r5_qa -f 018_telemetry_guard_indexes.sql
-- No data/constraints/FKs/defaults are changed. Each index commits separately.
-- A failed concurrent build can leave an INVALID index: stop and inspect it;
-- this script deliberately does not drop/repair any existing index.
\set ON_ERROR_STOP on
\set AUTOCOMMIT on
\if :{?r5_expected_database}
\else
  \echo 'STOP: supply r5_expected_database explicitly'
  DO $r5_guard$ BEGIN RAISE EXCEPTION 'R5 database guard failed'; END; $r5_guard$;
\endif
SELECT current_database() = :'r5_expected_database' AS r5_correct_database \gset
\if :r5_correct_database
\else
  \echo 'STOP: unexpected database'
  DO $r5_guard$ BEGIN RAISE EXCEPTION 'R5 database guard failed'; END; $r5_guard$;
\endif

-- Serial builds and a small maintenance budget for the 2 CPU / 4 GB host.
-- lock_timeout bounds lock acquisition, NOT total build duration or disk/WAL.
SET lock_timeout = '2s';
SET statement_timeout = '0';
SET maintenance_work_mem = '64MB';
SET max_parallel_maintenance_workers = 0;

-- Fail before DDL on missing columns or conflicting/invalid names. An existing
-- valid B-tree with this leading key is reusable (including a composite index).
-- A partial index is reusable only for the exact FK IS NOT NULL predicate.
DO $r5$
DECLARE
    item record;
    column_number smallint;
    named_oid oid;
BEGIN
    FOR item IN
        SELECT t.tab, f.col, 'ix_' || t.tab || '_' || f.col AS idx
        FROM (VALUES ('player_heartbeats'), ('player_playback_events')) t(tab)
        CROSS JOIN LATERAL (
            VALUES ('player_device_id'), ('channel_id'), ('client_id'), ('branch_id'),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_content_id' ELSE 'content_id' END),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_campaign_id' ELSE 'campaign_id' END),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_layout_id' ELSE 'layout_id' END)
        ) f(col)
    LOOP
        SELECT attnum INTO column_number FROM pg_catalog.pg_attribute
        WHERE attrelid = to_regclass('public.' || item.tab)
          AND attname = item.col AND attnum > 0 AND NOT attisdropped;
        IF column_number IS NULL THEN
            RAISE EXCEPTION 'R5 missing required column %.%', item.tab, item.col;
        END IF;
        named_oid := to_regclass('public.' || item.idx);
        IF named_oid IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
            JOIN pg_catalog.pg_am am ON am.oid = c.relam
            WHERE i.indexrelid = named_oid
              AND i.indrelid = to_regclass('public.' || item.tab)
              AND i.indisvalid AND i.indisready AND i.indislive
              AND am.amname = 'btree' AND i.indnkeyatts >= 1
              AND i.indkey[0] = column_number AND i.indexprs IS NULL
              AND (i.indpred IS NULL OR pg_get_expr(i.indpred, i.indrelid) IN (
                  format('(%I IS NOT NULL)', item.col), format('%I IS NOT NULL', item.col)))
        ) THEN
            RAISE EXCEPTION 'R5 conflicting or invalid index name: %', item.idx;
        END IF;
    END LOOP;
END
$r5$;

-- Newly built indexes use (FK, id): equality discovery plus stable row-lock order.
-- Existing usable FK-leading indexes are reused; they may require a local sort.
-- One row -> one concurrently built index. No IF NOT EXISTS name-only shortcut:
-- an equivalent index with another name also suppresses redundant creation.
WITH wanted AS (
    SELECT t.tab, f.col, 'ix_' || t.tab || '_' || f.col AS idx
    FROM (VALUES ('player_heartbeats'), ('player_playback_events')) t(tab)
    CROSS JOIN LATERAL (
        VALUES ('player_device_id'), ('channel_id'), ('client_id'), ('branch_id'),
               (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_content_id' ELSE 'content_id' END),
               (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_campaign_id' ELSE 'campaign_id' END),
               (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_layout_id' ELSE 'layout_id' END)
    ) f(col)
)
SELECT format('CREATE INDEX CONCURRENTLY %I ON public.%I (%I, id)%s;', w.idx, w.tab, w.col,
              CASE WHEN a.attnotnull THEN '' ELSE format(' WHERE %I IS NOT NULL', w.col) END)
FROM wanted w
JOIN pg_catalog.pg_attribute a ON a.attrelid = to_regclass('public.' || w.tab)
    AND a.attname = w.col AND a.attnum > 0 AND NOT a.attisdropped
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
    JOIN pg_catalog.pg_am am ON am.oid = c.relam
    WHERE i.indrelid = a.attrelid
      AND i.indisvalid AND i.indisready AND i.indislive
      AND am.amname = 'btree' AND i.indnkeyatts >= 1
      AND i.indkey[0] = a.attnum AND i.indexprs IS NULL
      AND (i.indpred IS NULL OR pg_get_expr(i.indpred, i.indrelid) IN (
          format('(%I IS NOT NULL)', w.col), format('%I IS NOT NULL', w.col)))
)
ORDER BY w.tab, w.col
\gexec

-- Postcondition: all fourteen real incoming FK equality shapes have an eligible
-- valid, ready index. This is index availability, not a production latency PASS.
DO $r5$
DECLARE item record;
BEGIN
    FOR item IN
        SELECT t.tab, f.col
        FROM (VALUES ('player_heartbeats'), ('player_playback_events')) t(tab)
        CROSS JOIN LATERAL (
            VALUES ('player_device_id'), ('channel_id'), ('client_id'), ('branch_id'),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_content_id' ELSE 'content_id' END),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_campaign_id' ELSE 'campaign_id' END),
                   (CASE WHEN t.tab = 'player_heartbeats' THEN 'current_layout_id' ELSE 'layout_id' END)
        ) f(col)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
            JOIN pg_catalog.pg_am am ON am.oid = c.relam
            JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE i.indrelid = to_regclass('public.' || item.tab) AND a.attname = item.col
              AND i.indisvalid AND i.indisready AND i.indislive
              AND am.amname = 'btree' AND i.indnkeyatts >= 1 AND i.indexprs IS NULL
              AND (i.indpred IS NULL OR pg_get_expr(i.indpred, i.indrelid) IN (
                  format('(%I IS NOT NULL)', item.col), format('%I IS NOT NULL', item.col)))
        ) THEN
            RAISE EXCEPTION 'R5 required index unavailable: %.%', item.tab, item.col;
        END IF;
    END LOOP;
END
$r5$;
\echo 'R5 telemetry index availability verified; production scale re-preflight still required.'
