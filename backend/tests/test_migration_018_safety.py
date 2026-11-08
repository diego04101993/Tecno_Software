"""Real psql 16 controls for 018, restricted to a new labelled local QA cluster.

No application import/startup and no existing database mutation. Every method
owns a disposable database because the reviewed migration explicitly uses public.
Unconfigured discovery skips this PostgreSQL/Docker-specific test class.
"""
import hashlib
import json
import os
import re
import subprocess
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "backend/migrations/018_telemetry_guard_indexes.sql"
OLD_SHA256 = "c1cd6c3e3050d7d7f101db1a4b9a7add0c3d77db9868708e2ae39ea9c11854f3"
FIELDS = {
    "player_heartbeats": ("player_device_id", "channel_id", "client_id", "branch_id",
                          "current_content_id", "current_campaign_id", "current_layout_id"),
    "player_playback_events": ("player_device_id", "channel_id", "client_id", "branch_id",
                               "content_id", "campaign_id", "layout_id"),
}
REQUIRED = frozenset(("player_device_id", "channel_id", "client_id", "branch_id"))
MIGRATION_018_OBSERVATIONS = []


class Migration018SafetyTests(unittest.TestCase):
    """The opt-in command can address only an explicitly labelled Docker QA PG."""

    @classmethod
    def setUpClass(cls):
        encoded = os.environ.get("SECURITY_R5_2_PSQL_COMMAND_JSON")
        if not encoded:
            raise unittest.SkipTest("R5.2 requires its isolated local psql 16 QA fixture")
        cls.prefix = json.loads(encoded)
        cls.base = os.environ["SECURITY_R5_2_BASE_DATABASE"]
        cls.marker = os.environ["SECURITY_R5_2_PG_MARKER"]
        cls.system_id = os.environ["SECURITY_R5_2_PG_SYSTEM_ID"]
        if (cls.base != "security_r5_2_qa" or not re.fullmatch(r"r52_[a-f0-9]+", cls.marker)
                or not cls.system_id.isdigit()):
            raise RuntimeError("R5.2 unsafe or incomplete QA fence")
        prefix = cls.prefix
        if (not isinstance(prefix, list) or len(prefix) != 11
                or any(not isinstance(part, str) for part in prefix)
                or prefix[1:3] != ["--host", "npipe:////./pipe/dockerDesktopLinuxEngine"]
                or prefix[3:5] != ["exec", "-i"]
                or not re.fullmatch(r"[a-f0-9]{64}", prefix[5])
                or prefix[6:] != ["psql", "-X", "-w", "-U", "r52qa"]):
            raise RuntimeError("R5.2 only accepts the explicit local Docker/psql QA command")
        cls.docker = prefix[:3]
        cls.container_id = prefix[5]
        cls._container_fence()
        version = subprocess.run(prefix + ["--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30, check=True)
        if not re.search(r"psql \(PostgreSQL\) 16(?:\.|\s)", version.stdout):
            raise RuntimeError("R5.2 requires the real PostgreSQL 16 psql client")
        cls.psql_version = version.stdout.strip()
        cls._database_fence(cls.base)
        selected = os.environ.get("SECURITY_R5_2_SQL_PATH")
        cls.selected_sql = Path(selected).resolve() if selected else MIGRATION
        if cls.selected_sql != MIGRATION.resolve():
            if (not cls.selected_sql.is_relative_to(ROOT / ".tmp/SECURITY_R5_2")
                    or hashlib.sha256(cls.selected_sql.read_bytes()).hexdigest() != OLD_SHA256):
                raise RuntimeError("Only the exact preserved BEFORE SQL may override 018")
        before = os.environ.get("SECURITY_R5_2_BEFORE_SQL_PATH")
        cls.before_sql = Path(before).resolve() if before else None
        if cls.before_sql is not None and (
                not cls.before_sql.is_relative_to(ROOT / ".tmp/SECURITY_R5_2")
                or hashlib.sha256(cls.before_sql.read_bytes()).hexdigest() != OLD_SHA256):
            raise RuntimeError("R5.2 BEFORE bytes do not match the approved original")

    @classmethod
    def _container_fence(cls):
        result = subprocess.run(cls.docker + ["inspect", cls.container_id], capture_output=True,
                                text=True, encoding="utf-8", timeout=30, check=True)
        item = json.loads(result.stdout)[0]
        if (item["Id"] != cls.container_id or not item["State"]["Running"]
                or item["Config"].get("Labels", {}).get("qa.owner") != "SECURITY_R5_2"
                or item["HostConfig"]["NetworkMode"] != "none"
                or item["HostConfig"].get("PortBindings")
                or any((item["NetworkSettings"].get("Ports") or {}).values())):
            raise RuntimeError("R5.2 Docker fixture ownership/isolation fence failed")

    @classmethod
    def _run(cls, database, sql, variables=()):
        if not re.fullmatch(r"security_r5_2_qa|r52_[a-f0-9]{32}", database):
            raise RuntimeError("R5.2 refuses a database outside its fixture namespace")
        command = cls.prefix + ["-d", database, "-qAt", "-v", "ON_ERROR_STOP=1"]
        for name, value in variables:
            command += ["-v", name + "=" + value]
        return subprocess.run(command + ["-f", "-"], input=sql, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)

    @classmethod
    def _json(cls, database, sql):
        result = cls._run(database, sql)
        if result.returncode:
            raise AssertionError("QA introspection failed: " + result.stderr)
        return json.loads(result.stdout.strip())

    @classmethod
    def _database_fence(cls, database):
        cls._container_fence()
        identity = cls._json(database, """
            SELECT json_build_object('database', current_database(),
              'marker', current_setting('vwflex.qa_worker', true),
              'system_id', (SELECT system_identifier::text FROM pg_control_system()),
              'version_num', current_setting('server_version_num'));
        """)
        if (identity["database"] != database or identity["marker"] != cls.marker
                or identity["system_id"] != cls.system_id
                or int(identity["version_num"]) // 10000 != 16):
            raise RuntimeError("R5.2 PostgreSQL database/marker/system identity fence failed")
        return identity

    def setUp(self):
        self._database_fence(self.base)
        self.database = "r52_" + uuid.uuid4().hex
        created = self._run(self.base, 'CREATE DATABASE "' + self.database + '" TEMPLATE template0;')
        self.assertEqual(created.returncode, 0, created.stderr)
        self.addCleanup(self._drop_own_database)
        self.identity = self._database_fence(self.database)
        ddl = []
        for table, fields in FIELDS.items():
            columns = ["id varchar(36) PRIMARY KEY"] + [
                column + " varchar(36)" + (" NOT NULL" if column in REQUIRED else "")
                for column in fields]
            ddl.append("CREATE TABLE public." + table + " (" + ", ".join(columns) + ");")
        ddl.append("""
            CREATE TABLE public.r52_ddl_audit(command_tag text NOT NULL);
            CREATE FUNCTION public.r52_capture_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO public.r52_ddl_audit(command_tag) VALUES (TG_TAG);
            END; $$;
            CREATE EVENT TRIGGER r52_capture_ddl ON ddl_command_end
                EXECUTE FUNCTION public.r52_capture_ddl();
        """)
        setup = self._run(self.database, "\n".join(ddl))
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self._reset_audit()

    def _drop_own_database(self):
        self._database_fence(self.base)
        self._database_fence(self.database)
        result = self._run(self.base, 'DROP DATABASE "' + self.database + '";')
        self.assertEqual(result.returncode, 0, result.stderr)

    def _reset_audit(self):
        result = self._run(self.database, "TRUNCATE public.r52_ddl_audit;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._ddl_count(), 0)

    def _ddl_count(self):
        return self._json(self.database, "SELECT to_json(count(*)) FROM public.r52_ddl_audit;")

    def _catalog(self):
        return self._json(self.database, """
            SELECT COALESCE(json_agg(row_to_json(q) ORDER BY q.table_name, q.name), '[]'::json)
            FROM (SELECT t.relname AS table_name, c.relname AS name, i.indexrelid::text AS oid,
              i.indisprimary AS primary, i.indisvalid AS valid, i.indisready AS ready,
              i.indislive AS live, am.amname AS access_method,
              pg_get_indexdef(i.indexrelid) AS definition,
              pg_get_expr(i.indpred, i.indrelid) AS predicate
              FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
              JOIN pg_namespace n ON n.oid=t.relnamespace
              JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam
              WHERE n.nspname='public'
                AND t.relname IN ('player_heartbeats','player_playback_events')) q;
        """)

    def _migrate(self, expected, *, before=False):
        path = self.before_sql if before else self.selected_sql
        if path is None:
            self.fail("Configured R5.2 baseline controls require BEFORE_SQL_PATH; no implicit SKIP")
        variables = () if expected is None else (("r5_expected_database", expected),)
        result = self._run(self.database, path.read_text(encoding="utf-8-sig"), variables)
        MIGRATION_018_OBSERVATIONS.append({
            "test": self.id(), "database": self.database, "before": before,
            "sql_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "expected_database": expected, "exit_code": result.returncode,
            "ddl_commands": self._ddl_count(), "catalog": self._catalog(),
            "psql_version": self.psql_version, "identity": self.identity,
            "stdout": result.stdout, "stderr": result.stderr,
        })
        return result

    def _assert_unchanged_rejection(self, result, original):
        self.assertEqual(self._ddl_count(), 0, "Rejected migration must execute zero DDL")
        self.assertEqual(self._catalog(), original, "Rejected migration must leave every index unchanged")
        self.assertNotEqual(result.returncode, 0, "Aborted guard must produce a nonzero process exit code")

    def test_before_wrong_database_returns_zero_but_executes_no_ddl(self):
        original = self._catalog()
        result = self._migrate("security_r5_2_deliberately_wrong", before=True)
        self.assertEqual(result.returncode, 0, "Reproduce the actual psql16 old-guard bug")
        self.assertEqual(self._ddl_count(), 0)
        self.assertEqual(self._catalog(), original)

    def test_before_missing_expected_variable_returns_zero_but_executes_no_ddl(self):
        original = self._catalog()
        result = self._migrate(None, before=True)
        self.assertEqual(result.returncode, 0, "Reproduce the actual psql16 old-guard bug")
        self.assertEqual(self._ddl_count(), 0)
        self.assertEqual(self._catalog(), original)

    def test_wrong_database_returns_nonzero_and_executes_no_ddl(self):
        original = self._catalog()
        self._assert_unchanged_rejection(self._migrate("security_r5_2_deliberately_wrong"), original)

    def test_missing_expected_variable_returns_nonzero_and_executes_no_ddl(self):
        original = self._catalog()
        self._assert_unchanged_rejection(self._migrate(None), original)

    @staticmethod
    def _expected_definition(table, column):
        definition = "CREATE INDEX ix_" + table + "_" + column + " ON public." + table
        definition += " USING btree (" + column + ", id)"
        return definition if column in REQUIRED else definition + " WHERE (" + column + " IS NOT NULL)"

    def _assert_fourteen_exact_indexes(self):
        catalog = [row for row in self._catalog() if not row["primary"]]
        expected = {"ix_" + table + "_" + column: (table, column)
                    for table, fields in FIELDS.items() for column in fields}
        self.assertEqual({row["name"] for row in catalog}, set(expected))
        for row in catalog:
            table, column = expected[row["name"]]
            with self.subTest(index=row["name"]):
                self.assertEqual(row["definition"], self._expected_definition(table, column))
                self.assertEqual(row["access_method"], "btree")
                self.assertTrue(row["valid"] and row["ready"] and row["live"])
        return catalog

    def test_correct_database_builds_exact_valid_indexes_and_natural_plans(self):
        for table, fields in FIELDS.items():
            values = ["md5('" + table + "-' || g::text)"] + [
                ("CASE WHEN g%13=0 THEN NULL ELSE " if column not in REQUIRED else "")
                + "'fk_' || lpad((g%4096)::text,4,'0')"
                + (" END" if column not in REQUIRED else "") for column in fields]
            seed = self._run(self.database, "INSERT INTO public." + table + " SELECT "
                             + ", ".join(values) + " FROM generate_series(1,32768) g; ANALYZE public."
                             + table + ";")
            self.assertEqual(seed.returncode, 0, seed.stderr)
        self._reset_audit()
        result = self._migrate(self.database)
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_fourteen_exact_indexes()
        self.assertEqual(self._ddl_count(), 14)
        for table, fields in FIELDS.items():
            for column in fields:
                with self.subTest(table=table, column=column):
                    sql = ("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id," + ",".join(fields)
                           + " FROM public." + table + " WHERE " + column + "='fk_0001'"
                           + " ORDER BY id FOR UPDATE OF " + table + ";")
                    plan = self._json(self.database, sql)
                    def nodes(node):
                        yield node
                        for child in node.get("Plans", []):
                            yield from nodes(child)
                    eligible = "ix_" + table + "_" + column
                    indexes = {node["Index Name"] for node in nodes(plan[0]["Plan"])
                               if "Index Name" in node}
                    self.assertIn(eligible, indexes, "Natural selective plan must use its eligible FK index")
                    self.assertTrue(any(
                        node.get("Index Name") == eligible
                        and re.search(r"\b" + re.escape(column) + r"\b", node.get("Index Cond", ""))
                        for node in nodes(plan[0]["Plan"])), "Eligible FK must appear in Index Cond")
                    self.assertGreater(plan[0]["Plan"]["Actual Rows"], 0)
                    MIGRATION_018_OBSERVATIONS.append({"test": self.id(), "table": table,
                        "column": column, "natural_plan": plan, "planner_forced": False})

    def test_rerun_reuses_all_fourteen_index_oids_without_ddl(self):
        self.assertEqual(self._migrate(self.database).returncode, 0)
        original = self._assert_fourteen_exact_indexes()
        self._reset_audit()
        result = self._migrate(self.database)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._assert_fourteen_exact_indexes(), original)
        self.assertEqual(self._ddl_count(), 0)

    def test_existing_eligible_fk_leading_indexes_are_reused_without_redundancy(self):
        setup = self._run(self.database, """
            CREATE INDEX r52_legacy_channel ON public.player_heartbeats (channel_id);
            CREATE INDEX r52_legacy_partial ON public.player_playback_events (content_id)
                WHERE content_id IS NOT NULL;
        """)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        original = {row["name"]: row for row in self._catalog() if not row["primary"]}
        self._reset_audit()
        self.assertEqual(self._migrate(self.database).returncode, 0)
        indexes = {row["name"]: row for row in self._catalog() if not row["primary"]}
        self.assertEqual(len(indexes), 14)
        self.assertEqual(self._ddl_count(), 12)
        for name, row in original.items():
            self.assertEqual(indexes[name], row)
        self.assertNotIn("ix_player_heartbeats_channel_id", indexes)
        self.assertNotIn("ix_player_playback_events_content_id", indexes)
        self._reset_audit()
        self.assertEqual(self._migrate(self.database).returncode, 0)
        self.assertEqual({row["name"]: row for row in self._catalog() if not row["primary"]}, indexes)
        self.assertEqual(self._ddl_count(), 0)

    def test_conflicting_index_name_fails_before_any_new_ddl(self):
        setup = self._run(self.database, """
            CREATE INDEX ix_player_heartbeats_channel_id ON public.player_heartbeats (id);
        """)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        original = self._catalog()
        self._reset_audit()
        self._assert_unchanged_rejection(self._migrate(self.database), original)

    def test_missing_required_column_fails_before_any_new_ddl(self):
        setup = self._run(self.database,
                          "ALTER TABLE public.player_playback_events DROP COLUMN layout_id;")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        original = self._catalog()
        self._reset_audit()
        self._assert_unchanged_rejection(self._migrate(self.database), original)

    def test_invalid_concurrent_index_is_not_silently_reused_or_repaired(self):
        values = ["md5(g::text)"] + ["'same-fk'" for _ in FIELDS["player_heartbeats"]]
        setup = self._run(self.database, "INSERT INTO public.player_heartbeats SELECT "
                          + ",".join(values) + " FROM generate_series(1,10) g;")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        failed = self._run(self.database, """
            CREATE UNIQUE INDEX CONCURRENTLY ix_player_heartbeats_channel_id
                ON public.player_heartbeats (channel_id);
        """)
        self.assertNotEqual(failed.returncode, 0)
        original = self._catalog()
        invalid = next(row for row in original if row["name"] == "ix_player_heartbeats_channel_id")
        self.assertFalse(invalid["valid"])
        self._reset_audit()
        self._assert_unchanged_rejection(self._migrate(self.database), original)
