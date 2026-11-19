"""Heartbeat-only work reduction, with the original media and write proofs.

No application startup or production connection. SQLite retains the original
sanitizer path; PostgreSQL coverage explicitly opts into the existing owned R5
fixture and its per-test schema. No latency assertion uses a faster fake path.
"""
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from dataclasses import replace
import os
from types import MappingProxyType
import unittest
from unittest.mock import patch

from sqlalchemy import event, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BindParameter

from app.api.routes import player
from app.models.entities import (
    Branch, CampaignChannelAssignment, Channel, ChannelConnectionStatus, Client, ContentItem,
    DataSource, DataSourceType, Dataset, DatasetImport, DeviceOutput, PlayerDevice, PlayerHeartbeat,
    PlayerPlaybackEvent,
)
from app.schemas.domain import PlayerHeartbeatCreate
from app.services import media_references as media
from app.services import tenant_mutations as mutations
from app.services.runtime_read import runtime_reads
from app.services.tenant_ownership import RowRef, TenantGraph, freeze
from app.services.tenant_relations import RELATION_SPECS, model_class, model_spec
from test_tenant_gate_performance import GateFixture
import test_player_telemetry_concurrency as telemetry_tests


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def heartbeat_template_statement(statement):
    return bool(getattr(statement, "is_select", False) and any(
        str(getattr(node, "name", "")).startswith("heartbeat_locked_")
        for node in visitors.iterate(statement)))


class HeartbeatStatementTemplateTests(unittest.TestCase):
    def setUp(self):
        mutations._heartbeat_snapshot_template.cache_clear()
        self.addCleanup(mutations._heartbeat_snapshot_template.cache_clear)

    def test_template_reuses_structure_with_required_fresh_primary_key_bindings(self):
        shape = (mutations._heartbeat_snapshot_shape("ContentItem", "share", False),) * 2
        statement, fields = mutations._heartbeat_snapshot_template(shape)
        repeated, repeated_fields = mutations._heartbeat_snapshot_template(shape)
        self.assertIs(repeated, statement)
        self.assertIs(repeated_fields, fields)
        bindings = {node.key: node for node in visitors.iterate(statement)
                    if isinstance(node, BindParameter) and node.key.startswith("heartbeat_id_")}
        self.assertEqual(set(bindings), {"heartbeat_id_0", "heartbeat_id_1"})
        self.assertTrue(all(node.required and node.value is None and node.callable is None
                            for node in bindings.values()))
        compiled = statement.compile(dialect=postgresql.dialect())
        first = compiled.construct_params({"heartbeat_id_0": "synthetic-A-1", "heartbeat_id_1": "synthetic-A-2"})
        second = compiled.construct_params({"heartbeat_id_0": "synthetic-B-2", "heartbeat_id_1": "synthetic-B-1"})
        self.assertEqual(first["heartbeat_id_0"], "synthetic-A-1")
        self.assertEqual(second["heartbeat_id_0"], "synthetic-B-2")
        self.assertEqual(second["heartbeat_id_1"], "synthetic-B-1")
        with self.assertRaises(InvalidRequestError):
            compiled.construct_params({"heartbeat_id_0": "synthetic-missing-slot"})

    def test_template_key_tracks_spec_column_binding_order_and_lock_policy(self):
        base = mutations._heartbeat_snapshot_shape("ContentItem", "share", False)
        statement, _ = mutations._heartbeat_snapshot_template((base,))
        changed_spec = replace(model_spec("ContentItem"), columns=tuple(reversed(model_spec("ContentItem").columns)))
        original_spec = mutations.model_spec
        with patch.object(mutations, "model_spec", lambda model: changed_spec if model == "ContentItem" else original_spec(model)):
            reordered = mutations._heartbeat_snapshot_shape("ContentItem", "share", False)
            reordered_statement, _ = mutations._heartbeat_snapshot_template((reordered,))
            self.assertNotEqual(reordered, base)
            self.assertIsNot(reordered_statement, statement)
        original_class = mutations.model_class
        alias = aliased(ContentItem, name="heartbeat_template_binding")
        with patch.object(mutations, "model_class", lambda model: alias if model == "ContentItem" else original_class(model)):
            rebound = mutations._heartbeat_snapshot_shape("ContentItem", "share", False)
            rebound_statement, _ = mutations._heartbeat_snapshot_template((rebound,))
            self.assertNotEqual(rebound, base)
            self.assertIsNot(rebound_statement, statement)
        update_shape = mutations._heartbeat_snapshot_shape("ContentItem", "update", True)
        update_statement, _ = mutations._heartbeat_snapshot_template((update_shape,))
        self.assertNotEqual(update_shape, base)
        self.assertIsNot(update_statement, statement)
        self.assertIn("FOR SHARE", str(statement.compile(dialect=postgresql.dialect())))
        self.assertIn("FOR UPDATE NOWAIT", str(update_statement.compile(dialect=postgresql.dialect())))

    def test_template_cache_is_bounded_to_32_backend_shapes(self):
        shape = mutations._heartbeat_snapshot_shape("Client", "share", False)
        for slots in range(1, 35):
            mutations._heartbeat_snapshot_template((shape,) * slots)
        info = mutations._heartbeat_snapshot_template.cache_info()
        self.assertEqual(info.maxsize, 32)
        self.assertEqual(info.currsize, 32)
        self.assertEqual(info.misses, 34)


