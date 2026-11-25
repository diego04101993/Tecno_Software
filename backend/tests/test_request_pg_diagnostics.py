"""Small correlation/neutrality checks; PostgreSQL cases require owned QA."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import logging
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core import request_pg_diagnostics as diagnostic
from app.core.channel_lock_observer import sample_channel_waits
from app.models.entities import Channel
from app.services.db_snapshots import readonly_snapshot_session
from test_heartbeat_inbox import HeartbeatInboxPostgresTests
from test_tenant_gate_performance import GateFixture

_REAL_EMIT = diagnostic._emit


@contextmanager
def trace(endpoint="runtime-config", request_id=None):
    value = diagnostic._Trace(endpoint=endpoint, method="GET" if endpoint == "runtime-config" else "POST",
                              request_id=request_id or str(uuid4()), scope={"_player_admission_started_ns": 1})
    token = diagnostic._current.set(value)
    try:
        yield value
    finally:
        diagnostic._current.reset(token)


class RequestPGCoreTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        if self._testMethodName != "test_real_emit_through_security_factory_is_json_and_preserves_correlation":
            self.enterContext(patch.object(diagnostic, "_emit", self.records.append))
        diagnostic.install(FastAPI())

    def test_real_emit_through_security_factory_is_json_and_preserves_correlation(self):
        from app.core.security_logging import install_security_log_redaction
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(diagnostic.__name__)
        previous = (list(logger.handlers), logger.level, logger.propagate, logger.disabled,
                    logging.getLogRecordFactory())
        identifier, transaction = str(uuid4()), str(uuid4())
        secret = "tpw_SYNTHETIC_EMITTER_REGRESSION_ONLY"
        try:
            logger.handlers, logger.propagate, logger.disabled = [handler], False, False
            logger.setLevel(logging.WARNING)
            install_security_log_redaction()
            self.assertTrue(getattr(logging.getLogRecordFactory(), "_tecno_security_redaction", False))
            self.assertIs(diagnostic._emit, _REAL_EMIT)
            diagnostic._emit({"event": "request_pg_sql_error", "request_id": identifier,
                              "endpoint": "heartbeat", "stage": "INITIAL_LOCK",
                              "pg_backend_pid": 4242, "db_transaction_id": transaction,
                              "sqlstate": "55P03", "timestamp": "2026-11-20T00:00:00.000Z",
                              "note": secret})
            handler.flush()
        finally:
            logger.handlers, logger.level, logger.propagate, logger.disabled = previous[:4]
            logging.setLogRecordFactory(previous[4])
            handler.close()
        lines = stream.getvalue().splitlines()
        self.real_emitted_line_count = len(lines)
        self.assertEqual(len(lines), 1, "The real diagnostic emitter must produce one actual JSON log line")
        row = json.loads(lines[0])
        self.assertEqual((row["request_id"], row["pg_backend_pid"], row["db_transaction_id"]),
                         (identifier, 4242, transaction))
        self.assertEqual((row["event"], row["endpoint"], row["stage"], row["sqlstate"]),
                         ("request_pg_sql_error", "heartbeat", "INITIAL_LOCK", "55P03"))
        self.assertEqual(row["note"], "[REDACTED]")
        self.assertNotIn(secret, stream.getvalue())

    def test_runtime_request_uuid_and_http_bytes_are_unchanged(self):
        def application(observe):
            app = FastAPI()
            @app.get("/api/player/runtime-config")
            def runtime():
                with diagnostic.diagnostic_stage("runtime_builder"):
                    return {"runtime_version": 3, "playlist": []}
            if observe:
                diagnostic.install(app)
            return app
        identifier = str(uuid4())
        with TestClient(application(False)) as old, TestClient(application(True)) as current:
            before = old.get("/api/player/runtime-config", headers={"X-Request-ID": identifier})
            after = current.get("/api/player/runtime-config", headers={"X-Request-ID": identifier})
        self.assertEqual((after.status_code, after.content, dict(after.headers)),
                         (before.status_code, before.content, dict(before.headers)))
        self.assertEqual({row["request_id"] for row in self.records}, {identifier})
        events = {row["event"] for row in self.records}
        self.assertTrue({"runtime_request_start", "runtime_builder_start", "runtime_builder_end",
                         "runtime_serialization_start", "runtime_serialization_end", "runtime_response_ready"} <= events)

    def test_missing_or_invalid_request_id_has_one_stable_technical_uuid(self):
        for headers in ([], [(b"x-request-id", b"invalid-secret-body")]):
            scope = {"headers": headers}
            first = diagnostic.request_uuid(scope)
            self.assertTrue(diagnostic._valid_uuid(first))
            self.assertEqual(diagnostic.request_uuid(scope), first)
            self.assertNotIn("invalid-secret", first)

    def test_failure_snapshot_is_exact_and_not_a_previous_recovered_error(self):
        error = OperationalError("sensitive SQL", {"token": "sensitive-token"}, Exception("sensitive message"))
        original = {"pg_backend_pid": 42, "db_transaction_id": str(uuid4()), "stage": "WAIT_BEGIN",
                    "sqlstate": "55P03", "exception_class": "DBAPI_ERROR", "entity": "PlayerDevice"}
        error._request_pg_failure = dict(original)
        with trace():
            wrapped = HTTPException(503, "existing response")
            diagnostic.capture_failure(wrapped, source_error=error)
            diagnostic.capture_failure(wrapped)
            self.assertEqual(diagnostic.failure_metadata(wrapped), original)
            self.assertIsNone(diagnostic.failure_metadata(HTTPException(503, "different response"))["pg_backend_pid"])

    def test_wait_end_marker_keeps_failed_stage_separately(self):
        error = OperationalError("hidden", {}, Exception("hidden"))
        error._request_pg_failure = {"pg_backend_pid": 41, "db_transaction_id": str(uuid4()),
                                     "stage": "WAIT_BEGIN", "sqlstate": "55P03", "exception_class": "DBAPI_ERROR"}
        with trace("heartbeat"):
            diagnostic.diag_event("heartbeat_guard_stage", stage="WAIT_END", error=error)
        self.assertEqual(self.records[-1]["stage"], "WAIT_END")
        self.assertEqual(self.records[-1]["error_stage"], "WAIT_BEGIN")
        self.assertEqual(diagnostic.failure_metadata(error)["stage"], "WAIT_BEGIN")

    def test_query_shapes_never_export_sql_values_and_use_hmac(self):
        shape = diagnostic._shape("SELECT id FROM channels WHERE secret='TOKEN WITH SPACES' FOR SHARE NOWAIT")
        self.assertEqual((shape["sql_tables"], shape["lock_mode"], shape["lock_nowait"]), (["channels"], "FOR_SHARE", True))
        self.assertEqual(shape["sql_fingerprint_scope"], "process")
        self.assertEqual(len(shape["sql_fingerprint"]), 64)
        self.assertNotIn("TOKEN", json.dumps(shape))
        self.assertEqual(diagnostic._shape("SELECT pg_advisory_xact_lock_shared(123)")["lock_mode"], "ADVISORY_SHARED")

    def test_entity_target_is_hmac_and_stale_device_cannot_label_advisory_error(self):
        with trace("heartbeat") as current:
            diagnostic.diag_event("heartbeat_guard_stage", stage="INITIAL_LOCK", entity="PlayerDevice", target="synthetic-id")
            self.assertNotIn("synthetic-id", json.dumps(self.records))
            self.assertEqual(len(self.records[-1]["target_hmac"]), 64)
            state = {"pid": 42, "id": str(uuid4()), "owner": current, "count": 1,
                     "ending": None, "last": diagnostic._shape("SELECT pg_advisory_xact_lock(123)")}
            connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), _request_pg_diagnostic_state=state)
            metadata = diagnostic._error_metadata(Exception(), connection)
            self.assertNotIn("entity", metadata)

    def test_worker_scope_has_new_id_and_never_inherits_http_id(self):
        @diagnostic.worker_scope("heartbeat-worker")
        async def work():
            self.assertIsNone(diagnostic._active().request_id)
            self.assertIsNotNone(diagnostic._active().worker_id)
        with trace("heartbeat") as outer:
            asyncio.run(work())
            self.assertIs(diagnostic._active(), outer)
        self.assertEqual({row["request_id"] for row in self.records}, {None})
        self.assertEqual({row["event"] for row in self.records}, {"player_worker_start", "player_worker_end"})

    def test_heartbeat_channel_capture_scope_is_opt_in_and_restores_request(self):
        with trace("playback-events") as request:
            with patch.object(diagnostic, "_CHANNEL_CAPTURE", False), diagnostic.heartbeat_channel_lock_scope():
                self.assertIs(diagnostic._active(), request)
            with patch.object(diagnostic, "_CHANNEL_CAPTURE", True), diagnostic.heartbeat_channel_lock_scope():
                worker = diagnostic._active()
                self.assertEqual(worker.endpoint, "heartbeat-worker")
                self.assertTrue(worker.channel_only)
                self.assertIsNone(worker.request_id)
                self.assertTrue(diagnostic._valid_uuid(worker.worker_id))
            self.assertIs(diagnostic._active(), request)
        self.assertIsNone(diagnostic._active())
        self.assertEqual(self.records, [])

    def test_channel_capture_events_are_bounded_private_and_require_a_returned_row(self):
        receipt = str(uuid4())
        worker = diagnostic._Trace(endpoint="playback-worker", method="WORKER",
                                   worker_id=str(uuid4()), receipt_id=receipt)
        state = {"pid": 42, "id": str(uuid4())}
        for statement in (
            "SELECT id FROM clients FOR UPDATE",
            "SELECT id FROM channels",
            "SELECT channels.id FROM channels JOIN clients ON true FOR UPDATE",
        ):
            metadata = {**diagnostic._shape(statement), "db_operation_id": 1}
            self.assertFalse(diagnostic._channel_attempt(worker, state, metadata))
        self.assertEqual(self.records, [])
        metadata = {**diagnostic._shape("SELECT id FROM channels WHERE id='synthetic-id' FOR SHARE"),
                    "db_operation_id": 2}
        self.assertTrue(diagnostic._channel_attempt(worker, state, metadata))
        diagnostic._channel_acquired(worker, state, metadata, 0)
        self.assertEqual([row["event"] for row in self.records], ["channel_lock_attempt"])
        for _ in range(3):
            self.assertTrue(diagnostic._channel_attempt(worker, state, metadata))
            diagnostic._channel_acquired(worker, state, metadata, 1)
        self.assertEqual([row["event"] for row in self.records],
                         ["channel_lock_attempt", "channel_lock_acquired"])
        self.assertEqual({row["pg_backend_pid"] for row in self.records}, {42})
        self.assertEqual({row["db_transaction_id"] for row in self.records}, {state["id"]})
        self.assertEqual({row["worker_id"] for row in self.records}, {worker.worker_id})
        for item in (receipt, "synthetic-id", "receipt_id", "history_id", "sql_fingerprint"):
            self.assertNotIn(item, json.dumps(self.records))

    def test_channel_wait_observer_keeps_empty_or_invalid_inputs_off_database(self):
        pg = Mock(dialect=SimpleNamespace(name="postgresql"))
        self.assertEqual(sample_channel_waits(pg, []), [])
        for invalid in ([True], [0], [-1], [2**31], ["42"], list(range(1, 18))):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sample_channel_waits(pg, invalid)
        pg.execute.assert_not_called()
        sqlite = Mock(dialect=SimpleNamespace(name="sqlite"))
        with self.assertRaises(ValueError):
            sample_channel_waits(sqlite, [42])
        sqlite.execute.assert_not_called()

        pair = {"waiter_pid": 42, "blocker_pid": 43, "wait_event": "transactionid"}
        pg.execute.return_value.mappings.return_value = [pair]
        self.assertEqual(sample_channel_waits(pg, [42, 42]), [pair])
        pg.execute.assert_called_once()
        query, params = pg.execute.call_args.args
        self.assertEqual(params, {"pids": [42]})
        self.assertIn("pg_blocking_pids", str(query))
        self.assertNotIn(".query", str(query))

    def test_observer_setup_failure_cannot_prevent_actual_commit_or_rollback(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE neutral (value integer)"))
            connection.commit()
            connection.execute(text("INSERT INTO neutral VALUES (1)"))
            with patch.object(diagnostic, "_state", side_effect=RuntimeError("observer only")):
                self.assertIsNone(connection.commit())
            connection.execute(text("INSERT INTO neutral VALUES (2)"))
            with patch.object(diagnostic, "_state", side_effect=RuntimeError("observer only")):
                self.assertIsNone(connection.rollback())
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM neutral")), 1)
            sentinel = RuntimeError("original commit failure")
            with patch.object(diagnostic, "_state", side_effect=RuntimeError("observer only")), patch.object(engine.dialect, "do_commit", side_effect=sentinel):
                with self.assertRaises(RuntimeError) as caught:
                    connection.commit()
            self.assertIs(caught.exception, sentinel)

    def test_logging_failure_cannot_change_http_response(self):
        app = FastAPI()
        @app.get("/api/player/runtime-config")
        def runtime():
            return {"same": True}
        diagnostic.install(app)
        # Exercise the real fail-open sink rather than replacing the caller.
        with patch.object(diagnostic, "_emit", _REAL_EMIT), patch("logging.Logger.warning", side_effect=RuntimeError("sink failure")), TestClient(app) as client:
            response = client.get("/api/player/runtime-config")
        self.assertEqual((response.status_code, response.json()), (200, {"same": True}))

    def test_actual_exception_classification_does_not_invent_channel_for_multi_table_lock(self):
        error = OperationalError("hidden", {}, Exception("hidden"))
        error._request_pg_failure = {"pg_backend_pid": 42, "db_transaction_id": str(uuid4()), "stage": "RUNTIME_TENANT_GUARD",
                                     "sqlstate": "55P03", "exception_class": "DBAPI_ERROR",
                                     "sql_tables": ["channels", "clients"], "lock_mode": "MULTIPLE"}
        self.assertEqual(diagnostic.runtime_reason(error), "RUNTIME_TENANT_GUARD_LOCK_UNAVAILABLE")
        error._request_pg_failure["stage"] = "RUNTIME_BUILDER"
        self.assertEqual(diagnostic.runtime_reason(error), "RUNTIME_DB_ERROR")
        wrapped = HTTPException(503, "same response")
        wrapped.__cause__ = TimeoutError("sensitive detail")
        self.assertEqual(diagnostic.runtime_reason(wrapped), "RUNTIME_TIMEOUT_OR_CANCEL")
        self.assertIsNone(diagnostic.failure_metadata(wrapped)["pg_backend_pid"])


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned isolated PostgreSQL QA required")
class RequestPGPostgresTests(GateFixture):
    make_engine = HeartbeatInboxPostgresTests.make_engine

    def setUp(self):
        super().setUp()
        self.records = []
        self.enterContext(patch.object(diagnostic, "_emit", self.records.append))
        diagnostic.install(FastAPI())

    def test_driver_pid_matches_pg_function_and_reused_pid_has_new_transaction_id(self):
        observed = []
        for endpoint in ("heartbeat", "runtime-config", "playback-events"):
            with trace(endpoint) as current:
                with self.engine.connect() as connection:
                    actual = connection.scalar(text("SELECT pg_backend_pid()"))
                    state = diagnostic._state(connection)
                    self.assertEqual(state["pid"], actual)
                    observed.append((current.request_id, actual, state["id"]))
                    connection.commit()
        self.assertEqual(len({row[0] for row in observed}), 3)
        self.assertEqual(len({row[2] for row in observed}), 3)
        self.assertEqual(len({row[1] for row in observed}), 1)
        self.assertEqual(len([row for row in self.records if row["event"] == "request_pg_transaction_end"]), 3)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_readonly_snapshot_has_fresh_transaction_and_actual_pid(self):
        with trace() as current, Session(self.engine) as source:
            original_pid = source.scalar(text("SELECT pg_backend_pid()"))
            with readonly_snapshot_session(source) as snapshot:
                actual_pid = snapshot.scalar(text("SELECT pg_backend_pid()"))
                self.assertEqual(snapshot.scalar(text("SHOW transaction_read_only")), "on")
                self.assertEqual(snapshot.scalar(text("SHOW transaction_isolation")), "repeatable read")
                self.assertEqual(diagnostic._database_metadata(snapshot)["pg_backend_pid"], actual_pid)
        beginnings = [row for row in self.records if row["event"] == "request_pg_transaction_start"]
        self.assertEqual(len(beginnings), 2)
        self.assertEqual({row["request_id"] for row in beginnings}, {current.request_id})
        self.assertEqual(len({row["db_transaction_id"] for row in beginnings}), 2)
        self.assertEqual({row["pg_backend_pid"] for row in beginnings}, {original_pid, actual_pid})
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_null_pool_transaction_is_correlated_without_extra_diagnostic_sql(self):
        engine = create_engine(self.engine.url, poolclass=NullPool, hide_parameters=True)
        self.addCleanup(engine.dispose)
        from sqlalchemy import event
        statements = []
        event.listen(engine, "before_cursor_execute", lambda _c, _u, statement, _p, _x, _m: statements.append(statement))
        with trace("playback-events") as current, engine.connect() as connection:
            actual = connection.scalar(text("SELECT pg_backend_pid()"))
            self.assertEqual(diagnostic._database_metadata(connection=connection)["pg_backend_pid"], actual)
        self.assertEqual(statements, ["SELECT pg_backend_pid()"])
        self.assertEqual({row["request_id"] for row in self.records}, {current.request_id})

    def test_postgres_channel_capture_opt_in_and_empty_row_does_not_acquire(self):
        for enabled, identity in ((False, self.ids["A"]["channel"]), (True, str(uuid4()))):
            with self.subTest(enabled=enabled), patch.object(diagnostic, "_CHANNEL_CAPTURE", enabled):
                self.records.clear()
                with trace("playback-events") as current, Session(self.engine) as db:
                    found = db.scalar(select(Channel.id).where(Channel.id == identity).with_for_update(read=True))
                    db.commit()
                self.assertEqual(found, None if enabled else identity)
                self.assertTrue(any(row["event"] == "request_pg_sql_end" for row in self.records))
                capture = [row for row in self.records if row["event"].startswith("channel_lock_")]
                if enabled:
                    self.assertEqual([row["event"] for row in capture],
                                     ["channel_lock_attempt", "channel_lock_transaction_end"])
                    self.assertEqual(capture[-1]["acquired_modes"], [])
                    self.assertTrue(all(row["request_id"] == current.request_id for row in capture))
                else:
                    self.assertEqual(capture, [])
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_postgres_real_heartbeat_materializer_captures_commit_and_rollback(self):
        from app.api.routes import player
        from app.models.entities import PlayerDevice, PlayerHeartbeat, PlayerHeartbeatInbox
        from app.schemas.domain import PlayerHeartbeatCreate
        from app.services import heartbeat_inbox

        ids = self.ids["A"]

        def abandon_after_claim(payload, device, db, **kwargs):
            claim = kwargs.pop("before_commit")

            def abandon(plan):
                claim(plan)
                raise RuntimeError("QA-only processor rollback after valid claim")

            return player._materialize_player_heartbeat(payload, device, db, before_commit=abandon, **kwargs)

        for expected, processor in (("COMMIT", player._materialize_player_heartbeat),
                                    ("ROLLBACK", abandon_after_claim)):
            with self.subTest(expected=expected), patch.object(diagnostic, "_CHANNEL_CAPTURE", True):
                with Session(self.engine, autoflush=False) as db:
                    heartbeat_inbox.enqueue(db, db.get(PlayerDevice, ids["device"]), db.get(Channel, ids["channel"]),
                                            PlayerHeartbeatCreate(status="online", current_content_id=ids["content"]))
                self.records.clear()
                with trace("playback-events") as request:
                    self.assertTrue(heartbeat_inbox.materialize_one(self.engine, processor))
                    self.assertIs(diagnostic._active(), request)
                capture = [row for row in self.records if row["event"].startswith("channel_lock_")]
                self.assertEqual([row["event"] for row in capture], [
                    "channel_lock_attempt", "channel_lock_acquired", "channel_lock_transaction_end",
                ])
                self.assertEqual({row["endpoint"] for row in capture}, {"heartbeat-worker"})
                self.assertEqual({row["request_id"] for row in capture}, {None})
                self.assertEqual(len({row["worker_id"] for row in capture}), 1)
                self.assertTrue(diagnostic._valid_uuid(capture[0]["worker_id"]))
                self.assertEqual(len({row["pg_backend_pid"] for row in capture}), 1)
                self.assertGreater(capture[0]["pg_backend_pid"], 0)
                self.assertEqual(len({row["db_transaction_id"] for row in capture}), 1)
                self.assertEqual([row["timestamp"] for row in capture], sorted(row["timestamp"] for row in capture))
                self.assertEqual(capture[1]["lock_mode"], "FOR_UPDATE")
                self.assertEqual((capture[2]["operation"], capture[2]["outcome"], capture[2]["acquired_modes"]),
                                 (expected, "ok", ["FOR_UPDATE"]))
                for private_value in (ids["channel"], ids["device"], ids["client"], "receipt_id", "history_id"):
                    self.assertNotIn(private_value, json.dumps(capture))
        with Session(self.engine) as db:
            self.assertEqual(len(list(db.scalars(select(PlayerHeartbeat.id)))), 1)
            self.assertEqual(len(list(db.scalars(select(PlayerHeartbeatInbox.id)))), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_real_lock_error_keeps_exact_pid_transaction_and_state_after_rollback(self):
        with Session(self.engine) as holder:
            holder.scalar(select(Channel.id).where(Channel.id == self.ids["A"]["channel"]).with_for_update())
            with trace() as current, Session(self.engine) as request:
                pid = request.scalar(text("SELECT pg_backend_pid()"))
                with self.assertRaises(OperationalError) as caught:
                    with diagnostic.diagnostic_stage("runtime_db", db=request):
                        request.scalar(select(Channel.id).where(Channel.id == self.ids["A"]["channel"]).with_for_update(read=True, nowait=True))
                snapshot = diagnostic.failure_metadata(caught.exception)
                request.rollback()
                self.assertEqual(diagnostic.failure_metadata(caught.exception), snapshot)
                self.assertEqual((snapshot["pg_backend_pid"], snapshot["sqlstate"], snapshot["stage"]), (pid, "55P03", "RUNTIME_DB"))
                self.assertEqual(diagnostic.runtime_reason(caught.exception), "RUNTIME_CHANNEL_LOCK_UNAVAILABLE")
                other = HTTPException(503, "unrelated")
                self.assertIsNone(diagnostic.failure_metadata(other)["pg_backend_pid"])
            holder.rollback()
        failures = [row for row in self.records if row["event"] == "request_pg_sql_error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["request_id"], failures[0]["pg_backend_pid"]), (current.request_id, pid))
        self.assertNotIn(self.ids["A"]["channel"], json.dumps(self.records))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_concurrent_requests_never_exchange_ids_or_backend_pids(self):
        barrier = threading.Barrier(2)
        def one(endpoint):
            with trace(endpoint) as current, self.engine.connect() as connection:
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                barrier.wait(timeout=3)
                connection.commit()
                return current.request_id, pid
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(one, "heartbeat")
            second = pool.submit(one, "playback-events")
            answers = [first.result(timeout=5), second.result(timeout=5)]
        self.assertNotEqual(answers[0][1], answers[1][1])
        for request_id, pid in answers:
            self.assertEqual({row["pg_backend_pid"] for row in self.records if row["request_id"] == request_id}, {pid})
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_worker_transactions_do_not_borrow_http_identity_or_log_idle_polls(self):
        @diagnostic.worker_scope("playback-worker")
        async def work():
            for _ in range(3):
                with self.engine.connect() as connection:
                    connection.scalar(text("SELECT pg_backend_pid()"))
        with trace("heartbeat") as request:
            asyncio.run(work())
        self.assertEqual(len(self.records), 2)
        self.assertTrue(all(row["request_id"] is None and row["worker_id"] for row in self.records))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def http_lock_error(self, endpoint):
        """Real HTTP + actual admission + actual PG55P03; controlled SQL route."""
        from app.api.routes.player import _admit_player_activity
        from app.core.playback_failure_diagnostics import Playback503DiagnosticsMiddleware, handler_entered
        app = FastAPI()
        actual_pids = []
        def database():
            with Session(self.engine) as session:
                yield session

        def operation(request, session):
            if endpoint == "runtime-config":
                diagnostic.runtime_handler_entered(request)
            else:
                handler_entered(request)
            try:
                with diagnostic.diagnostic_stage("runtime_db" if endpoint == "runtime-config" else "request", db=session):
                    actual_pids.append(session.scalar(text("SELECT pg_backend_pid()")))
                    session.scalar(select(Channel.id).where(Channel.id == self.ids["A"]["channel"])
                                   .with_for_update(read=True, nowait=True))
            except OperationalError as error:
                session.rollback()
                detail = "Runtime snapshot unavailable" if endpoint == "runtime-config" else "Player operation temporarily unavailable"
                raise diagnostic.capture_failure(HTTPException(503, detail), source_error=error) from None
            raise AssertionError("Controlled held PG lock unexpectedly succeeded")

        if endpoint == "runtime-config":
            @app.get("/api/player/runtime-config", dependencies=[Depends(_admit_player_activity)])
            def runtime(request: Request, session: Session = Depends(database)):
                return operation(request, session)
        else:
            @app.post("/api/player/playback-events", dependencies=[Depends(_admit_player_activity)])
            def playback(request: Request, session: Session = Depends(database)):
                return operation(request, session)
            app.add_middleware(Playback503DiagnosticsMiddleware)
        diagnostic.install(app)
        identifier = str(uuid4())
        with Session(self.engine) as holder, TestClient(app) as client:
            holder.scalar(select(Channel.id).where(Channel.id == self.ids["A"]["channel"]).with_for_update())
            if endpoint == "runtime-config":
                response = client.get("/api/player/runtime-config", headers={"X-Request-ID": identifier})
            else:
                response = client.post("/api/player/playback-events", headers={"X-Request-ID": identifier},
                                       json={"status": "completed", "synthetic_private": "DO_NOT_LOG"})
            holder.rollback()
        expected = "Runtime snapshot unavailable" if endpoint == "runtime-config" else "Player operation temporarily unavailable"
        self.assertEqual((response.status_code, response.json()), (503, {"detail": expected}))
        name = "runtime_http_503" if endpoint == "runtime-config" else "playback_http_503"
        if endpoint == "runtime-config":
            failures = [row for row in self.records if row["event"] == name]
        else:
            # Playback uses its preserved, separate safe logger sink.
            failures = self.playback_records
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["request_id"], failures[0]["pg_backend_pid"], failures[0]["sqlstate"]),
                         (identifier, actual_pids[0], "55P03"))
        sql_errors = [row for row in self.records if row["event"] == "request_pg_sql_error"]
        self.assertEqual(len(sql_errors), 1)
        self.assertEqual((sql_errors[0]["request_id"], sql_errors[0]["pg_backend_pid"]), (identifier, actual_pids[0]))
        self.assertNotIn("DO_NOT_LOG", json.dumps(self.records))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_real_runtime_http503_has_exact_request_pid_sqlstate_and_body(self):
        self.http_lock_error("runtime-config")

    def test_real_playback_http503_has_exact_request_pid_sqlstate_and_body(self):
        self.playback_records = []
        original = __import__("logging").getLogger("app.core.playback_failure_diagnostics")
        def record(line):
            self.playback_records.append(json.loads(line))
        with patch.object(original, "warning", side_effect=record):
            self.http_lock_error("playback-events")
