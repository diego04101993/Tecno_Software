"""Receipt-specific worker diagnostics; unchanged acceptance and bounded retries.

PostgreSQL cases require the existing verified, local, UUID-schema QA fixture.
No app startup, production service, migration or external Player is involved.
"""
import io
import hashlib
import json
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import player
from app.core import request_pg_diagnostics as diagnostic
from app.models.entities import (
    Branch, Campaign, Channel, Client, ContentItem, Layout, PlayerDevice,
    PlayerPlaybackEvent, PlayerPlaybackInbox,
)
from app.services import playback_inbox as inbox
import test_playback_inbox as playback_tests
from test_tenant_gate_performance import GateFixture


_IMMUTABLE = ("id", "player_device_id", "channel_id", "client_id", "branch_id",
              "payload_json", "payload_bytes", "accepted_at")


def immutable(row):
    return tuple(getattr(row, field) for field in _IMMUTABLE)


class _WorkerFixture(playback_tests._InboxFixture, GateFixture):
    def setUp(self):
        super().setUp()
        diagnostic.install(FastAPI())
        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(diagnostic.__name__)
        previous = (list(logger.handlers), logger.level, logger.propagate, logger.disabled)

        def restore():
            logger.handlers, logger.level, logger.propagate, logger.disabled = previous
            handler.close()
        self.addCleanup(restore)
        logger.handlers, logger.propagate, logger.disabled = [handler], False, False
        logger.setLevel(logging.WARNING)

    def records(self):
        return [json.loads(line) for line in self.log.getvalue().splitlines()]

    def receipt_records(self, identity):
        return [row for row in self.records() if row.get("receipt_id") == identity]


