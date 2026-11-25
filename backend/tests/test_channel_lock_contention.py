"""Canonical workers never wait for another lock while retaining Channel.

Only isolated fixtures and actual existing processors are used. The external
PostgreSQL before/after runner records the complete incident-shaped wait graph.
"""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import io
import json
import logging
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from sqlalchemy import event, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import player
from app.core import request_pg_diagnostics as diagnostic
from app.models.entities import Channel, PlayerDevice, PlayerHeartbeat, PlayerHeartbeatInbox, PlayerPlaybackEvent, PlayerPlaybackInbox
from app.schemas.domain import PlayerHeartbeatCreate, PlayerPlaybackEventCreate
from app.services import heartbeat_inbox, media_references as media, playback_inbox
from app.services.media_storage import MediaPathError
from app.services.tenant_mutations import MutationIntent, PlayerPlaybackReceiptBusy, player_write_guard
from app.services.tenant_ownership import RowRef
import test_heartbeat_inbox as heartbeat_cases
from test_tenant_gate_performance import GateFixture


def immutable(row):
    return tuple(getattr(row, key) for key in (
        "id", "player_device_id", "channel_id", "client_id", "branch_id",
        "payload_json", "payload_bytes", "accepted_at"))


class _Fixture(GateFixture):
    legacy_playback_settlement = False

    def setUp(self):
        super().setUp()
        # Explicit processing lets assertions inspect the durable ACK boundary.
        self.enterContext(patch.object(heartbeat_inbox, "ensure_worker", return_value=None))
        self.enterContext(patch.object(playback_inbox, "ensure_worker", return_value=None))

    def accept(self, kind, **changes):
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            values = dict(current_content_id=self.ids["A"]["content"],
                          current_campaign_id=self.ids["A"]["campaign"],
                          current_layout_id=self.ids["A"]["layout"], status="online")
            if kind == "heartbeat":
                values.update(changes)
                ack = heartbeat_inbox.enqueue(db, device, channel, PlayerHeartbeatCreate(**values))
                identity = db.scalar(select(PlayerHeartbeatInbox.id).order_by(PlayerHeartbeatInbox.accepted_at.desc()))
            else:
                values = dict(content_id=self.ids["A"]["content"], campaign_id=self.ids["A"]["campaign"],
                              layout_id=self.ids["A"]["layout"], started_at=datetime.now(UTC), status="completed")
                values.update(changes)
                ack = playback_inbox.enqueue(db, device, channel, PlayerPlaybackEventCreate(**values))
                identity = ack.event_id
        return ack, identity

    def receipt(self, kind, identity):
        model = PlayerHeartbeatInbox if kind == "heartbeat" else PlayerPlaybackInbox
        with Session(self.engine) as db:
            row = db.get(model, identity)
            if row is not None:
                db.expunge(row)
            return row

    def history(self, kind, identity):
        with Session(self.engine) as db:
            return db.get(PlayerHeartbeat if kind == "heartbeat" else PlayerPlaybackEvent, identity)

    def process(self, kind):
        if kind == "heartbeat":
            return heartbeat_inbox.materialize_one(self.engine, player._materialize_player_heartbeat)
        return playback_inbox.materialize_one(self.engine, player._record_player_playback_event)

    def ready(self, kind, identity):
        model = PlayerHeartbeatInbox if kind == "heartbeat" else PlayerPlaybackInbox
        with Session(self.engine) as db:
            db.execute(update(model).where(model.id == identity).values(
                available_at=datetime.now(UTC) - timedelta(seconds=1)))
            db.commit()

    @contextmanager
    def sqlite_media_holder(self):
        acquired, release = threading.Event(), threading.Event()

        def hold():
            with media._SQLITE_LOCK:
                acquired.set()
                release.wait(4)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(acquired.wait(2))
        try:
            yield
        finally:
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())


