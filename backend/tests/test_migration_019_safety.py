"""Static fail-closed controls for the reviewed standalone psql 16 index script.

These tests never run SQL or contact any database. Both reviewed repository
artifacts must exist. Real PostgreSQL plan/verifier evidence belongs to the
separately fenced, isolated QA fixture.
"""
import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NAME = "019_player_heartbeats_latest_index.sql"
VERIFY_NAME = "019_player_heartbeats_latest_index_verify.sql"
MIGRATION = ROOT / "backend/migrations" / NAME
VERIFIER = ROOT / "backend/migrations" / VERIFY_NAME
INDEX = "ix_player_heartbeats_channel_received_at_id"


def uncomment(source):
    return re.sub(r"--[^\n]*", "", source)


def top_level(source):
    source = uncomment(source)
    source = re.sub(r"\$([A-Za-z_][A-Za-z_0-9]*)\$.*?\$\1\$", "", source, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", source)


def catalog_blocks(source):
    return re.findall(r"DO \$migration_019_catalog\$(.*?)\$migration_019_catalog\$;", source, flags=re.S)


class Migration019SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf8")
        cls.verify = VERIFIER.read_text(encoding="utf8")

    def test_018_bytes_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / "backend/migrations/018_telemetry_guard_indexes.sql").read_bytes()).hexdigest(),
                         "05af175ae7c588c490d2bcf96dd46ca8072f70251cc4ba7734eba6924272a797")

    def test_both_artifacts_use_lf_utf8_without_bom(self):
        for path in (MIGRATION, VERIFIER):
            with self.subTest(path=path):
                data = path.read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\r", data)
                data.decode("utf8")

    def test_database_identity_mandatory_and_errors_are_sql_failures(self):
        for source in (self.sql, self.verify):
            with self.subTest(artifact=source[:30]):
                self.assertLess(source.index(r"\set ON_ERROR_STOP on"), source.index("SELECT current_database()"))
                self.assertIn(r"\if :{?expected_database}", source)
                self.assertIn("current_database() = :'expected_database'", source)
                self.assertIn("RAISE EXCEPTION 'MIGRATION_019_EXPECTED_DATABASE_REQUIRED'", source)
                self.assertIn("RAISE EXCEPTION 'MIGRATION_019_DATABASE_MISMATCH'", source)
                self.assertNotRegex(uncomment(source), r"\\(?:q|quit)\b")

    def test_version_requires_postgresql_16(self):
        for source in (self.sql, self.verify):
            self.assertIn("current_setting('server_version_num')::integer / 10000 <> 16", source)
            self.assertIn("RAISE EXCEPTION 'MIGRATION_019_REQUIRES_POSTGRESQL_16'", source)

    def test_single_exact_concurrent_create_without_include(self):
        source = uncomment(self.sql)
        ddl = "CREATE INDEX CONCURRENTLY " + INDEX + " ON public.player_heartbeats (channel_id, received_at DESC, id DESC);"
        self.assertEqual(source.count("CREATE INDEX CONCURRENTLY"), 1)
        self.assertIn("SELECT '" + ddl + "'", source)
        self.assertIn("WHERE pg_catalog.to_regclass('public." + INDEX + "') IS NULL\n\\gexec", source)
        self.assertNotRegex(source, r"\bINCLUDE\s*\(")
        self.assertNotIn("IF NOT EXISTS", source)

    def test_build_script_has_no_outer_transaction(self):
        self.assertIn(r"\set AUTOCOMMIT on", self.sql)
        self.assertNotRegex(top_level(self.sql), r"(?im)^\s*(BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK)\b")

    def test_advisory_session_lock_covers_every_catalog_check_and_ddl(self):
        sql = self.sql
        acquire = sql.index("pg_catalog.pg_try_advisory_lock(190019,")
        first_guard = sql.index("DO $migration_019_catalog$")
        create = sql.index("SELECT 'CREATE INDEX CONCURRENTLY")
        final_guard = sql.rindex("DO $migration_019_catalog$")
        release = sql.index("pg_catalog.pg_advisory_unlock(190019,")
        self.assertLess(acquire, first_guard)
        self.assertLess(first_guard, create)
        self.assertLess(create, final_guard)
        self.assertLess(final_guard, release)
        self.assertEqual(sql.count("pg_catalog.hashtext(current_database() || ':public." + INDEX + "')"), 2)
        self.assertIn("RAISE EXCEPTION 'MIGRATION_019_ANOTHER_RUN_ACTIVE'", sql)
        self.assertIn("RAISE EXCEPTION 'MIGRATION_019_SESSION_LOCK_LOST'", sql)
        self.assertNotIn("pg_advisory_xact_lock", sql)

    def test_precondition_and_postcondition_check_the_same_exact_shape(self):
        before, after = catalog_blocks(self.sql)
        required = "IF index_oid IS NULL THEN\n        RAISE EXCEPTION 'MIGRATION_019_REQUIRED_INDEX_MISSING';\n    END IF;"
        absent = "-- Absence is allowed only before the explicit concurrent build."
        self.assertEqual(before.replace(absent, required), after)
        self.assertEqual(catalog_blocks(self.verify), [after])

    def test_name_conflict_invalid_or_missing_object_is_never_repaired(self):
        for block in catalog_blocks(self.sql) + catalog_blocks(self.verify):
            self.assertIn("i.indexrelid = index_oid AND i.indrelid = table_oid", block)
            self.assertIn("i.indisvalid AND i.indisready AND i.indislive", block)
            self.assertIn("RAISE EXCEPTION 'MIGRATION_019_INDEX_NAME_DEFINITION_OR_STATE_CONFLICT'", block)
            self.assertIn("RAISE EXCEPTION 'MIGRATION_019_REQUIRED_COLUMN_MISSING'", block)
            self.assertIn("RAISE EXCEPTION 'MIGRATION_019_REQUIRED_TABLE_MISSING_OR_UNSUPPORTED'", block)
        for source in (self.sql, self.verify):
            self.assertNotRegex(top_level(source), r"(?i)\b(DROP|REINDEX|ALTER|DELETE|TRUNCATE|UPDATE|INSERT)\b")

    def test_key_order_direction_null_order_and_no_include_are_exact(self):
        for block in catalog_blocks(self.sql) + catalog_blocks(self.verify):
            self.assertIn("i.indnkeyatts = 3 AND i.indnatts = 3", block)
            self.assertIn("i.indkey[0] = channel_att AND i.indkey[1] = received_att AND i.indkey[2] = id_att", block)
            self.assertIn("i.indoption[0] = 0 AND i.indoption[1] = 3 AND i.indoption[2] = 3", block)
            self.assertIn("i.indexprs IS NULL AND i.indpred IS NULL", block)
            self.assertIn("NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion", block)

    def test_opclass_and_collation_are_exact_defaults(self):
        for block in catalog_blocks(self.sql) + catalog_blocks(self.verify):
            for offset, prefix, opclass in ((0, "channel", "text_ops"), (1, "received", "timestamptz_ops"), (2, "id", "text_ops")):
                self.assertIn(f"i.indcollation[{offset}] = {prefix}_col.attcollation", block)
                self.assertIn(f"{prefix}_class.opcname = '{opclass}' AND {prefix}_class.opcdefault", block)
                self.assertIn(f"{prefix}_class.opcnamespace = 'pg_catalog'::pg_catalog.regnamespace", block)

    def test_rendered_definition_and_storage_options_also_match_exactly(self):
        for block in catalog_blocks(self.sql) + catalog_blocks(self.verify):
            self.assertIn("CREATE INDEX " + INDEX + " ON public.player_heartbeats USING btree (channel_id, received_at DESC, id DESC)", block)
            self.assertIn("pg_catalog.regexp_replace(pg_catalog.pg_get_indexdef(index_oid), '[[:space:]]+', ' ', 'g') = expected_definition", block)
            self.assertIn("idx.reloptions IS NULL AND idx.reltablespace = 0", block)

    def test_verifier_is_standalone_readonly_and_cannot_execute_ddl(self):
        self.assertIn("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;", self.verify)
        self.assertIn("SET LOCAL search_path = pg_catalog;", self.verify)
        self.assertNotIn(r"\gexec", self.verify)
        self.assertNotRegex(top_level(self.verify), r"(?i)\b(CREATE|DROP|REINDEX|ALTER|UPDATE|INSERT|DELETE|TRUNCATE)\b")
        self.assertNotIn("pg_try_advisory_lock", self.verify)
        self.assertIn("MIGRATION_019_READONLY_VERIFIER=PASS", self.verify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