class _HeartbeatWorkCases:
    def payload(self, **overrides):
        a = self.ids["A"]
        values = dict(status="online", current_content_id=a["content"],
                      current_campaign_id=a["campaign"], current_layout_id=a["layout"])
        values.update(overrides)
        return values

    def post(self, payload=None):
        return self.http.post("/api/player/heartbeat",
            headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
            json=self.payload() if payload is None else payload)

    def latest(self):
        with Session(self.engine) as db:
            row = db.scalar(select(PlayerHeartbeat).order_by(PlayerHeartbeat.received_at.desc()))
            self.assertIsNotNone(row)
            db.expunge(row)
            return row

    def test_ack_matches_committed_history_and_preserves_sqlite_serialization(self):
        after_commit, post_commit_sql = [], []
        original = Session.commit

        def commit(db):
            heartbeat_write = any(isinstance(row, PlayerHeartbeat) for row in db.new)
            result = original(db)
            if heartbeat_write:
                after_commit.append(True)
            return result

        def sql(_connection, _cursor, statement, _params, _context, _many):
            if after_commit:
                post_commit_sql.append(statement.split(None, 1)[0])

        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            with patch.object(Session, "commit", commit):
                response = self.post()
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(after_commit, [True])
        expected_reads = [] if self.engine.dialect.name == "postgresql" else ["SELECT", "SELECT", "SELECT"]
        self.assertEqual(post_commit_sql, expected_reads,
                         "PostgreSQL ACK must avoid redundant reads; SQLite retains its legacy read path")
        self.assertEqual(self.engine.pool.checkedout(), 0)
        row = self.latest()
        self.assertEqual(set(response.json()), {"ok", "received_at", "channel_id"})
        self.assertIs(response.json()["ok"], True)
        self.assertEqual(response.json()["channel_id"], row.channel_id)
        received = datetime.fromisoformat(response.json()["received_at"].replace("Z", "+00:00"))
        self.assertEqual(utc(received), utc(row.received_at))
        self.assertEqual(received.tzinfo is None, self.engine.dialect.name == "sqlite",
                         "SQLite's existing naive timestamp representation changed")

    def test_all_heartbeat_diagnostics_remain_individual_history(self):
        for number in range(4):
            response = self.post(self.payload(
                status="online", app_version="heartbeat-work-" + str(number),
                resolution={"width": 1920, "height": 1080}, mode="normal",
                cache_status={"sequence": number}, errors=[{"sequence": number}],
                local_time="2026-01-02T03:04:05Z", playback_position={"position": number}))
            self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as db:
            rows = list(db.scalars(select(PlayerHeartbeat).order_by(PlayerHeartbeat.received_at)))
            self.assertEqual(len(rows), 4, "Heartbeat work reduction discarded required history")
            self.assertEqual(len({row.id for row in rows}), 4)
            for number, row in enumerate(rows):
                self.assertEqual(row.status, "online")
                self.assertEqual(row.app_version, "heartbeat-work-" + str(number))
                self.assertEqual(row.resolution_json, {"width": 1920, "height": 1080})
                self.assertEqual(row.mode, "normal")
                self.assertEqual(row.cache_status_json, {"sequence": number})
                self.assertEqual(row.errors_json, [{"sequence": number}])
                self.assertEqual(utc(row.local_time), datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
                self.assertEqual(row.playback_position, {"position": number})
            self.assertEqual(db.get(PlayerDevice, self.ids["A"]["device"]).app_version, "heartbeat-work-3")
            self.assertEqual(utc(db.get(Channel, self.ids["A"]["channel"]).last_ping_at),
                             utc(rows[-1].received_at))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_foreign_optional_ids_omit_only_the_reported_bad_reference(self):
        for bad in ("content", "campaign", "layout"):
            with self.subTest(bad=bad):
                response = self.post(self.payload(**{"current_" + bad + "_id": self.ids["B"][bad]}))
                self.assertEqual(response.status_code, 200, response.text)
                row = self.latest()
                for key in ("content", "campaign", "layout"):
                    self.assertEqual(getattr(row, "current_" + key + "_id"),
                                     None if key == bad else self.ids["A"][key])

    def test_content_opaque_media_validation_is_not_replaced_by_file_or_tenant_check(self):
        foreign = "/media/" + self.ids["B"]["client"] + "/owned.png"
        encoded = foreign.replace("/media/", "/m%65dia/")
        cases = (
            ("source_url", encoded),
            ("metadata_json", {"label(" + encoded + ")": "synthetic"}),
            ("html_content", '<img src="' + encoded + '">'),
            ("text_content", foreign),
        )
        content = self.rows["A"]["content"]
        for field, value in cases:
            with self.subTest(field=field):
                previous = getattr(content, field)
                setattr(content, field, value)
                self.db.commit()
                response = self.post(self.payload(current_campaign_id=None))
                self.assertEqual(response.status_code, 200, response.text)
                row = self.latest()
                self.assertIsNone(row.current_content_id, "Foreign opaque media survived heartbeat sanitization")
                self.assertEqual(row.current_layout_id, self.ids["A"]["layout"])
                setattr(content, field, previous)
                self.db.commit()

    def test_layout_encoded_media_still_omits_layout_without_repair(self):
        layout = self.rows["A"]["layout"]
        zones = [{"image": "/m%65dia/" + self.ids["B"]["client"] + "/owned.png"}]
        layout.zones = zones
        self.db.commit()
        response = self.post(self.payload(current_campaign_id=None))
        self.assertEqual(response.status_code, 200, response.text)
        row = self.latest()
        self.assertIsNone(row.current_layout_id)
        self.assertEqual(row.current_content_id, self.ids["A"]["content"])
        with Session(self.engine) as db:
            self.assertEqual(db.get(type(layout), layout.id).zones, zones, "Sanitizer repaired stored layout")

    def test_incoming_batch_preserves_exact_edge_ids_in_one_roundtrip(self):
        a, b = self.ids["A"], self.ids["B"]
        owned = CampaignChannelAssignment(campaign_id=a["campaign"], channel_id=a["channel"])
        foreign = CampaignChannelAssignment(campaign_id=b["campaign"], channel_id=a["channel"])
        output = DeviceOutput(root_channel_id=a["channel"], channel_id=a["channel"], name="Both edges")
        unrelated = DeviceOutput(root_channel_id=b["channel"], channel_id=b["channel"], name="Other tenant")
        self.db.add_all([owned, foreign, output, unrelated])
        self.db.commit()
        edges = tuple(edge for edge in RELATION_SPECS if edge.target == "Channel"
                      and edge.model not in {"PlayerHeartbeat", "PlayerPlaybackEvent"})
        expected = {edge.key: set(self.db.scalars(select(model_class(edge.model).id).where(
            getattr(model_class(edge.model), edge.field) == a["channel"]))) for edge in edges}
        statements = []

        def sql(_connection, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            actual = mutations._heartbeat_incoming_ids(TenantGraph(self.db), RowRef("Channel", a["channel"]))
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(actual, expected, "Batching changed the authoritative FK-only discovery result")
        self.assertEqual(actual["CampaignChannelAssignment.channel_id"], {owned.id, foreign.id},
                         "An owner predicate concealed a foreign incoming dependency")
        self.assertIn(output.id, actual["DeviceOutput.channel_id"])
        self.assertIn(output.id, actual["DeviceOutput.root_channel_id"], "UNION lost a repeated ID on another edge")
        self.assertNotIn(unrelated.id, set().union(*actual.values()))
        self.assertEqual(len(statements), 1, "Ordinary Channel incoming discovery must use one SQL roundtrip")

    def test_heartbeat_batches_incoming_discovery_in_both_fresh_phases(self):
        statements = []

        def sql(_connection, _cursor, statement, _params, _context, _many):
            if "UNION ALL" in statement.upper() and "AS relation_key" in statement:
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            response = self.post()
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(statements), 2, "Each fresh heartbeat proof needs its own batched incoming query")
        edges = [edge for edge in RELATION_SPECS if edge.target == "Channel"
                 and edge.model not in {"PlayerHeartbeat", "PlayerPlaybackEvent"}]
        self.assertTrue(all(statement.upper().count("UNION ALL") == len(edges) - 1
                            for statement in statements))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_foreign_incoming_assignment_is_not_hidden_by_batch(self):
        self.db.add(CampaignChannelAssignment(campaign_id=self.ids["B"]["campaign"],
                                              channel_id=self.ids["A"]["channel"]))
        self.db.commit()
        response = self.post()
        self.assertEqual(response.status_code, 409, response.text)
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(PlayerHeartbeat)))
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_second_phase_rediscovers_new_foreign_incoming_assignment(self):
        original = mutations._graph_validation_phase
        phases = []

        @contextmanager
        def phase(graph, gate=None):
            phases.append(True)
            with original(graph, gate):
                yield
            if len(phases) == 1:
                # A controlled inter-phase write in the same transaction tests
                # freshness without bypassing PostgreSQL's real parent locks.
                graph.session.execute(insert(CampaignChannelAssignment).values(
                    campaign_id=self.ids["B"]["campaign"], channel_id=self.ids["A"]["channel"]))

        with patch.object(mutations, "_graph_validation_phase", phase):
            response = self.post()
        self.assertEqual(phases, [True, True])
        self.assertEqual(response.status_code, 409, "A negative incoming result escaped its first proof phase")
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(PlayerHeartbeat)))
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_second_phase_rechecks_media_authority(self):
        original = mutations._graph_validation_phase
        phases = []

        @contextmanager
        def phase(graph, gate=None):
            phases.append(True)
            with original(graph, gate):
                yield
            if len(phases) == 1:
                graph.session.execute(update(ContentItem).where(
                    ContentItem.id == self.ids["B"]["content"]).values(
                        file_path=self.rows["A"]["content"].file_path))

        with patch.object(mutations, "_graph_validation_phase", phase):
            response = self.post()
        self.assertEqual(phases, [True, True])
        self.assertEqual(response.status_code, 409, "Media authority was reused across proof phases")
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(PlayerHeartbeat)))
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_current_playback_label_is_committed_with_original_precedence(self):
        a = self.ids["A"]
        cases = ((self.payload(), self.rows["A"]["content"].name),
                 (self.payload(current_content_id=None), self.rows["A"]["campaign"].name),
                 (self.payload(current_content_id=None, current_campaign_id=None),
                  "Layout " + self.rows["A"]["layout"].name),
                 (self.payload(current_content_id=None, current_campaign_id=None, current_layout_id=None),
                  "Layout " + self.rows["A"]["layout"].name))
        for payload, label in cases:
            response = self.post(payload)
            self.assertEqual(response.status_code, 200, response.text)
            with Session(self.engine) as db:
                self.assertEqual(db.get(Channel, a["channel"]).current_playback, label)
                row = db.scalar(select(PlayerHeartbeat).order_by(PlayerHeartbeat.received_at.desc()))
                for field in ("current_content_id", "current_campaign_id", "current_layout_id"):
                    self.assertEqual(getattr(row, field), payload[field])
                self.assertEqual(utc(row.received_at), utc(datetime.fromisoformat(
                    response.json()["received_at"].replace("Z", "+00:00"))))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_heartbeat_auth_failures_preserve_401_403_and_no_activity(self):
        for authorization in (None, "Bearer synthetic-unknown-player-token"):
            with self.subTest(authorization_present=authorization is not None):
                headers = {} if authorization is None else {"Authorization": authorization}
                response = self.http.post("/api/player/heartbeat", headers=headers, json=self.payload())
                self.assertEqual(response.status_code, 401, response.text)
        cases = (("device", "is_active", False, 401),
                 ("client", "status", "suspended", 403),
                 ("branch", "is_active", False, 403),
                 ("device", "client_id", self.ids["B"]["client"], 403),
                 ("device", "branch_id", self.ids["B"]["branch"], 403),
                 ("channel", "branch_id", self.ids["B"]["branch"], 403))
        for model, field, value, expected in cases:
            with self.subTest(model=model, field=field):
                row = self.rows["A"][model]
                previous = getattr(row, field)
                setattr(row, field, value)
                self.db.commit()
                try:
                    response = self.post()
                    self.assertEqual(response.status_code, expected, response.text)
                    with Session(self.engine) as db:
                        self.assertIsNone(db.scalar(select(PlayerHeartbeat)))
                        self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
                        self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)
                finally:
                    setattr(row, field, previous)
                    self.db.commit()
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_status_and_empty_version_preserve_existing_persisted_semantics(self):
        self.rows["A"]["device"].app_version = "preserved-heartbeat-version"
        self.db.commit()
        for reported, expected in (("OFFLINE", ChannelConnectionStatus.OFFLINE),
                                   ("custom-status", ChannelConnectionStatus.UNKNOWN)):
            response = self.post(self.payload(status=reported, app_version=""))
            self.assertEqual(response.status_code, 200, response.text)
            with Session(self.engine) as db:
                channel = db.get(Channel, self.ids["A"]["channel"])
                device = db.get(PlayerDevice, self.ids["A"]["device"])
                row = db.scalar(select(PlayerHeartbeat).order_by(PlayerHeartbeat.received_at.desc()))
                self.assertEqual(channel.status, expected)
                self.assertEqual(row.status, reported.lower())
                self.assertEqual(row.app_version, "")
                self.assertEqual(device.app_version, "preserved-heartbeat-version")
                self.assertEqual(utc(channel.last_ping_at), utc(row.received_at))
                self.assertEqual(utc(device.last_seen_at), utc(row.received_at))
        self.assertEqual(self.engine.pool.checkedout(), 0)