class ChannelLockContentionTests(_Fixture):
    def test_heartbeat_materializer_explicitly_uses_receipt_worker_policy(self):
        _, identity = self.accept("heartbeat")
        original, seen = player.player_write_guard, []

        @contextmanager
        def observed(*args, **kwargs):
            seen.append(kwargs.get("receipt_processing", False))
            with original(*args, **kwargs) as plan:
                yield plan

        with patch.object(player, "player_write_guard", side_effect=observed):
            self.assertTrue(self.process("heartbeat"))
        self.assertEqual(seen, [True])
        self.assertIsNone(self.receipt("heartbeat", identity))
        self.assertIsNotNone(self.history("heartbeat", identity))

    def test_nonblocking_media_mode_is_explicitly_shared_and_boolean(self):
        for shared, nonblocking in ((False, True), (True, 1), (True, None)):
            with self.subTest(shared=shared, nonblocking=nonblocking), Session(self.engine) as db:
                with self.assertRaises(MediaPathError), media.media_reference_operation(db):
                    media.lock_media_paths(db, [self.rows["A"]["path"]],
                                           shared=shared, nonblocking=nonblocking)
                self.assertNotIn(media._STATE, db.info)

    def test_default_writer_coordination_and_no_shared_to_exclusive_upgrade_are_preserved(self):
        with Session(self.engine) as db, media.media_reference_operation(db):
            media.lock_media_paths(db, [self.rows["A"]["path"]])
            self.assertFalse(db.info[media._STATE]["shared"])
        with Session(self.engine) as db, media.media_reference_operation(db):
            media.lock_media_paths(db, [self.rows["A"]["path"]], shared=True, nonblocking=True)
            with self.assertRaises(MediaPathError) as error:
                media.lock_media_paths(db, [self.rows["A"]["path"]])
            self.assertEqual(error.exception.code, "MEDIA_LOCK_MODE_CHANGED")

    def test_receipt_policy_cannot_be_enabled_for_runtime_or_activation(self):
        with Session(self.engine, autoflush=False) as db:
            channel = db.get(Channel, self.ids["A"]["channel"])
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            intent = MutationIntent("UPDATE", RowRef("PlayerDevice", device.id),
                                    (("last_seen_at", datetime.now(UTC)),))
            for operation in ("get_runtime_config", "activate_player"):
                with self.subTest(operation=operation), self.assertRaises(HTTPException) as error:
                    with player_write_guard(db, channel=channel, player_device=device, operation=operation,
                                            intents=(intent,), receipt_processing=True):
                        self.fail("Receipt processing is private to canonical telemetry")
                self.assertEqual(error.exception.status_code, 409)

    def test_both_workers_retain_then_materialize_receipts_after_logical_media_busy(self):
        for kind in ("heartbeat", "playback"):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = immutable(self.receipt(kind, identity))
                with self.sqlite_media_holder():
                    started = time.monotonic()
                    self.assertTrue(self.process(kind))
                    self.assertLess(time.monotonic() - started, 2)
                    row = self.receipt(kind, identity)
                    self.assertEqual((row.status, row.attempts, row.last_error_code), ("PENDING", 0, None))
                    self.assertEqual(immutable(row), original)
                    self.assertIsNone(self.history(kind, identity))
                self.ready(kind, identity)
                self.assertTrue(self.process(kind))
                self.assertIsNone(self.receipt(kind, identity))
                self.assertIsNotNone(self.history(kind, identity))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_foreign_raw_optional_reference_still_cannot_create_authority(self):
        _, identity = self.accept("playback", content_id=self.ids["B"]["content"])
        self.assertTrue(self.process("playback"))
        self.assertIsNone(self.history("playback", identity).content_id)
        self.assertIsNone(self.receipt("playback", identity))

    def test_recoverable_media_deferrals_do_not_spend_real_failure_budget(self):
        for kind, inbox in (("heartbeat", heartbeat_inbox), ("playback", playback_inbox)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = immutable(self.receipt(kind, identity))
                before = inbox.counters(self.engine)
                with self.sqlite_media_holder():
                    for _ in range(inbox.MAX_ATTEMPTS + 1):
                        self.process(kind)
                        row = self.receipt(kind, identity)
                        self.assertEqual((row.status, row.attempts, row.last_error_code),
                                         ("PENDING", 0, None))
                        self.assertEqual(immutable(row), original)
                        self.assertIsNone(self.history(kind, identity))
                        self.assertFalse(self.process(kind), "The deferred receipt is not eligible before its existing backoff")
                        self.ready(kind, identity)
                self.assertTrue(self.process(kind))
                self.assertFalse(self.process(kind))
                with Session(self.engine) as db:
                    model = PlayerHeartbeat if kind == "heartbeat" else PlayerPlaybackEvent
                    self.assertEqual(db.scalar(select(func.count()).select_from(model).where(
                        model.id == identity)), 1)
                self.assertIsNone(self.receipt(kind, identity))
                after = inbox.counters(self.engine)
                self.assertEqual(after.get("media_deferrals", 0) - before.get("media_deferrals", 0),
                                 inbox.MAX_ATTEMPTS + 1)
                for metric in ("processing_failures", "scheduled_retries", "retained_failed"):
                    self.assertEqual(after.get(metric, 0), before.get(metric, 0))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_media_busy_after_guard_yield_is_not_a_recoverable_deferral(self):
        for kind, inbox, processor in (
                ("heartbeat", heartbeat_inbox, player._materialize_player_heartbeat),
                ("playback", playback_inbox, player._record_player_playback_event)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                before = inbox.counters(self.engine)

                def fail_after_guard_yield(payload, device, db, **kwargs):
                    def late_failure(_plan):
                        raise media.MediaCoordinationBusy()
                    return processor(payload, device, db, **dict(kwargs, before_commit=late_failure))

                self.assertTrue(inbox.materialize_one(self.engine, fail_after_guard_yield))
                row = self.receipt(kind, identity)
                self.assertEqual((row.status, row.attempts), ("PENDING", 1))
                self.assertIsNotNone(row.last_error_code)
                self.assertIsNone(self.history(kind, identity))
                self.assertEqual(inbox.counters(self.engine).get("media_deferrals", 0),
                                 before.get("media_deferrals", 0))
                self.assertEqual(inbox.counters(self.engine).get("processing_failures", 0),
                                 before.get("processing_failures", 0) + 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_media_deferral_preserves_a_previous_real_failure_and_recovers_once(self):
        for kind, inbox in (("heartbeat", heartbeat_inbox), ("playback", playback_inbox)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)

                def real_failure(*_args, **_kwargs):
                    raise HTTPException(status_code=503, detail="Synthetic non-media processor failure")

                self.assertTrue(inbox.materialize_one(self.engine, real_failure))
                original = self.receipt(kind, identity)
                self.assertEqual(original.attempts, 1)
                self.assertIsNotNone(original.last_error_code)
                counters = inbox.counters(self.engine)
                with self.sqlite_media_holder():
                    for _ in range(2):
                        self.ready(kind, identity)
                        self.assertTrue(self.process(kind))
                        current = self.receipt(kind, identity)
                        self.assertEqual((current.status, current.attempts, current.last_error_code),
                                         (original.status, original.attempts, original.last_error_code))
                        self.assertEqual(immutable(current), immutable(original))
                        self.assertGreater(current.available_at.replace(tzinfo=UTC), datetime.now(UTC))
                for metric in ("processing_failures", "scheduled_retries", "retained_failed"):
                    self.assertEqual(inbox.counters(self.engine).get(metric, 0), counters.get(metric, 0))
                self.ready(kind, identity)
                self.assertTrue(self.process(kind))
                self.assertFalse(self.process(kind))
                with Session(self.engine) as db:
                    model = PlayerHeartbeat if kind == "heartbeat" else PlayerPlaybackEvent
                    self.assertEqual(db.scalar(select(func.count()).select_from(model).where(model.id == identity)), 1)
                self.assertIsNone(self.receipt(kind, identity))

    def test_deferred_receipt_revalidates_changed_scope_instead_of_materializing(self):
        for kind in ("heartbeat", "playback"):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = immutable(self.receipt(kind, identity))
                with self.sqlite_media_holder():
                    self.assertTrue(self.process(kind))
                with Session(self.engine) as db:
                    db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                        branch_id=self.ids["B"]["branch"]))
                    db.commit()
                try:
                    self.ready(kind, identity)
                    self.assertTrue(self.process(kind))
                    current = self.receipt(kind, identity)
                    self.assertEqual((current.status, current.attempts, current.last_error_code),
                                     ("FAILED", 1, "AUTHORITY_CHANGED" if kind == "heartbeat"
                                      else "AUTHORITY_OR_RECEIPT_INVALID"))
                    self.assertEqual(immutable(current), original)
                    self.assertIsNone(self.history(kind, identity))
                    self.assertFalse(self.process(kind))
                finally:
                    with Session(self.engine) as db:
                        db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                            branch_id=self.ids["A"]["branch"]))
                        db.commit()

    def test_nonmedia_transient_failures_still_exhaust_the_same_five_attempts(self):
        class SyntheticLockUnavailable(Exception):
            sqlstate = "55P03"

        failures = (
            ("http503", lambda: HTTPException(status_code=503, detail="Synthetic non-media failure")),
            ("scope_busy", PlayerPlaybackReceiptBusy),
            ("database55P03", lambda: OperationalError("synthetic lock", {}, SyntheticLockUnavailable())),
        )
        for kind, inbox in (("heartbeat", heartbeat_inbox), ("playback", playback_inbox)):
            self.assertEqual(inbox.MAX_ATTEMPTS, 5)
            self.assertEqual(inbox._RETRY_SECONDS, (1, 2, 4, 8))
            for label, factory in failures:
                with self.subTest(kind=kind, reason=label):
                    _, identity = self.accept(kind)
                    original = immutable(self.receipt(kind, identity))
                    before = inbox.counters(self.engine)

                    def fail(*_args, **_kwargs):
                        raise factory()

                    for attempt in range(1, 6):
                        self.assertTrue(inbox.materialize_one(self.engine, fail))
                        current = self.receipt(kind, identity)
                        self.assertEqual(current.attempts, attempt)
                        self.assertEqual(current.status, "FAILED" if attempt == 5 else "PENDING")
                        self.assertEqual(immutable(current), original)
                        self.assertIsNone(self.history(kind, identity))
                        if attempt < 5:
                            self.assertFalse(inbox.materialize_one(self.engine, fail))
                            self.ready(kind, identity)
                    self.assertFalse(inbox.materialize_one(self.engine, fail))
                    after = inbox.counters(self.engine)
                    self.assertEqual(after.get("processing_failures", 0) - before.get("processing_failures", 0), 5)
                    self.assertEqual(after.get("scheduled_retries", 0) - before.get("scheduled_retries", 0), 4)
                    self.assertEqual(after.get("retained_failed", 0) - before.get("retained_failed", 0), 1)
                    self.assertEqual(after.get("media_deferrals", 0), before.get("media_deferrals", 0))

    def test_deferred_receipts_keep_finite_capacity_and_original_pending_age(self):
        for kind, inbox, model in (("heartbeat", heartbeat_inbox, PlayerHeartbeatInbox),
                                  ("playback", playback_inbox, PlayerPlaybackInbox)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                with Session(self.engine) as db:
                    db.execute(update(model).where(model.id == identity).values(
                        accepted_at=datetime.now(UTC) - timedelta(days=1)))
                    db.commit()
                original = self.receipt(kind, identity)
                with self.sqlite_media_holder():
                    self.assertTrue(self.process(kind))
                current = self.receipt(kind, identity)
                self.assertEqual(immutable(current), immutable(original))
                with Session(self.engine) as db:
                    report = inbox.snapshot(db)
                self.assertEqual((report["pending_count"], report["failed_count"]), (1, 0))
                self.assertEqual(report["pending_payload_bytes"], original.payload_bytes)
                self.assertGreaterEqual(report["oldest_age_seconds"], 86400)
                self.assertGreaterEqual(report["oldest_pending_age_seconds"], 86400)
                with patch.object(inbox, "MAX_PENDING_COUNT", 1), self.assertRaises(HTTPException) as error:
                    self.accept(kind)
                self.assertEqual(error.exception.status_code, 503)
                self.assertEqual(immutable(self.receipt(kind, identity)), immutable(original))

    def test_deferral_storage_error_retains_the_original_durable_receipt(self):
        for kind, inbox in (("heartbeat", heartbeat_inbox), ("playback", playback_inbox)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = self.receipt(kind, identity)
                before = inbox.counters(self.engine)
                with self.sqlite_media_holder(), patch.object(
                        Session, "commit", side_effect=OperationalError("synthetic deferral write", {}, Exception())):
                    self.assertFalse(self.process(kind))
                current = self.receipt(kind, identity)
                self.assertEqual(immutable(current), immutable(original))
                self.assertEqual((current.status, current.attempts, current.last_error_code, current.available_at),
                                 (original.status, original.attempts, original.last_error_code, original.available_at))
                self.assertIsNone(self.history(kind, identity))
                after = inbox.counters(self.engine)
                self.assertEqual(after.get("deferral_storage_errors", 0) - before.get("deferral_storage_errors", 0), 1)
                self.assertEqual(after.get("media_deferrals", 0), before.get("media_deferrals", 0))
                self.assertEqual(after.get("processing_failures", 0), before.get("processing_failures", 0))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_invalidated_device_keeps_accepted_receipt_without_canonical_history(self):
        _, identity = self.accept("heartbeat")
        original = immutable(self.receipt("heartbeat", identity))
        with Session(self.engine) as db:
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(is_active=False))
            db.commit()
        self.assertTrue(self.process("heartbeat"))
        row = self.receipt("heartbeat", identity)
        self.assertEqual((row.status, row.last_error_code), ("FAILED", "AUTHORITY_CHANGED"))
        self.assertEqual(immutable(row), original)
        self.assertIsNone(self.history("heartbeat", identity))


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned local PostgreSQL QA required")
class ChannelLockContentionPostgresTests(_Fixture):
    make_engine = heartbeat_cases.HeartbeatInboxPostgresTests.make_engine

    def test_real_parent_nowait_conflicts_still_fail_after_five_attempts(self):
        for kind, inbox in (("heartbeat", heartbeat_inbox), ("playback", playback_inbox)):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = immutable(self.receipt(kind, identity))
                counters = inbox.counters(self.engine)
                with Session(self.engine) as held:
                    held.scalar(select(PlayerDevice.id).where(
                        PlayerDevice.id == self.ids["A"]["device"]).with_for_update())
                    for attempt in range(1, 6):
                        self.assertTrue(self.process(kind))
                        current = self.receipt(kind, identity)
                        self.assertEqual((current.status, current.attempts),
                                         ("FAILED" if attempt == 5 else "PENDING", attempt))
                        self.assertEqual(immutable(current), original)
                        self.assertIsNone(self.history(kind, identity))
                        with Session(self.engine) as available:
                            self.assertEqual(available.scalar(select(Channel.id).where(
                                Channel.id == self.ids["A"]["channel"]).with_for_update(nowait=True)),
                                self.ids["A"]["channel"])
                        if attempt < 5:
                            self.ready(kind, identity)
                after = inbox.counters(self.engine)
                self.assertEqual(after.get("processing_failures", 0) - counters.get("processing_failures", 0), 5)
                self.assertEqual(after.get("retained_failed", 0) - counters.get("retained_failed", 0), 1)
                self.assertEqual(after.get("media_deferrals", 0), counters.get("media_deferrals", 0))
                self.assertFalse(self.process(kind))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_heartbeat_worker_releases_channel_when_device_is_busy(self):
        _, identity = self.accept("heartbeat")
        original = immutable(self.receipt("heartbeat", identity))
        with Session(self.engine) as held:
            held.scalar(select(PlayerDevice.id).where(PlayerDevice.id == self.ids["A"]["device"]).with_for_update())
            started = time.monotonic()
            self.assertTrue(self.process("heartbeat"))
            self.assertLess(time.monotonic() - started, 2)
            with Session(self.engine) as available:
                self.assertEqual(available.scalar(select(Channel.id).where(
                    Channel.id == self.ids["A"]["channel"]).with_for_update(nowait=True)), self.ids["A"]["channel"])
            row = self.receipt("heartbeat", identity)
            self.assertEqual((row.status, row.last_error_code), ("PENDING", "PROCESSOR_TEMPORARILY_UNAVAILABLE"))
            self.assertEqual(immutable(row), original)
        self.ready("heartbeat", identity)
        self.assertTrue(self.process("heartbeat"))
        self.assertIsNotNone(self.history("heartbeat", identity))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_both_worker_media_probes_release_channel_and_preserve_durable_retry(self):
        diagnostic.install(FastAPI())
        for kind in ("heartbeat", "playback"):
            with self.subTest(kind=kind):
                _, identity = self.accept(kind)
                original = immutable(self.receipt(kind, identity))
                stream = io.StringIO()
                handler = logging.StreamHandler(stream)
                logger = logging.getLogger(diagnostic.__name__)
                previous = (list(logger.handlers), logger.propagate, logger.level)
                logger.handlers, logger.propagate, logger.level = [handler], False, logging.WARNING
                try:
                    with Session(self.engine) as held:
                        held.scalar(text("SELECT pg_advisory_xact_lock(:key)"),
                                    {"key": media.media_lock_key(self.rows["A"]["path"])})
                        started = time.monotonic()
                        self.assertTrue(self.process(kind))
                        self.assertLess(time.monotonic() - started, 2)
                        with Session(self.engine) as available:
                            self.assertEqual(available.scalar(select(Channel.id).where(
                                Channel.id == self.ids["A"]["channel"]).with_for_update(nowait=True)), self.ids["A"]["channel"])
                        row = self.receipt(kind, identity)
                        self.assertEqual((row.status, row.attempts, row.last_error_code), ("PENDING", 0, None))
                        self.assertEqual(immutable(row), original)
                        self.assertIsNone(self.history(kind, identity))
                    if kind == "playback":
                        errors = [json.loads(line) for line in stream.getvalue().splitlines()]
                        error = next(row for row in errors if row.get("event") == "playback_worker_media_deferred")
                        self.assertIsNone(error["sqlstate"])
                        self.assertGreater(error["pg_backend_pid"], 0)
                        self.assertEqual((error["lock_context"], error["lock_mode"], error["sql_tables"]),
                                         ("MEDIA_PATH", "ADVISORY_SHARED", []))
                        self.assertIs(error["media_try_lock_granted"], False)
                        self.assertFalse(error["lock_nowait"])
                        self.assertEqual(error["deferral_reason"], "MEDIA_COORDINATION_BUSY")
                        self.assertNotIn("error_reason", error)
                finally:
                    logger.handlers, logger.propagate, logger.level = previous
                    handler.close()
                self.ready(kind, identity)
                self.assertTrue(self.process(kind))
                self.assertIsNone(self.receipt(kind, identity))
                self.assertIsNotNone(self.history(kind, identity))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_partial_media_try_batch_releases_all_granted_locks_and_its_connection(self):
        first = self.rows["A"]["path"]
        second = first.with_name("second-synthetic.png")
        second.write_bytes(b"Synthetic partial coordination batch")
        paths = sorted((first, second), key=media.media_lock_key)
        coordinators = []

        def observe(connection, cursor, statement, parameters, execution, many):
            if connection.engine is not self.engine and statement.startswith(
                    "SELECT pg_try_advisory_xact_lock_shared"):
                driver = connection.connection.driver_connection
                coordinators.append((connection, driver))

        with Session(self.engine) as held:
            held.scalar(text("SELECT pg_advisory_xact_lock(:key)"), {"key": media.media_lock_key(paths[1])})
            event.listen(Engine, "before_cursor_execute", observe)
            try:
                with Session(self.engine) as db:
                    with self.assertRaises(media.MediaCoordinationBusy), media.media_reference_operation(db):
                        media.lock_media_paths(db, paths, shared=True, nonblocking=True)
                    self.assertNotIn(media._STATE, db.info)
                    self.assertIsNone(db.get_transaction())
            finally:
                event.remove(Engine, "before_cursor_execute", observe)
            self.assertEqual(len(coordinators), 2, "The first key was granted before the second was busy")
            self.assertTrue(all(connection.closed and driver.closed for connection, driver in coordinators))
            with Session(self.engine) as available:
                self.assertTrue(available.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                 {"key": media.media_lock_key(paths[0])}))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_retry_revalidates_device_after_a_busy_media_attempt(self):
        _, identity = self.accept("heartbeat")
        original = immutable(self.receipt("heartbeat", identity))
        with Session(self.engine) as held:
            held.scalar(text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": media.media_lock_key(self.rows["A"]["path"])})
            self.assertTrue(self.process("heartbeat"))
        with Session(self.engine) as db:
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(is_active=False))
            db.commit()
        self.ready("heartbeat", identity)
        self.assertTrue(self.process("heartbeat"))
        row = self.receipt("heartbeat", identity)
        self.assertEqual((row.status, row.last_error_code), ("FAILED", "AUTHORITY_CHANGED"))
        self.assertEqual(immutable(row), original)
        self.assertIsNone(self.history("heartbeat", identity))
        self.assertEqual(self.engine.pool.checkedout(), 0)