class PlaybackWorkerCorrelationTests(_WorkerFixture):
    def test_worker_relation_allowlist_covers_the_complete_current_model_catalog(self):
        from app.services.tenant_relations import MODEL_SPECS
        self.assertTrue({model.table for model in MODEL_SPECS} <= diagnostic._WORKER_TABLES)
        shape = diagnostic._shape("SELECT content_folders.id FROM content_folders FOR SHARE NOWAIT", worker_receipt=True)
        self.assertEqual((shape["sql_tables"], shape["lock_mode"], shape["lock_nowait"]),
                         (["content_folders"], "FOR_SHARE", True))
        self.assertEqual(diagnostic._shape("SELECT secret_table.id FROM secret_table FOR SHARE", worker_receipt=True)["sql_tables"], [])

    def test_error_callsite_is_allowlisted_and_bounded_without_frame_values(self):
        for module, name, expected in (
                ("app.services.tenant_mutations", "_PlayerTelemetryGate.__enter__", "TENANT_BARRIER"),
                ("app.services.media_references", "lock_media_paths", "MEDIA_PATH"),
                ("app.services.tenant_mutations", "_DeleteGraph.snapshot", "AUTHORITY_ROW"),
                ("unrelated.module", "lock_media_paths", "UNKNOWN")):
            frame = SimpleNamespace(f_globals={"__name__": module, "secret": "not-used"},
                                    f_code=SimpleNamespace(co_qualname=name), f_back=None)
            with self.subTest(expected=expected), patch.object(diagnostic.inspect, "currentframe", return_value=frame):
                self.assertEqual(diagnostic._worker_lock_context(), expected)
        unknown = SimpleNamespace(f_globals={"__name__": "unknown"},
                                  f_code=SimpleNamespace(co_qualname="unknown"), f_back=frame)
        unknown.f_back = unknown
        with patch.object(diagnostic.inspect, "currentframe", return_value=unknown):
            self.assertEqual(diagnostic._worker_lock_context(), "UNKNOWN")

    def test_real_emitter_correlates_one_receipt_without_inheriting_http_identity(self):
        ack = self.accept()
        request = diagnostic._Trace(endpoint="heartbeat", method="POST", request_id=str(uuid4()))
        token = diagnostic._current.set(request)
        try:
            self.assertTrue(self.process())
            self.assertIs(diagnostic._active(), request)
        finally:
            diagnostic._current.reset(token)
        records = self.receipt_records(ack.event_id)
        self.assertTrue(records, "Use the actual JSON emitter, not a mocked event dictionary")
        self.assertEqual({row["history_id"] for row in records}, {ack.event_id})
        self.assertEqual({row["attempt_number"] for row in records}, {1})
        self.assertEqual(len({row["worker_id"] for row in records}), 1)
        self.assertTrue(all(row["request_id"] is None and row["scope"] == "worker" for row in records))
        self.assertNotIn(request.request_id, self.log.getvalue())
        self.assertTrue(all(row["timestamp"].endswith("Z") for row in records))
        stages = {row["playback_worker_stage"] for row in records}
        self.assertTrue({"PLAYBACK_RECEIPT_SELECTED", "PLAYBACK_VALIDATE_SCOPE", "PLAYBACK_MATERIALIZE",
                         "PLAYBACK_CLAIM_RECEIPT", "PLAYBACK_COMMITTED"} <= stages)
        self.assertIsNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_existing_five_attempt_policy_retains_durable_receipt_and_four_old_failed_rows(self):
        historical = [self.accept() for _ in range(4)]
        with Session(self.engine) as db:
            db.execute(update(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id.in_(
                [ack.event_id for ack in historical])).values(status="FAILED", attempts=5,
                                                             last_error_code="PROCESSOR_ERROR"))
            db.commit()
        old = {ack.event_id: vars(self.receipt(ack.event_id)).copy() for ack in historical}
        ack = self.accept()
        before = immutable(self.receipt(ack.event_id))

        def locked(*args, **kwargs):
            raise inbox.PlayerPlaybackReceiptBusy()

        for attempt in range(1, 6):
            self.assertTrue(self.process(locked))
            row = self.receipt(ack.event_id)
            self.assertEqual((row.attempts, row.status, row.last_error_code),
                             (attempt, "FAILED" if attempt == 5 else "PENDING", "TARGET_TEMPORARILY_LOCKED"))
            self.assertEqual(immutable(row), before)
            if attempt < 5:
                self.ready_again(ack.event_id)
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        for identity, original in old.items():
            current = vars(self.receipt(identity))
            self.assertEqual({key: value for key, value in current.items() if key != "_sa_instance_state"},
                             {key: value for key, value in original.items() if key != "_sa_instance_state"})
        failures = [row for row in self.receipt_records(ack.event_id)
                    if row.get("error_reason") == "TARGET_TEMPORARILY_LOCKED" and row.get("exception_class")]
        self.assertEqual([row["attempt_number"] for row in failures], list(range(1, 6)))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_logging_failure_cannot_change_processing_or_consume_the_http_trace(self):
        ack = self.accept()
        with patch.object(diagnostic, "_emit", side_effect=RuntimeError("synthetic logger unavailable")):
            self.assertTrue(self.process())
        self.assertIsNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertIsNone(diagnostic._active())
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_payload_and_exception_values_are_never_logged(self):
        secret = "SYNTHETIC_NEVER_LOG_PAYLOAD_OR_SQL_PARAMETERS"
        ack = self.accept(self.payload(metadata_json={"note": secret}))

        def failed(*args, **kwargs):
            raise OperationalError(secret, {"password": secret}, Exception(secret))

        self.assertTrue(self.process(failed))
        self.assertNotIn(secret, self.log.getvalue())
        row = self.receipt(ack.event_id)
        self.assertEqual((row.status, row.last_error_code), ("PENDING", "DATABASE_TEMPORARILY_UNAVAILABLE"))
        self.assertEqual(row.payload_json["metadata_json"], {"note": secret})
        self.ready_again(ack.event_id)
        self.assertTrue(self.process())
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_empty_consumer_does_not_log_idle_poll_or_create_diagnostic_context(self):
        self.assertFalse(self.process())
        self.assertEqual(self.records(), [])
        self.assertIsNone(diagnostic._active())
        self.assertEqual(self.engine.pool.checkedout(), 0)


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Verified owned local PostgreSQL QA required")
class PlaybackWorkerCorrelationPostgresTests(_WorkerFixture):
    def make_engine(self):
        from test_player_connection_pool import PlayerConnectionPoolTests
        return PlayerConnectionPoolTests.make_engine(self)

    def parallel_engine(self):
        return playback_tests.PlaybackInboxPostgresTests.parallel_engine(self)

    def test_distinct_locked_authorities_have_exact_pid_relation_lock_and_recover_on_retry(self):
        blocker = self.parallel_engine()
        for model, key, relation, mode in (
                (Channel, "channel", "channels", "FOR_SHARE"),
                (Client, "client", "clients", "FOR_SHARE"),
                (Branch, "branch", "branches", "FOR_SHARE"),
                (PlayerDevice, "device", "player_devices", "FOR_UPDATE"),
                (Campaign, "campaign", "campaigns", "FOR_SHARE"),
                (ContentItem, "content", "contents", "FOR_SHARE"),
                (Layout, "layout", "layouts", "FOR_SHARE")):
            with self.subTest(relation=relation):
                ack = self.accept()
                before = immutable(self.receipt(ack.event_id))
                actual_pids = []

                def processor(payload, device, db, **kwargs):
                    actual_pids.append(db.scalar(text("SELECT pg_backend_pid()")))
                    return player._record_player_playback_event(payload, device, db, **kwargs)

                with Session(blocker) as held:
                    held.scalar(select(model.id).where(model.id == self.ids["A"][key]).with_for_update())
                    self.assertTrue(self.process(processor))
                    row = self.receipt(ack.event_id)
                    self.assertEqual((row.status, row.attempts, row.last_error_code),
                                     ("PENDING", 1, "TARGET_TEMPORARILY_LOCKED"))
                    self.assertEqual(immutable(row), before)
                errors = [row for row in self.receipt_records(ack.event_id)
                          if row.get("error_reason") == "TARGET_TEMPORARILY_LOCKED" and row.get("sqlstate")]
                self.assertEqual(len(errors), 1)
                error = errors[0]
                self.assertEqual((error["pg_backend_pid"], error["sqlstate"], error["relation"], error["lock_mode"]),
                                 (actual_pids[0], "55P03", relation, mode))
                self.assertEqual(error["error_stage"], "PLAYBACK_MATERIALIZE")
                self.assertEqual(error["lock_context"], "AUTHORITY_ROW")
                # Only the first Channel lock can wait. Held through the full
                # budget it still counts as a real failure with the same PID.
                self.assertEqual(error["lock_nowait"], relation != "channels")
                self.assertEqual((error["receipt_id"], error["history_id"], error["attempt_number"]),
                                 (ack.event_id, ack.event_id, 1))
                self.ready_again(ack.event_id)
                self.assertTrue(self.process())
                self.assertIsNone(self.receipt(ack.event_id))
                with Session(self.engine) as db:
                    self.assertIsNotNone(db.get(PlayerPlaybackEvent, ack.event_id))
                self.assertEqual(self.engine.pool.checkedout(), 0)
                self.assertEqual(blocker.pool.checkedout(), 0)

    def test_advisory_lock_errors_distinguish_tenant_barrier_from_media_coordination(self):
        from app.services.media_references import media_lock_key
        blocker = self.parallel_engine()
        targets = (
            (int.from_bytes(hashlib.sha256(("tecno.tenant.write.v1:" + self.ids["A"]["client"]).encode())
                            .digest()[:8], "big", signed=True), "TENANT_BARRIER"),
            (media_lock_key(self.root / self.ids["A"]["client"] / "owned.png"), "MEDIA_PATH"),
        )
        for key, context in targets:
            with self.subTest(lock_context=context):
                ack = self.accept()

                def bounded_qa_timeout(connection, cursor, statement, parameters, execution, many):
                    trace = diagnostic._active()
                    # QA-only acceleration of the blocking tenant barrier.
                    # The canonical media TRY probe never waits for this limit.
                    if (trace is not None and trace.receipt_id == ack.event_id
                            and statement == "SET LOCAL lock_timeout = '10s'"):
                        return "SET LOCAL lock_timeout = '100ms'", parameters
                    return statement, parameters

                event.listen(Engine, "before_cursor_execute", bounded_qa_timeout, retval=True)
                try:
                    with Session(blocker) as held:
                        held.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                        self.assertTrue(self.process())
                finally:
                    event.remove(Engine, "before_cursor_execute", bounded_qa_timeout)
                row = self.receipt(ack.event_id)
                is_media = context == "MEDIA_PATH"
                self.assertEqual((row.status, row.attempts, row.last_error_code),
                                 ("PENDING", 0, None) if is_media else ("PENDING", 1, "TARGET_TEMPORARILY_LOCKED"))
                errors = [row for row in self.receipt_records(ack.event_id)
                          if (row.get("event") == "playback_worker_media_deferred" if is_media else
                              row.get("error_reason") == "TARGET_TEMPORARILY_LOCKED")
                          and (row.get("sqlstate") or row.get("media_try_lock_granted") is False)]
                self.assertEqual(len(errors), 1)
                self.assertEqual((errors[0]["lock_context"], errors[0]["relation"], errors[0]["lock_mode"]),
                                 (context, None, "ADVISORY_SHARED"))
                if context == "MEDIA_PATH":
                    self.assertNotIn("error_reason", errors[0])
                    self.assertEqual(errors[0]["deferral_reason"], "MEDIA_COORDINATION_BUSY")
                    self.assertEqual(errors[0]["defer_seconds"], 1)
                    self.assertGreaterEqual(errors[0]["pending_age_seconds"], 0)
                    self.assertIsNone(errors[0]["sqlstate"])
                    self.assertIs(errors[0]["media_try_lock_granted"], False)
                    self.assertFalse(errors[0]["lock_nowait"])
                else:
                    self.assertEqual(errors[0]["sqlstate"], "55P03")
                self.assertGreater(errors[0]["pg_backend_pid"], 0)
                self.ready_again(ack.event_id)
                self.assertTrue(self.process())
                self.assertIsNone(self.receipt(ack.event_id))
                with Session(self.engine) as db:
                    self.assertIsNotNone(db.get(PlayerPlaybackEvent, ack.event_id))
                self.assertEqual(self.engine.pool.checkedout(), 0)
                self.assertEqual(blocker.pool.checkedout(), 0)

    def test_prolonged_real_lock_obeys_existing_terminal_policy_without_loss_or_loop(self):
        ack = self.accept()
        before = immutable(self.receipt(ack.event_id))
        blocker = self.parallel_engine()
        with Session(blocker) as held:
            held.scalar(select(PlayerDevice.id).where(PlayerDevice.id == self.ids["A"]["device"]).with_for_update())
            for attempt in range(1, 6):
                self.assertTrue(self.process())
                row = self.receipt(ack.event_id)
                self.assertEqual((row.attempts, row.status, row.last_error_code),
                                 (attempt, "FAILED" if attempt == 5 else "PENDING", "TARGET_TEMPORARILY_LOCKED"))
                self.assertEqual(immutable(row), before)
                if attempt < 5:
                    self.ready_again(ack.event_id)
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        errors = [row for row in self.receipt_records(ack.event_id)
                  if row.get("error_reason") == "TARGET_TEMPORARILY_LOCKED" and row.get("sqlstate") == "55P03"]
        self.assertEqual([row["attempt_number"] for row in errors], list(range(1, 6)))
        self.assertTrue(all(row["relation"] == "player_devices" and row["pg_backend_pid"] > 0 for row in errors))
        self.assertEqual(self.engine.pool.checkedout(), 0)
        self.assertEqual(blocker.pool.checkedout(), 0)