class HeartbeatWorkSQLiteTests(_HeartbeatWorkCases, GateFixture):
    def test_sqlite_keeps_original_unshared_sanitizer(self):
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(builds.call_count, 3, "SQLite cannot assume a PostgreSQL read-only MVCC snapshot")


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"),
                     "isolated SECURITY_R5 PostgreSQL fixture not configured")
class HeartbeatWorkPostgresTests(_HeartbeatWorkCases, GateFixture):
    make_engine = telemetry_tests.PlayerTelemetryPostgresTests.make_engine

    def test_reused_statement_templates_bind_fresh_tenants_rows_and_labels(self):
        statements, turn = {}, ["A1"]

        def execute(state):
            if heartbeat_template_statement(state.statement):
                # Observe the object passed to Session.execute, before the
                # compiler's independent cache can substitute its own object.
                key = state.statement._generate_cache_key().key
                statements.setdefault(turn[0], {}).setdefault(key, state.statement)

        event.listen(Session, "do_orm_execute", execute)
        try:
            first = self.post()
            self.assertEqual(first.status_code, 200, first.text)
            turn[0] = "B"
            b = self.ids["B"]
            second = self.http.post("/api/player/heartbeat",
                headers={"Authorization": "Bearer " + self.rows["B"]["token"]},
                json={"status": "online", "current_content_id": b["content"],
                      "current_campaign_id": b["campaign"], "current_layout_id": b["layout"]})
            self.assertEqual(second.status_code, 200, second.text)
            self.rows["A"]["content"].name = "A fresh after template warmup"
            self.db.commit()
            turn[0] = "A2"
            third = self.post()
            self.assertEqual(third.status_code, 200, third.text)
        finally:
            event.remove(Session, "do_orm_execute", execute)
        common = set(statements["A1"]) & set(statements["B"]) & set(statements["A2"])
        self.assertGreaterEqual(len(common), 2)
        for key in common:
            self.assertIs(statements["A1"][key], statements["B"][key])
            self.assertIs(statements["A1"][key], statements["A2"][key])
        self.assertEqual(first.json()["channel_id"], self.ids["A"]["channel"])
        self.assertEqual(second.json()["channel_id"], b["channel"])
        self.assertEqual(third.json()["channel_id"], self.ids["A"]["channel"])
        with Session(self.engine) as db:
            rows = list(db.scalars(select(PlayerHeartbeat)))
            self.assertEqual(len(rows), 3)
            for row in rows:
                ids = self.ids["A"] if row.channel_id == self.ids["A"]["channel"] else b
                self.assertEqual((row.client_id, row.branch_id, row.player_device_id,
                    row.current_content_id, row.current_campaign_id, row.current_layout_id),
                    tuple(ids[key] for key in ("client", "branch", "device", "content", "campaign", "layout")))
            self.assertEqual(db.get(Channel, self.ids["A"]["channel"]).current_playback,
                             "A fresh after template warmup")
            self.assertEqual(db.get(Channel, b["channel"]).current_playback, "B")
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_cached_template_executes_ast_guard_again_and_denies_writable_cte(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        original_check = media._OwnershipValidationPhase.before_cursor
        original_template = mutations._heartbeat_snapshot_template
        executed, checked = [], []

        def execute(state):
            if heartbeat_template_statement(state.statement):
                executed.append(state.statement)

        def check(phase, connection, cursor, statement, parameters, context, many):
            expression = context.compiled.statement if context.compiled is not None else None
            if heartbeat_template_statement(expression):
                checked.append(expression)
            return original_check(phase, connection, cursor, statement, parameters, context, many)

        event.listen(Session, "do_orm_execute", execute)
        try:
            with patch.object(media._OwnershipValidationPhase, "before_cursor", check):
                response = self.post()
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            event.remove(Session, "do_orm_execute", execute)
        self.assertGreaterEqual(len(executed), 2)
        self.assertEqual(len(checked), len(executed), "A cached template skipped the current phase's AST proof")
        before_checks = len(checked)

        def poisoned(shape):
            statement, fields = original_template(shape)
            forbidden = insert(ContentItem).values(client_id=self.ids["A"]["client"],
                name="Forbidden cached-template write", type=self.rows["A"]["content"].type).returning(
                    ContentItem.id).cte("heartbeat_forbidden_write")
            return statement.add_cte(forbidden), fields

        with patch.object(mutations, "_heartbeat_snapshot_template", poisoned), patch.object(
                media._OwnershipValidationPhase, "before_cursor", check):
            response = self.post()
        self.assertEqual(response.status_code, 409, "The cached-statement path bypassed the DML AST guard")
        self.assertGreater(len(checked), before_checks)
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(ContentItem.id).where(
                ContentItem.name == "Forbidden cached-template write")))
            self.assertEqual(len(list(db.scalars(select(PlayerHeartbeat)))), 2)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_native_snapshot_batches_preserve_typed_values_missing_rows_and_fresh_phases(self):
        self.rows["A"]["content"].metadata_json = {"label": "synthetic", "nested": [1, True, None]}
        self.rows["A"]["channel"].last_ping_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.db.commit()
        original = mutations._heartbeat_preload_snapshots
        observed, missing, batched_sql = {}, set(), []

        def types(value):
            if isinstance(value, MappingProxyType):
                return type(value), tuple((key, types(item)) for key, item in value.items())
            if isinstance(value, tuple):
                return type(value), tuple(types(item) for item in value)
            return type(value)

        def preload(graph, gate, operation, references):
            loaded = original(graph, gate, operation, references)
            phase = bool(getattr(graph, "media_validation_locked", False))
            for ref in loaded:
                cls = model_class(ref.model)
                record = graph.session.execute(select(*(getattr(cls, key) for key in
                    model_spec(ref.model).columns)).where(cls.id == ref.identity)).mappings().one_or_none()
                actual = graph.snapshots[ref]
                if record is None:
                    self.assertIsNone(actual)
                    if ref.model == "PlayerHeartbeat":
                        missing.add(phase)
                    continue
                expected = freeze(dict(record))
                self.assertIs(type(actual), MappingProxyType)
                self.assertEqual(actual, expected)
                self.assertEqual(types(actual), types(expected), "Typed values were coerced through JSON")
                self.assertEqual(set(actual), set(model_spec(ref.model).columns))
                self.assertIs(graph._snapshot_lock_proofs[ref][0], actual)
                self.assertIs(graph._snapshot_lock_proofs[ref][1], gate.transaction)
                observed.setdefault(ref, {})[phase] = actual
            return loaded

        def sql(_connection, _cursor, statement, _params, _context, _many):
            if "heartbeat_locked_" in statement:
                batched_sql.append(statement)

        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            with patch.object(mutations, "_heartbeat_preload_snapshots", preload):
                response = self.post()
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(missing, {False, True}, "A negative history snapshot escaped a proof phase")
        for model in ("ContentItem", "Campaign", "Layout", "PlayerDevice", "Branch", "Client"):
            row = observed[RowRef(model, self.ids["A"][{"ContentItem": "content", "Campaign": "campaign",
                "Layout": "layout", "PlayerDevice": "device", "Branch": "branch", "Client": "client"}[model]])]
            self.assertEqual(set(row), {False, True})
            self.assertIsNot(row[False], row[True], "A row snapshot was reused across proof phases")
        self.assertGreaterEqual(len(batched_sql), 2)
        self.assertTrue(all("FOR SHARE" in statement or "FOR UPDATE" in statement for statement in batched_sql))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_native_snapshot_batch_physically_locks_content_rows(self):
        original = mutations._heartbeat_preload_snapshots
        tested_phases = set()

        def preload(graph, gate, operation, references):
            loaded = original(graph, gate, operation, references)
            phase = bool(getattr(graph, "media_validation_locked", False))
            target = RowRef("ContentItem", self.ids["A"]["content"])
            if target in loaded and phase not in tested_phases:
                with Session(self.engine) as other:
                    from sqlalchemy import text
                    other.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    with self.assertRaises(DBAPIError) as blocked:
                        other.execute(update(ContentItem).where(ContentItem.id == target.identity).values(
                            name="Competing synthetic update"))
                    self.assertEqual(getattr(blocked.exception.orig, "sqlstate", None), "55P03",
                                     "Typed snapshots did not retain their real PostgreSQL row lock")
                    other.rollback()
                tested_phases.add(phase)
            return loaded

        with patch.object(mutations, "_heartbeat_preload_snapshots", preload):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(tested_phases, {False, True})
        with Session(self.engine) as db:
            self.assertEqual(db.get(ContentItem, self.ids["A"]["content"]).name, "A")
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_heartbeat_media_owner_batch_matches_original_values_and_strict_errors(self):
        from app.services.heartbeat_queries import heartbeat_query_scope
        a = self.ids["A"]
        source = DataSource(client_id=a["client"], name="Heartbeat owner source",
                            source_type=DataSourceType.FILE_UPLOAD)
        self.db.add(source)
        self.db.flush()
        dataset = Dataset(client_id=a["client"], data_source_id=source.id,
                          name="Heartbeat owner dataset", slug="heartbeat-owner-dataset")
        self.db.add(dataset)
        self.db.flush()
        self.db.add_all([
            DatasetImport(dataset_id=dataset.id, source_filename="relative.csv",
                          storage_path=a["client"] + "/relative.csv"),
            DatasetImport(dataset_id=dataset.id, source_filename="prefixed.csv",
                          storage_path="media/" + a["client"] + "/prefixed.csv"),
            DatasetImport(dataset_id=dataset.id, source_filename="empty.csv", storage_path=""),
            ContentItem(client_id=a["client"], name="No path", type=self.rows["A"]["content"].type,
                        file_path=None),
            ContentItem(client_id=a["client"], name="Invalid path", type=self.rows["A"]["content"].type,
                        file_path="../synthetic-outside.png"),
        ])
        self.db.commit()
        statements = []

        def sql(_connection, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        with Session(self.engine) as db:
            event.listen(self.engine, "before_cursor_execute", sql)
            try:
                original = media.media_reference_owners(db, self.root)
                self.assertEqual(len(statements), 3)
                statements.clear()
                with heartbeat_query_scope(db):
                    batched = media.media_reference_owners(db, self.root)
                self.assertEqual(len(statements), 1, "Heartbeat authority map still makes three roundtrips")
                self.assertIn("LEFT OUTER JOIN", statements[0].upper(),
                              "DatasetImport's authoritative outer join was narrowed")
                self.assertEqual(batched, original)
                self.assertEqual(batched[self.rows["A"]["path"].resolve()], {a["client"]})
                self.assertEqual(batched[(self.root / a["client"] / "relative.csv").resolve()], {a["client"]})
                self.assertEqual(batched[(self.root / a["client"] / "prefixed.csv").resolve()], {a["client"]})
                statements.clear()
                self.assertEqual(media.media_reference_owners(db, self.root), original)
                self.assertEqual(len(statements), 3, "Heartbeat batching escaped into an ordinary owner read")
                with self.assertRaises(media.MediaPathError):
                    media.media_reference_owners(db, self.root, strict=True)
                with heartbeat_query_scope(db), self.assertRaises(media.MediaPathError):
                    media.media_reference_owners(db, self.root, strict=True)
            finally:
                event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_heartbeat_batch_scope_does_not_opt_in_runtime_or_playback(self):
        from app.services import heartbeat_queries
        original = media.media_reference_owners
        observed, current = {}, ["heartbeat"]

        def owners(db, *args, **kwargs):
            observed.setdefault(current[0], []).append(heartbeat_queries.heartbeat_query_batch_active(db))
            return original(db, *args, **kwargs)

        with patch.object(media, "media_reference_owners", owners), patch.object(
                heartbeat_queries, "load_heartbeat_auth_rows",
                wraps=heartbeat_queries.load_heartbeat_auth_rows) as joined_auth:
            response = self.post()
            self.assertEqual(response.status_code, 200, response.text)
            heartbeat_auth_calls = joined_auth.call_count
            current[0] = "runtime"
            headers = {"Authorization": "Bearer " + self.rows["A"]["token"]}
            response = self.http.get("/api/player/runtime-config", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            current[0] = "playback"
            response = self.http.post("/api/player/playback-events", headers=headers,
                                      json={"content_id": self.ids["A"]["content"],
                                            "campaign_id": self.ids["A"]["campaign"],
                                            "layout_id": self.ids["A"]["layout"],
                                            "started_at": "2026-11-18T12:00:00Z", "status": "completed"})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(heartbeat_auth_calls, 1)
            self.assertEqual(joined_auth.call_count, heartbeat_auth_calls,
                             "Non-heartbeat authentication opted into the heartbeat-only join")
        self.assertTrue(observed["heartbeat"])
        self.assertTrue(all(observed["heartbeat"]))
        self.assertFalse(any(observed.get("runtime", ())))
        self.assertFalse(any(observed.get("playback", ())))
        # This case starts the real durable-playback consumer to prove it does
        # not inherit heartbeat scope. Quiesce that owned worker before testing
        # for leaked connections; an active polling Session is not a leak.
        from app.services.playback_inbox import stop_worker
        self.http.portal.call(stop_worker, self.http.app, self.engine)
        with Session(self.engine) as db:
            self.assertFalse(heartbeat_queries.heartbeat_query_batch_active(db))
            self.assertIsNotNone(db.get(PlayerPlaybackEvent, response.json()["event_id"]))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def mutate_after_read(self, action, *, payload=None):
        original = player._sanitize_heartbeat_telemetry
        calls = []

        def changed_after_read(value, db, client_id):
            result = original(value, db, client_id)
            with Session(self.engine) as other:
                action(other)
                other.commit()
            calls.append(True)
            return result

        with patch.object(player, "_sanitize_heartbeat_telemetry", changed_after_read):
            response = self.post(payload)
        self.assertEqual(calls, [True])
        self.assertEqual(self.engine.pool.checkedout(), 0)
        return response

    def assert_no_history(self):
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(PlayerHeartbeat)))

    def test_authenticated_token_rotation_after_read_is_not_rebased(self):
        def rotate(db):
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                player_token_hash="f" * 64))

        response = self.mutate_after_read(rotate)
        self.assertEqual(response.status_code, 409, "The read snapshot replaced the originally authenticated token")
        self.assert_no_history()

    def test_authenticated_channel_code_change_after_read_is_not_rebased(self):
        def change(db):
            db.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"]).values(
                channel_code="TC-HBWORK-CHANGED"))

        response = self.mutate_after_read(change)
        self.assertEqual(response.status_code, 409)
        self.assert_no_history()

    def test_authenticated_branch_move_after_read_is_not_rebased(self):
        branch = Branch(client_id=self.ids["A"]["client"], name="Heartbeat move", code="HBMOVE")
        self.db.add(branch)
        self.db.commit()
        branch_id = branch.id

        def move(db):
            db.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"]).values(branch_id=branch_id))
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(branch_id=branch_id))

        response = self.mutate_after_read(move)
        self.assertEqual(response.status_code, 409)
        self.assert_no_history()

    def test_authenticated_tenant_move_after_read_is_not_rebased_even_without_optional_ids(self):
        def move(db):
            scope = dict(client_id=self.ids["B"]["client"], branch_id=self.ids["B"]["branch"])
            db.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"]).values(**scope))
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(**scope))

        response = self.mutate_after_read(move, payload={"status": "online"})
        self.assertEqual(response.status_code, 409)
        self.assert_no_history()

    def test_authenticated_device_channel_move_after_read_remains_rejected(self):
        channel = Channel(client_id=self.ids["A"]["client"], branch_id=self.ids["A"]["branch"],
                          name="Heartbeat move target")
        self.db.add(channel)
        self.db.commit()
        channel_id = channel.id

        def move(db):
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                channel_id=channel_id))

        response = self.mutate_after_read(move)
        self.assertEqual(response.status_code, 409)
        self.assert_no_history()

    def test_device_deactivation_after_read_remains_rejected(self):
        def deactivate(db):
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(is_active=False))

        response = self.mutate_after_read(deactivate)
        self.assertEqual(response.status_code, 409)
        self.assert_no_history()

    def test_client_suspension_after_read_still_uses_fresh_guard(self):
        def suspend(db):
            db.execute(update(Client).where(Client.id == self.ids["A"]["client"]).values(status="suspended"))

        response = self.mutate_after_read(suspend)
        self.assertEqual(response.status_code, 403)
        self.assert_no_history()

    def test_branch_deactivation_after_read_still_uses_fresh_guard(self):
        def deactivate(db):
            db.execute(update(Branch).where(Branch.id == self.ids["A"]["branch"]).values(is_active=False))

        response = self.mutate_after_read(deactivate)
        self.assertEqual(response.status_code, 403)
        self.assert_no_history()

    def test_unchanged_authenticated_tuples_do_not_create_false_conflict(self):
        a = self.ids["A"]
        device, channel = self.rows["A"]["device"], self.rows["A"]["channel"]
        device_values = {key: getattr(device, key) for key in (
            "id", "client_id", "branch_id", "channel_id", "player_token_hash", "is_active")}
        channel_values = {key: getattr(channel, key) for key in ("id", "client_id", "branch_id", "channel_code")}

        def touch(db):
            db.execute(update(PlayerDevice).where(PlayerDevice.id == a["device"]).values(**device_values))
            db.execute(update(Channel).where(Channel.id == a["channel"]).values(**channel_values))

        response = self.mutate_after_read(touch)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.latest().channel_id, a["channel"])

    def test_concurrent_activity_does_not_rebase_scope_or_move_times_backwards(self):
        future = datetime.now(UTC) + timedelta(minutes=1)

        def activity(db):
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                last_seen_at=future, app_version="concurrent-version"))
            db.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"]).values(last_ping_at=future))

        response = self.mutate_after_read(activity)
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            self.assertEqual(utc(device.last_seen_at), future)
            self.assertEqual(utc(channel.last_ping_at), future)
            self.assertEqual(device.app_version, "concurrent-version")
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_optional_read_map_is_shared_only_inside_fresh_read_only_snapshot(self):
        original, builds = media.media_reference_owners, []

        def owners(db, root, **kwargs):
            scope = runtime_reads(db)
            phase = db.info.get(media._OWNERSHIP_VALIDATION_PHASE)
            builds.append((db, scope, phase))
            if scope is not None:
                self.assertEqual(db.connection().get_isolation_level(), "REPEATABLE READ")
                self.assertEqual(db.connection().exec_driver_sql("SHOW transaction_read_only").scalar_one(), "on")
            return original(db, root, **kwargs)

        with patch.object(media, "media_reference_owners", owners):
            for _ in range(2):
                response = self.post()
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(builds), 4, "Expected one read map and one fresh guarded map per heartbeat")
        for start in (0, 2):
            self.assertIsNotNone(builds[start][1])
            self.assertIsNone(builds[start][2])
            self.assertIsNone(builds[start + 1][1])
            self.assertIsNotNone(builds[start + 1][2])
            self.assertIsNot(builds[start][0], builds[start + 1][0])
        self.assertIsNot(builds[0][1], builds[2][1], "Read authority escaped into the next heartbeat")
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_fresh_guard_rejects_authority_change_after_optional_read(self):
        original = getattr(player, "_sanitize_heartbeat_telemetry",
                           lambda payload, db, client_id: player._sanitize_telemetry(payload, db, client_id, "current_"))
        called = []

        def changed_after_read(payload, db, client_id):
            result = original(payload, db, client_id)
            with Session(self.engine) as other:
                other.execute(update(ContentItem).where(ContentItem.id == self.ids["B"]["content"]).values(
                    file_path=self.rows["A"]["content"].file_path))
                other.commit()
            called.append(True)
            return result

        with patch.object(player, "_sanitize_heartbeat_telemetry", changed_after_read, create=True):
            response = self.post()
        self.assertEqual(called, [True])
        self.assertEqual(response.status_code, 409, response.text)
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(PlayerHeartbeat)))
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_optional_snapshot_failure_releases_its_connection_and_proof(self):
        original = media.media_reference_owners

        def fail_in_snapshot(db, root, **kwargs):
            if runtime_reads(db) is not None:
                raise RuntimeError("synthetic heartbeat snapshot failure")
            return original(db, root, **kwargs)

        sanitizer = getattr(player, "_sanitize_heartbeat_telemetry",
                            lambda payload, db, client_id: player._sanitize_telemetry(payload, db, client_id, "current_"))
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, device.channel_id)
            client_id = channel.client_id
            with patch.object(media, "media_reference_owners", fail_in_snapshot):
                with self.assertRaisesRegex(RuntimeError, "synthetic heartbeat snapshot failure"):
                    sanitizer(PlayerHeartbeatCreate(**self.payload()), db, client_id)
            self.assertIsNone(runtime_reads(db), "Snapshot read proof escaped onto the writer session")
        self.assertEqual(self.engine.pool.checkedout(), 0)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.engine.pool.checkedout(), 0)
