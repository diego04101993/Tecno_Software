"""Heartbeat-only work reduction, with the original media and write proofs.

No application startup or production connection. SQLite retains the original
sanitizer path; PostgreSQL coverage explicitly opts into the existing owned R5
fixture and its per-test schema. No latency assertion uses a faster fake path.
"""
from datetime import UTC, datetime, timedelta
import os
import unittest
from unittest.mock import patch

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.api.routes import player
from app.models.entities import Branch, Channel, Client, ContentItem, PlayerDevice, PlayerHeartbeat
from app.schemas.domain import PlayerHeartbeatCreate
from app.services import media_references as media
from app.services.runtime_read import runtime_reads
from test_tenant_gate_performance import GateFixture
import test_player_telemetry_concurrency as telemetry_tests


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
