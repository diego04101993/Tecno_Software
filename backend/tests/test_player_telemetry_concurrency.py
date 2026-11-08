"""R5.1: real shared telemetry locks and authoritative structural races.

No application startup or physical lab is used. PostgreSQL tests require the
owned R5 fixture, and each test creates and drops only its own UUID schema.
Events coordinate races; the production ten-second timeout is never increased.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("R5.1 requires the isolated SQLite-configured test harness")

from fastapi import HTTPException
from sqlalchemy import create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from app.api.routes import branches, channels
from app.models.entities import (
    Branch, Campaign, Channel, Client, ContentItem, Layout, PlayerDevice,
    PlayerHeartbeat, PlayerPlaybackEvent, User,
)
from app.services import media_references as media
from app.services import tenant_mutations as mutations
from app.services.media_storage import MediaPathError
from app.services.tenant_ownership import RowRef, context_for_actor
from test_tenant_gate_performance import GateFixture


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _PlayerCases:
    def seed_legacy_history(self, count=1105, *, foreign_owner=False):
        a = self.ids["A"]
        identities = {}
        for model in (PlayerHeartbeat, PlayerPlaybackEvent):
            rows = []
            for _ in range(count):
                row = dict(id=str(uuid4()), player_device_id=a["device"], channel_id=a["channel"],
                           client_id=self.ids["B"]["client"] if foreign_owner else a["client"],
                           branch_id=a["branch"])
                prefix = "current_" if model is PlayerHeartbeat else ""
                row.update({prefix + key + "_id": a[key] for key in ("content", "campaign", "layout")})
                row["received_at" if model is PlayerHeartbeat else "started_at"] = datetime(2000, 1, 1, tzinfo=UTC)
                rows.append(row)
            self.db.execute(insert(model), rows)
            identities[model] = [row["id"] for row in rows]
        self.db.commit()
        return identities

    def request(self, kind="heartbeat", identity=None, *, foreign=False):
        identity = identity or dict(self.ids["A"], token=self.rows["A"]["token"])
        refs = self.ids["B" if foreign else "A"]
        headers = {"Authorization": "Bearer " + identity["token"]}
        if kind == "runtime":
            return self.http.get("/api/player/runtime-config", headers=headers)
        values = {"content_id": refs["content"], "campaign_id": refs["campaign"],
                  "layout_id": refs["layout"]}
        if kind == "heartbeat":
            values = {"current_" + key: value for key, value in values.items()}
            values.update(status="online", app_version="r51-synthetic")
        else:
            values.update(started_at=datetime.now(UTC).isoformat(), status="completed")
        return self.http.post("/api/player/" + ("heartbeat" if kind == "heartbeat" else "playback-events"),
                              headers=headers, json=values)

    def player_intents(self, db, kind="heartbeat", *, time=None, foreign=False):
        ids = self.ids["A"]
        channel, device = db.get(Channel, ids["channel"]), db.get(PlayerDevice, ids["device"])
        now = time or datetime.now(UTC)
        if kind == "runtime":
            return channel, device, "get_runtime_config", (
                mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", device.id),
                                         (("last_seen_at", now),)),), None
        refs = self.ids["B" if foreign else "A"]
        model = PlayerHeartbeat if kind == "heartbeat" else PlayerPlaybackEvent
        values = dict(player_device_id=device.id, channel_id=channel.id,
                      client_id=channel.client_id, branch_id=channel.branch_id)
        prefix = "current_" if kind == "heartbeat" else ""
        values.update({prefix + key + "_id": refs[key] for key in ("content", "campaign", "layout")})
        values.update({"received_at" if kind == "heartbeat" else "started_at": now})
        ref = RowRef(model.__name__, str(uuid4()))
        intents = [mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", device.id),
                    (("last_seen_at", now),) if kind == "heartbeat" else ())]
        if kind == "heartbeat":
            intents.insert(0, mutations.MutationIntent("UPDATE", RowRef("Channel", channel.id),
                            (("last_ping_at", now), ("status", "online"))))
        intents.append(mutations.MutationIntent("CREATE", ref, tuple(values.items())))
        return channel, device, ("create_player_heartbeat" if kind == "heartbeat"
                                 else "create_player_playback_event"), tuple(intents), (model, ref, values)

    @contextmanager
    def player_scope(self, db, kind="heartbeat", **kwargs):
        channel, device, operation, intents, history = self.player_intents(db, kind, **kwargs)
        with mutations.player_write_guard(db, channel=channel, player_device=device,
                                          operation=operation, intents=intents) as plan:
            yield plan, history

    def apply_player(self, db, plan, history):
        for item in plan.proposed:
            if item.reference.model in {"Channel", "PlayerDevice"}:
                model = Channel if item.reference.model == "Channel" else PlayerDevice
                row = db.get(model, item.reference.identity)
                for key in item.supplied_fields:
                    setattr(row, key, item.values[key])
        if history is not None:
            model, ref, values = history
            db.add(model(id=ref.identity, **values))
        db.commit()

    def assert_history_scopes(self):
        with Session(self.engine) as db:
            for model in (PlayerHeartbeat, PlayerPlaybackEvent):
                for row in db.scalars(select(model)):
                    device, channel = db.get(PlayerDevice, row.player_device_id), db.get(Channel, row.channel_id)
                    self.assertIsNotNone(device)
                    self.assertIsNotNone(channel)
                    self.assertEqual((row.client_id, row.branch_id, row.channel_id),
                                     (device.client_id, device.branch_id, device.channel_id))
                    self.assertEqual((row.client_id, row.branch_id), (channel.client_id, channel.branch_id))
                    prefix = "current_" if model is PlayerHeartbeat else ""
                    for key, parent in (("content", ContentItem), ("campaign", Campaign), ("layout", Layout)):
                        value = getattr(row, prefix + key + "_id")
                        if value:
                            self.assertEqual(db.get(parent, value).client_id, row.client_id)

    def test_foreign_optional_http_references_are_sanitized_not_persisted(self):
        for kind, model in (("heartbeat", PlayerHeartbeat), ("event", PlayerPlaybackEvent)):
            with self.subTest(kind=kind):
                response = self.request(kind, foreign=True)
                self.assertEqual(response.status_code, 200 if kind == "heartbeat" else 201, response.text)
                with Session(self.engine) as db:
                    row = db.scalar(select(model))
                    prefix = "current_" if model is PlayerHeartbeat else ""
                    self.assertEqual([getattr(row, prefix + key + "_id")
                                      for key in ("content", "campaign", "layout")], [None, None, None])
        self.assert_history_scopes()

    def test_backend_authored_foreign_history_scope_is_rejected(self):
        for kind in ("heartbeat", "event"):
            with self.subTest(kind=kind):
                try:
                    with self.assertRaises(HTTPException) as caught:
                        with self.player_scope(self.db, kind, foreign=True):
                            self.fail("Foreign media relationships reached telemetry insertion")
                    self.assertEqual(caught.exception.status_code, 409)
                finally:
                    self.db.rollback()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(PlayerHeartbeat)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(PlayerPlaybackEvent)), 0)

    def test_all_new_required_history_scope_fields_are_authoritative(self):
        for kind in ("heartbeat", "event"):
            for field, target in (("client_id", "client"), ("branch_id", "branch"),
                                  ("channel_id", "channel"), ("player_device_id", "device")):
                with self.subTest(kind=kind, field=field):
                    channel, device, operation, intents, _ = self.player_intents(self.db, kind)
                    create = intents[-1]
                    values = dict(create.patch, **{field: self.ids["B"][target]})
                    altered = intents[:-1] + (mutations.MutationIntent("CREATE", create.reference, tuple(values.items())),)
                    try:
                        with self.assertRaises(HTTPException) as caught:
                            with mutations.player_write_guard(self.db, channel=channel, player_device=device,
                                                              operation=operation, intents=altered):
                                self.fail("A forged telemetry scope reached the append")
                        self.assertEqual(caught.exception.status_code, 409)
                    finally:
                        self.db.rollback()

    def test_new_history_uuid_collision_remains_rejected(self):
        identities = self.seed_legacy_history(1)
        for kind, model in (("heartbeat", PlayerHeartbeat), ("event", PlayerPlaybackEvent)):
            with self.subTest(kind=kind):
                channel, device, operation, intents, _ = self.player_intents(self.db, kind)
                colliding = mutations.MutationIntent("CREATE", RowRef(model.__name__, identities[model][0]),
                                                     intents[-1].patch)
                try:
                    with self.assertRaises(HTTPException) as caught:
                        with mutations.player_write_guard(self.db, channel=channel, player_device=device,
                                                          operation=operation, intents=intents[:-1] + (colliding,)):
                            self.fail("A telemetry CREATE reused an existing server identity")
                    self.assertEqual(caught.exception.status_code, 409)
                finally:
                    self.db.rollback()
                self.assertEqual(self.db.scalar(select(func.count()).select_from(model)), 1)
                self.db.rollback()

    def test_hot_preflights_do_not_scan_or_materialize_existing_history(self):
        self.seed_legacy_history()
        history_tables = {"player_heartbeats", "player_playback_events"}
        real_preflight, real_snapshot = mutations.preflight_relationship_graph, mutations._WriteGraph.snapshot
        active = {"ids": frozenset(), "inside": False}
        scans, persisted, probes, phases, certificates = [], [], [], [], []

        def capture_sql(_connection, _cursor, _sql, _params, execution, _many):
            statement = execution.compiled.statement if execution.compiled is not None else None
            if not active["inside"] or statement is None or not getattr(statement, "is_select", False):
                return
            tables = {getattr(node, "name", None) for node in visitors.iterate(statement)} & history_tables
            if not tables:
                return
            where = statement._where_criteria
            predicate = where[0] if len(where) == 1 else None
            # Preserve the mandatory existence probe for the exact newly
            # server-created UUID; it can never authorize an old history row.
            exact_new_pk = (len(statement.get_final_froms()) == 1 and isinstance(predicate, BinaryExpression)
                and predicate.operator is operators.eq and getattr(predicate.left, "name", None) == "id"
                and getattr(getattr(predicate.left, "table", None), "name", None) in history_tables
                and isinstance(predicate.right, BindParameter) and predicate.right.value in active["ids"])
            (probes if exact_new_pk else scans).append(tuple(sorted(tables)))

        def capture_snapshot(graph, ref):
            value = real_snapshot(graph, ref)
            reference = graph.reference(ref)
            if active["inside"] and value is not None and reference.model in {"PlayerHeartbeat", "PlayerPlaybackEvent"}:
                persisted.append(reference)
            return value

        def capture_preflight(gate, intents, operation, **kwargs):
            active["ids"] = frozenset(i.reference.identity for i in intents if i.action == "CREATE")
            active["inside"] = True
            try:
                plan = real_preflight(gate, intents, operation, **kwargs)
                phases.append(gate.graph.media_validation_locked)
                certificates.append(plan.telemetry)
                if operation.function != "get_runtime_config":
                    self.assertTrue({RowRef(model, self.ids["A"][key]) for model, key in
                                     (("ContentItem", "content"), ("Campaign", "campaign"), ("Layout", "layout"))}
                                    <= set(plan.checked))
                return plan
            finally:
                active["inside"] = False

        event.listen(self.engine, "before_cursor_execute", capture_sql)
        try:
            with patch.object(mutations, "preflight_relationship_graph", capture_preflight), \
                    patch.object(mutations._WriteGraph, "snapshot", capture_snapshot):
                for kind in ("heartbeat", "event", "runtime"):
                    try:
                        with self.player_scope(self.db, kind):
                            pass
                    finally:
                        self.db.rollback()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_sql)
        self.assertEqual(phases, [False, True] * 3)
        self.assertEqual(scans, [], "The append-only hot path scanned historical telemetry")
        self.assertEqual(persisted, [], "The append-only hot path materialized historical telemetry")
        self.assertEqual(len(probes), 4)
        self.assertEqual(certificates, [()] * 6)

    def test_old_bad_history_does_not_authorize_new_rows_and_still_blocks_structural_writes(self):
        identities = self.seed_legacy_history(1, foreign_owner=True)
        for kind, model in (("heartbeat", PlayerHeartbeat), ("event", PlayerPlaybackEvent)):
            response = self.request(kind)
            self.assertEqual(response.status_code, 200 if kind == "heartbeat" else 201, response.text)
            with Session(self.engine) as db:
                old = db.get(model, identities[model][0])
                self.assertEqual(old.client_id, self.ids["B"]["client"], "Telemetry silently repaired old data")
                new = db.scalar(select(model).where(model.id != old.id))
                self.assertEqual((new.client_id, new.branch_id, new.channel_id, new.player_device_id),
                                 tuple(self.ids["A"][key] for key in ("client", "branch", "channel", "device")))
        self.assertEqual(self.request("runtime").status_code, 200)
        try:
            with self.assertRaises(HTTPException) as caught:
                with self.channel_guard(self.db):
                    self.fail("Structural mutation ignored old foreign incoming telemetry")
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            self.db.rollback()

    def test_non_activity_player_patch_cannot_reuse_append_only_history_exemption(self):
        self.seed_legacy_history(1, foreign_owner=True)
        a = self.ids["A"]
        channel, device = self.db.get(Channel, a["channel"]), self.db.get(PlayerDevice, a["device"])
        intent = mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", device.id),
                                          (("device_name", "A structural edit"),))
        try:
            with self.assertRaises(HTTPException) as caught:
                with mutations.player_write_guard(self.db, channel=channel, player_device=device,
                        operation="get_runtime_config", intents=(intent,)):
                    self.fail("A structural patch used the append-only shortcut")
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            self.db.rollback()

    def test_old_activity_request_never_moves_persisted_receipt_time_backwards(self):
        future = datetime.now(UTC) + timedelta(minutes=1)
        a = self.ids["A"]
        self.db.execute(update(Channel).where(Channel.id == a["channel"]).values(last_ping_at=future))
        self.db.execute(update(PlayerDevice).where(PlayerDevice.id == a["device"]).values(last_seen_at=future))
        self.db.commit()
        for kind in ("heartbeat", "runtime"):
            with self.subTest(kind=kind):
                with self.player_scope(self.db, kind, time=future - timedelta(minutes=2)) as (plan, history):
                    self.apply_player(self.db, plan, history)
                self.db.expire_all()
                self.assertEqual(utc(self.db.get(PlayerDevice, a["device"]).last_seen_at), future)
                self.assertEqual(utc(self.db.get(Channel, a["channel"]).last_ping_at), future)
        self.assert_history_scopes()

    def test_shared_media_mode_upgrade_and_cleanup_are_rejected(self):
        self.db.connection()
        with media.media_reference_operation(self.db):
            media.lock_media_paths(self.db, [self.rows["A"]["path"]], shared=True)
            with self.assertRaises(MediaPathError) as caught:
                media.lock_media_paths(self.db, [self.rows["A"]["path"]])
            self.assertEqual(str(caught.exception), "MEDIA_LOCK_MODE_CHANGED")
            with self.assertRaises(MediaPathError):
                media.require_media_commit(self.db)

    def test_media_phase_mode_drift_without_another_lookup_fails_closed(self):
        self.db.connection()
        with media.media_reference_operation(self.db):
            media.lock_media_paths(self.db, [self.rows["A"]["path"]], shared=True)
            state = self.db.info[media._STATE]
            try:
                with self.assertRaises(HTTPException) as caught:
                    with media.media_ownership_validation_phase(self.db):
                        self.assertTrue(self.owned())
                        state["shared"] = False
                self.assertEqual(caught.exception.status_code, 409)
            finally:
                state["shared"] = True

    def test_hot_lock_timeout_is_operational_error_and_never_a_success_or_relation_conflict(self):
        class LockTimeout(Exception):
            sqlstate = "55P03"

        error = OperationalError("synthetic controlled lock timeout", {}, LockTimeout())
        with patch.object(mutations._PlayerTelemetryGate, "__enter__", side_effect=error):
            response = self.request()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "Player operation temporarily unavailable"})
        self.assertEqual(self.db.scalar(select(func.count()).select_from(PlayerHeartbeat)), 0)
        self.assertEqual(self.db.get(PlayerDevice, self.ids["A"]["device"]).player_token_hash,
                         sha256(self.rows["A"]["token"].encode()).hexdigest())
        self.db.rollback()
        self.assertEqual(self.request().status_code, 200)


class PlayerTelemetrySQLiteTests(_PlayerCases, GateFixture):
    """Original serial SQLite semantics remain the non-concurrency oracle."""


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"),
                     "isolated SECURITY_R5 PostgreSQL fixture not configured")
class PlayerTelemetryPostgresTests(_PlayerCases, GateFixture):
    def make_engine(self):
        url = make_url(os.environ["SECURITY_R5_TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or url.database != "security_r5_qa" or url.query.get("options")):
            raise RuntimeError("R5.1 requires the isolated localhost security_r5_qa database")
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        expected = os.environ.get("SECURITY_R5_PG_SYSTEM_IDENTIFIER")
        if not marker and not expected:
            raise RuntimeError("R5.1 requires the owned PostgreSQL marker or system identifier")
        schema = "r51_" + uuid4().hex
        # Match the application's default pool; sixteen concurrent HTTP requests
        # must not fabricate forty data sockets in the 30-connection QA fixture.
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True)
        with engine.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")), "security_r5_qa")
            if marker:
                self.assertEqual(connection.scalar(text("SELECT current_setting('vwflex.qa_worker', true)")), marker)
            if expected:
                self.assertEqual(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")), expected)
            connection.execute(text("CREATE SCHEMA " + schema))

        def cleanup():
            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            engine.dispose()
        self.addCleanup(cleanup)
        return engine

    def players(self, count):
        result = [dict(self.ids["A"], token=self.rows["A"]["token"])]
        for index in range(1, count):
            token = "tpw_r51_synthetic_" + uuid4().hex
            channel = Channel(client_id=self.ids["A"]["client"], branch_id=self.ids["A"]["branch"],
                              name="Independent " + str(index))
            self.db.add(channel); self.db.flush()
            device = PlayerDevice(channel_id=channel.id, client_id=channel.client_id,
                branch_id=channel.branch_id, hardware_id="r51-" + str(index), device_name="Synthetic",
                player_token_hash=sha256(token.encode()).hexdigest(), is_active=True)
            self.db.add(device); self.db.flush()
            result.append(dict(self.ids["A"], channel=channel.id, device=device.id, token=token))
        self.db.commit()
        return result

    def concurrent_requests(self, requests):
        self.db.rollback()  # No controller read transaction may retain a data socket.
        ready = threading.Barrier(len(requests))

        def call(item):
            ready.wait(timeout=10)
            kind, identity = item
            response = self.request(kind, identity)
            return kind, response.status_code, response.text

        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            results = list(pool.map(call, requests, timeout=60))
        self.assertEqual([(kind, status) for kind, status, _ in results],
                         [(kind, 201 if kind == "event" else 200) for kind, _ in requests], str(results))
        self.assert_history_scopes()
        return results

    def count(self, model):
        with Session(self.engine) as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_eight_independent_heartbeats_same_tenant_all_succeed(self):
        self.concurrent_requests([("heartbeat", identity) for identity in self.players(8)])
        self.assertEqual(self.count(PlayerHeartbeat), 8)

    def test_sixteen_independent_heartbeats_same_tenant_all_succeed(self):
        self.concurrent_requests([("heartbeat", identity) for identity in self.players(16)])
        self.assertEqual(self.count(PlayerHeartbeat), 16)

    def test_eight_independent_events_same_tenant_all_succeed(self):
        self.concurrent_requests([("event", identity) for identity in self.players(8)])
        self.assertEqual(self.count(PlayerPlaybackEvent), 8)

    def test_same_player_concurrent_heartbeats_keep_all_appends_and_monotonic_activity(self):
        token_hash = self.rows["A"]["device"].player_token_hash
        identity = self.players(1)[0]
        self.concurrent_requests([("heartbeat", identity)] * 4)
        self.assertEqual(self.count(PlayerHeartbeat), 4)
        with Session(self.engine) as db:
            times = [utc(value) for value in db.scalars(select(PlayerHeartbeat.received_at))]
            self.assertGreaterEqual(utc(db.get(Channel, identity["channel"]).last_ping_at), max(times))
            device = db.get(PlayerDevice, identity["device"])
            self.assertGreaterEqual(utc(device.last_seen_at), max(times))
            self.assertEqual(device.player_token_hash, token_hash)

    def test_same_player_concurrent_events_are_append_only(self):
        identity = self.players(1)[0]
        self.concurrent_requests([("event", identity)] * 6)
        self.assertEqual(self.count(PlayerPlaybackEvent), 6)
        with Session(self.engine) as db:
            self.assertEqual(len(set(db.scalars(select(PlayerPlaybackEvent.id)))), 6)
            self.assertIsNone(db.get(PlayerDevice, identity["device"]).last_seen_at)

    def test_same_player_mixed_runtime_heartbeat_event_remains_usable(self):
        identity = self.players(1)[0]
        self.concurrent_requests([(kind, identity) for kind in ("heartbeat", "event", "runtime", "heartbeat", "event")])
        self.assertEqual(self.count(PlayerHeartbeat), 2)
        self.assertEqual(self.count(PlayerPlaybackEvent), 2)
        self.assertEqual(self.request("runtime").status_code, 200)

    def test_compatible_telemetry_guards_actually_overlap(self):
        identities = self.players(2)
        second_entered, finish = threading.Event(), threading.Event()

        def other():
            with Session(self.engine, autoflush=False) as db:
                channel = db.get(Channel, identities[1]["channel"])
                device = db.get(PlayerDevice, identities[1]["device"])
                intent = mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", device.id),
                                                  (("last_seen_at", datetime.now(UTC)),))
                with mutations.player_write_guard(db, channel=channel, player_device=device,
                        operation="get_runtime_config", intents=(intent,)):
                    second_entered.set()
                    self.assertTrue(finish.wait(5))
                db.rollback()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.player_scope(self.db, "runtime"):
                future = pool.submit(other)
                try:
                    self.assertTrue(second_entered.wait(5), "Independent telemetry guards serialized at the tenant barrier")
                finally:
                    finish.set()
            self.db.rollback()
            future.result(timeout=8)

    def test_two_media_readers_overlap_and_exclusive_writer_waits_for_both(self):
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        writer_started, writer_entered = threading.Event(), threading.Event()
        path = self.rows["A"]["path"]

        def reader(index):
            with Session(self.engine) as db:
                db.connection()
                with media.media_reference_operation(db):
                    media.lock_media_paths(db, [path], shared=True)
                    entered[index].set()
                    self.assertTrue(release[index].wait(8))

        def writer():
            with Session(self.engine) as db:
                db.connection()
                with media.media_reference_operation(db):
                    writer_started.set()
                    media.lock_media_paths(db, [path])
                    writer_entered.set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(reader, index) for index in range(2)]
            try:
                self.assertTrue(all(item.wait(5) for item in entered))
                future = pool.submit(writer)
                self.assertTrue(writer_started.wait(2))
                self.assertFalse(writer_entered.wait(0.1))
                release[0].set(); readers[0].result(timeout=5)
                self.assertFalse(writer_entered.wait(0.1), "One remaining media reader did not protect the asset")
            finally:
                for event in release:
                    event.set()
            for reader_future in readers:
                reader_future.result(timeout=5)
            future.result(timeout=5)
            self.assertTrue(writer_entered.is_set())

    def test_lost_media_coordination_transaction_is_rejected(self):
        self.db.connection()
        with media.media_reference_operation(self.db):
            media.lock_media_paths(self.db, [self.rows["A"]["path"]], shared=True)
            with self.assertRaises(HTTPException) as caught:
                with media.media_ownership_validation_phase(self.db):
                    self.assertTrue(self.owned())
                    self.db.info[media._STATE]["connection"].rollback()
            self.assertEqual(caught.exception.status_code, 409)

    def assert_external_update_waits(self, model, identity, values):
        def contender():
            with Session(self.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    db.execute(update(model).where(model.id == identity).values(**values))
                    db.commit()
                except OperationalError as error:
                    self.assertEqual(getattr(error.orig, "sqlstate", None), "55P03")
                    db.rollback()
                    return
                self.fail("An authoritative telemetry parent changed under its proof")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contender).result(timeout=5)

    def test_event_parent_nonkey_scope_and_status_changes_are_row_locked(self):
        ids = self.ids["A"]
        targets = ((ContentItem, "content", {"client_id": self.ids["B"]["client"]}),
                   (Campaign, "campaign", {"client_id": self.ids["B"]["client"]}),
                   (Layout, "layout", {"client_id": self.ids["B"]["client"]}),
                   (Branch, "branch", {"is_active": False}),
                   (Client, "client", {"status": "inactive"}))
        try:
            with self.player_scope(self.db, "event"):
                for model, key, values in targets:
                    with self.subTest(parent=model.__name__):
                        self.assert_external_update_waits(model, ids[key], values)
        finally:
            self.db.rollback()

    def structural(self, db, model, identity, values, operation):
        actor = db.get(User, self.ids["A"]["user"])
        intent = mutations.MutationIntent("UPDATE", RowRef(model.__name__, identity), tuple(values.items()))
        with mutations.write_guard(db, actor=actor, client_id=self.ids["A"]["client"],
                                   operation=operation, intents=(intent,)):
            row = db.get(model, identity)
            for field, value in values.items():
                setattr(row, field, value)
            db.commit()

    def queued_structural(self, action, *, expected="ALLOW", kind="heartbeat"):
        started = threading.Event()

        def contender():
            with Session(self.engine, autoflush=False) as db:
                started.set()
                try:
                    action(db)
                    return "ALLOW"
                except HTTPException as error:
                    db.rollback()
                    self.assertEqual(error.status_code, 409)
                    return "DENY"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.player_scope(self.db, kind) as (plan, history):
                future = pool.submit(contender)
                self.assertTrue(started.wait(2))
                self.assertFalse(future.done())
                self.apply_player(self.db, plan, history)
            self.assertEqual(future.result(timeout=10), expected)
        self.assert_history_scopes()

    def test_heartbeat_serializes_with_real_channel_update_guard(self):
        self.queued_structural(lambda db: self.structural(db, Channel, self.ids["A"]["channel"],
                                                       {"name": "Renamed safely"}, "update_channel"))
        self.assertEqual(self.request().status_code, 200)

    def test_fresh_online_heartbeat_preserves_existing_channel_delete_policy(self):
        details = []

        def remove_online(db):
            try:
                channels.delete_channel(self.ids["A"]["channel"], db, db.get(User, self.ids["A"]["user"]))
            except HTTPException as error:
                details.append(error.detail)
                raise
        self.queued_structural(remove_online, expected="DENY")
        self.assertEqual(details, ["Esta pantalla está en línea y no puede eliminarse. Solo puedes eliminar pantallas offline."])
        self.assertEqual(self.count(PlayerHeartbeat), 1)
        self.assertEqual(self.request().status_code, 200)

    def test_internal_channel_delete_guard_and_cleanup_serialize_without_orphans(self):
        def guarded_cleanup(db):
            channel = db.get(Channel, self.ids["A"]["channel"])
            actor = db.get(User, self.ids["A"]["user"])
            with mutations.delete_guard(db, (channel,), actor=actor, operation="delete_channel"):
                channels.cleanup_channel_delete_dependencies(db, channel)
                db.delete(channel)
                db.commit()
        self.queued_structural(guarded_cleanup)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.request().status_code, 401)

    def test_device_anchor_blocks_new_foreign_incoming_telemetry_fk(self):
        def insert_foreign(model):
            with Session(self.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                values = dict(player_device_id=self.ids["A"]["device"], client_id=self.ids["B"]["client"],
                              branch_id=self.ids["B"]["branch"], channel_id=self.ids["B"]["channel"])
                if model is PlayerPlaybackEvent:
                    values["started_at"] = datetime.now(UTC)
                db.add(model(**values))
                with self.assertRaises(OperationalError) as caught:
                    db.flush()
                self.assertEqual(getattr(caught.exception.orig, "sqlstate", None), "55P03")
                db.rollback()
        try:
            with self.player_scope(self.db, "event"):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    for model in (PlayerHeartbeat, PlayerPlaybackEvent):
                        with self.subTest(model=model.__name__):
                            pool.submit(insert_foreign, model).result(timeout=5)
        finally:
            self.db.rollback()
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_heartbeat_serializes_with_real_branch_delete_and_cleanup(self):
        self.queued_structural(lambda db: branches.delete_branch(self.ids["A"]["branch"], db,
                                                               db.get(User, self.ids["A"]["user"])))
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.request().status_code, 401)

    def test_heartbeat_serializes_device_deactivation(self):
        self.queued_structural(lambda db: self.structural(db, PlayerDevice, self.ids["A"]["device"],
                                                       {"is_active": False}, "activate_player"))
        self.assertEqual(self.request().status_code, 401)
        self.assertEqual(self.count(PlayerHeartbeat), 1)

    def test_heartbeat_serializes_guarded_device_delete_without_orphan_history(self):
        def remove_device(db):
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            actor = db.get(User, self.ids["A"]["user"])
            with mutations.delete_guard(db, (device,), actor=actor, operation="delete_channel"):
                db.delete(device)
                db.commit()
        self.queued_structural(remove_device)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.request().status_code, 401)

    def test_heartbeat_blocks_device_relationship_move_after_history_append(self):
        branch = Branch(client_id=self.ids["A"]["client"], name="Move target", code="MOVE")
        self.db.add(branch); self.db.commit()
        branch_id = branch.id

        def reparent(db):
            actor = db.get(User, self.ids["A"]["user"])
            intents = tuple(mutations.MutationIntent("UPDATE", RowRef(model, self.ids["A"][key]),
                            (("branch_id", branch_id),)) for model, key in (("Channel", "channel"), ("PlayerDevice", "device")))
            with mutations.write_guard(db, actor=actor, client_id=self.ids["A"]["client"],
                                       operation="activate_player", intents=intents):
                self.fail("Historical telemetry branch scope was silently rewritten")
        self.queued_structural(reparent, expected="DENY")
        self.assertEqual(self.request().status_code, 200)

    def test_event_serializes_with_actual_campaign_mutation_guard(self):
        self.queued_structural(lambda db: self.structural(db, Campaign, self.ids["A"]["campaign"],
                                       {"playback_mode": "ordered"}, "update_campaign_playback_mode"), kind="event")
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)

    def test_cross_tenant_structural_attempt_fails_while_legitimate_players_continue(self):
        self.queued_structural(lambda db: self.structural(db, Channel, self.ids["A"]["channel"],
                                       {"client_id": self.ids["B"]["client"]}, "update_channel"), expected="DENY")
        self.concurrent_requests([("heartbeat", identity) for identity in self.players(3)])
        with Session(self.engine) as db:
            self.assertEqual(db.get(Channel, self.ids["A"]["channel"]).client_id, self.ids["A"]["client"])

    def after_structural(self, action, expected_status, *, kind="heartbeat"):
        waiting = threading.Event()
        real_enter = mutations._PlayerTelemetryGate.__enter__

        def before_shared(gate):
            waiting.set()
            return real_enter(gate)

        with Session(self.engine, autoflush=False) as db, ThreadPoolExecutor(max_workers=1) as pool:
            graph = mutations._WriteGraph(db)
            context = context_for_actor(db, db.get(User, self.ids["A"]["user"]))
            with mutations.MutationGate(graph, context):
                with patch.object(mutations._PlayerTelemetryGate, "__enter__", before_shared):
                    future = pool.submit(self.request, kind)
                    self.assertTrue(waiting.wait(5), "Request did not reach the structural barrier")
                    self.assertFalse(future.done())
                    action(db)
                    db.commit()
                    response = future.result(timeout=10)
            self.assertEqual(response.status_code, expected_status, response.text)
        self.assert_history_scopes()
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_fresh_device_deactivation_before_lock_rejects_preloaded_identity(self):
        self.after_structural(lambda db: self.structural(db, PlayerDevice, self.ids["A"]["device"],
                                      {"is_active": False}, "activate_player"), 409)

    def test_fresh_channel_code_change_before_lock_rejects_preloaded_identity(self):
        self.after_structural(lambda db: self.structural(db, Channel, self.ids["A"]["channel"],
                                      {"channel_code": "TC-R51-NEW-CODE"}, "update_channel"), 409)

    def test_fresh_device_token_change_before_lock_rejects_preloaded_identity(self):
        self.after_structural(lambda db: self.structural(db, PlayerDevice, self.ids["A"]["device"],
                                      {"player_token_hash": sha256(b"r51-other-synthetic-token").hexdigest()},
                                      "activate_player"), 409)

    def test_fresh_coherent_branch_move_before_lock_rejects_old_channel_identity(self):
        branch = Branch(client_id=self.ids["A"]["client"], name="Fresh move target", code="FRESH")
        self.db.add(branch); self.db.commit()

        def reparent(db):
            actor = db.get(User, self.ids["A"]["user"])
            intents = tuple(mutations.MutationIntent("UPDATE", RowRef(model, self.ids["A"][key]),
                            (("branch_id", branch.id),)) for model, key in (("Channel", "channel"), ("PlayerDevice", "device")))
            with mutations.write_guard(db, actor=actor, client_id=self.ids["A"]["client"],
                                       operation="activate_player", intents=intents):
                db.get(Channel, self.ids["A"]["channel"]).branch_id = branch.id
                db.get(PlayerDevice, self.ids["A"]["device"]).branch_id = branch.id
                db.commit()
        self.after_structural(reparent, 409)

    def test_fresh_client_suspension_before_lock_rejects_event(self):
        self.after_structural(lambda db: self.structural(db, Client, self.ids["A"]["client"],
                                      {"status": "inactive"}, "update_client_status"), 403, kind="event")

    def test_fresh_channel_delete_before_lock_cannot_create_orphan_heartbeat(self):
        self.after_structural(lambda db: channels.delete_channel(self.ids["A"]["channel"], db,
                                     db.get(User, self.ids["A"]["user"])), 409)

    def test_fresh_branch_delete_before_lock_cannot_create_orphan_event(self):
        self.after_structural(lambda db: branches.delete_branch(self.ids["A"]["branch"], db,
                                     db.get(User, self.ids["A"]["user"])), 409, kind="event")
