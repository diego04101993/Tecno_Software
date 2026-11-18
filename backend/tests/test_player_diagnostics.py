"""Opt-in Player diagnostics: isolated processes, synthetic HTTP and SQLite.

The application entry point/startup is never imported. Each worker gets its
own temporary SQLite database and log handler, so global SQLAlchemy hooks and
the environment flag cannot affect other suites in the same Python process.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


BACKEND = Path(__file__).resolve().parents[1]
RESULT_MARKER = "PLAYER_DIAGNOSTICS_TEST_RESULT="


class PlayerDiagnosticsTests(unittest.TestCase):
    def run_case(self, scenario, enabled="1"):
        with TemporaryDirectory(prefix="player-diagnostics-test-") as directory:
            environment = dict(os.environ)
            environment.update({
                "APP_ENV": "test", "DATABASE_URL": "sqlite://",
                "AUTO_SEED": "false", "DEBUG": "false",
                "SECRET_KEY": "synthetic-diagnostics-test-secret-" + "7" * 32,
                "MEDIA_ROOT": str(Path(directory) / "media"),
                "PYTHONDONTWRITEBYTECODE": "1",
                # Preserve the runner's already available QA libraries. This
                # is also used by the offline Linux image with a read-only
                # cache of pure-Python HTTP test dependencies.
                "PYTHONPATH": os.pathsep.join(dict.fromkeys(
                    [str(BACKEND), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))])),
                "DIAGNOSTIC_TEST_DIRECTORY": directory,
            })
            if enabled is None:
                environment.pop("PLAYER_DIAGNOSTICS", None)
            else:
                environment["PLAYER_DIAGNOSTICS"] = enabled
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()),
                 "--diagnostic-worker", scenario],
                cwd=str(BACKEND), env=environment, capture_output=True,
                text=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(completed.returncode, 0,
                             completed.stdout + "\n" + completed.stderr)
            results = [line[len(RESULT_MARKER):] for line in completed.stdout.splitlines()
                       if line.startswith(RESULT_MARKER)]
            self.assertEqual(len(results), 1, completed.stdout)
            return json.loads(results[0])

    def test_unset_and_other_flag_values_leave_original_callables_and_hooks(self):
        for enabled in (None, "", "0", "false", "true", "yes"):
            with self.subTest(flag=enabled):
                self.run_case("disabled", enabled)

    def test_enabled_preserves_http_payload_status_and_other_headers(self):
        disabled = self.run_case("contracts", "0")
        enabled = self.run_case("contracts")
        self.assertEqual(enabled["responses"], disabled["responses"])
        self.assertEqual(len(enabled["events"]), 3)
        self.assertEqual(disabled["events"], [])

    def test_only_the_three_exact_player_paths_are_diagnosed(self):
        self.run_case("paths")

    def test_request_id_preserved_only_for_a_single_valid_uuid(self):
        self.run_case("request_ids")

    def test_stages_are_correlated_and_unexecuted_stages_are_explicit(self):
        self.run_case("stages")

    def test_other_time_uses_interval_union_without_broad_container_spans(self):
        self.run_case("interval_union")

    def test_failed_sql_and_commit_release_connections_and_request_state(self):
        self.run_case("database_errors")

    def test_exception_text_parameters_headers_query_and_body_are_never_logged(self):
        self.run_case("privacy")

    def test_request_and_admission_counters_are_isolated_under_concurrency(self):
        self.run_case("concurrency")

    def test_process_sampler_starts_without_application_lifespan(self):
        self.run_case("sampler")

    def test_sink_failure_does_not_change_http_or_application_exceptions(self):
        self.run_case("sink_failure")

    def test_bounded_timelines_keep_exact_counts_and_exclusive_totals(self):
        self.run_case("trace_bound")

    def test_wrappers_preserve_sync_async_signatures_values_and_exceptions(self):
        self.run_case("callables")


def _worker(scenario):
    # These imports happen only after the worker environment is isolated.
    sys.path.insert(0, str(BACKEND))
    import asyncio
    from contextlib import redirect_stdout
    import inspect
    import io
    import threading
    from uuid import UUID, uuid4
    from unittest.mock import patch

    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import Column, Integer, String, Table, create_engine, text
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
    from sqlalchemy.orm import Session, registry, sessionmaker
    from sqlalchemy.pool import QueuePool
    from sqlalchemy.pool import Pool
    import fastapi.routing
    from starlette.responses import JSONResponse as StarletteJSONResponse

    testcase = unittest.TestCase()
    original_connect, original_commit = Engine.connect, Session.commit
    original_pool_connect = Pool.connect
    original_serialize = fastapi.routing.serialize_response
    original_render = StarletteJSONResponse.render
    from app.core import player_diagnostics as diagnostic

    database_path = Path(os.environ["DIAGNOSTIC_TEST_DIRECTORY"]) / "owned.sqlite"
    engine = create_engine("sqlite:///" + str(database_path),
                           poolclass=QueuePool, pool_size=2, max_overflow=0,
                           pool_timeout=0.2, hide_parameters=True,
                           connect_args={"check_same_thread": False})
    mapper_registry = registry()
    table = Table("diagnostic_fixture", mapper_registry.metadata,
                  Column("id", Integer, primary_key=True), Column("value", String))

    class FixtureRow:
        pass

    mapper_registry.map_imperatively(FixtureRow, table)
    mapper_registry.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, value="synthetic-existing-row"))
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    captured = []
    original_emit = diagnostic._emit
    emit_lock = threading.RLock()

    def capture_emit(payload):
        # Exercise the actual JSON encoder/sink rather than asserting on the
        # in-memory object passed to it. A single lock protects stdout changes
        # while thread/coroutine instrumentation overlaps in the worker.
        with emit_lock:
            output = io.StringIO()
            with redirect_stdout(output):
                original_emit(payload)
            for line in output.getvalue().splitlines():
                captured.append(json.loads(line))

    diagnostic._emit = capture_emit

    def request_events():
        return [row for row in captured if row.get("event") == "player_request_diagnostics"]

    def get_db():
        with sessions() as db:
            yield db

    @diagnostic.observed("AUTH")
    def authenticate(request: Request, db: Session = Depends(get_db)):
        db.execute(text("SELECT 1"))
        return "synthetic-authorized"

    @diagnostic.observed("HANDLER")
    def handler_function(request: Request, db: Session = Depends(get_db),
                         authenticated=Depends(authenticate)):
        with diagnostic.diagnostic_span("OWNERSHIP"):
            db.execute(text("SELECT 2"))
        with diagnostic.diagnostic_span("MEDIA_VALIDATION"):
            db.execute(text("SELECT 3"))
        action = request.headers.get("x-test-action")
        if action == "sql_error":
            try:
                db.execute(text("SELECT sql_secret_synthetic FROM missing_secret_synthetic"))
            except SQLAlchemyError:
                db.rollback()
                raise HTTPException(409, "Synthetic SQL failure") from None
        if action == "commit_error":
            db.add(FixtureRow(id=1, value="synthetic_commit_secret_value"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "Synthetic commit failure") from None
        if action == "unexpected_error":
            raise RuntimeError("synthetic_private_exception_message")
        db.commit()
        return JSONResponse({"synthetic": "same-payload", "path": request.url.path},
                            status_code=201 if request.url.path.endswith("playback-events") else 200,
                            headers={"x-existing-header": "unchanged"})

    for path, methods in (
        ("/api/player/runtime-config", ["GET"]),
        ("/api/player/heartbeat", ["POST"]),
        ("/api/player/playback-events", ["POST"]),
    ):
        app.add_api_route(path, handler_function, methods=methods)

    if scenario == "disabled":
        def original(value: int = 3):
            return value

        async def original_async(value: int = 3):
            return value

        testcase.assertIs(diagnostic.observed("AUTH")(original), original)
        testcase.assertIs(diagnostic.observed("AUTH")(original_async), original_async)
        before = {
            name: len(getattr(engine.dispatch, name))
            for name in ("before_cursor_execute", "after_cursor_execute", "begin", "commit", "rollback")
        }
        diagnostic.install(app, engine)
        testcase.assertEqual(app.user_middleware, [])
        testcase.assertIs(Engine.connect, original_connect)
        testcase.assertIs(Pool.connect, original_pool_connect)
        testcase.assertIs(Session.commit, original_commit)
        testcase.assertIs(fastapi.routing.serialize_response, original_serialize)
        testcase.assertIs(StarletteJSONResponse.render, original_render)
        for name, count in before.items():
            testcase.assertEqual(len(getattr(engine.dispatch, name)), count)
        testcase.assertEqual(captured, [])
        engine.dispose()
        return {"disabled": True}

    diagnostic.install(app, engine)
    if diagnostic.ENABLED:
        installed = (Pool.connect, Session.commit, fastapi.routing.serialize_response,
                     StarletteJSONResponse.render)
        diagnostic.install(app, engine)
        testcase.assertEqual(len(app.user_middleware), 1)
        testcase.assertEqual(installed, (Pool.connect, Session.commit, fastapi.routing.serialize_response,
                                        StarletteJSONResponse.render),
                             "Repeated installation must not stack process-wide hooks")

    if scenario == "contracts":
        responses = []
        with TestClient(app) as client:
            for method, path in (
                ("GET", "/api/player/runtime-config"),
                ("POST", "/api/player/heartbeat"),
                ("POST", "/api/player/playback-events"),
            ):
                response = client.request(method, path, json={"synthetic": True})
                responses.append({"status": response.status_code, "body": response.json(),
                                  "headers": dict(response.headers)})
        testcase.assertEqual(engine.pool.checkedout(), 0)
        engine.dispose()
        return {"responses": responses, "events": request_events()}

    if scenario == "paths":
        for path in ("/", "/api/player/time", "/api/player/activate",
                     "/api/channels/", "/api/player/heartbeat-extra",
                     "/api/player/runtime-config/", "/other/api/player/heartbeat"):
            app.add_api_route(path, lambda: {"unchanged": True}, methods=["GET", "POST"])
        with TestClient(app) as client:
            for path in ("/", "/api/player/time", "/api/player/activate",
                         "/api/channels/", "/api/player/heartbeat-extra",
                         "/api/player/runtime-config/", "/other/api/player/heartbeat"):
                response = client.get(path)
                testcase.assertEqual(response.status_code, 200)
                testcase.assertEqual(response.json(), {"unchanged": True})
                testcase.assertNotIn("x-request-id", response.headers)
            testcase.assertEqual(request_events(), [])
            for method, path in (("GET", "/api/player/runtime-config"),
                                 ("POST", "/api/player/heartbeat"),
                                 ("POST", "/api/player/playback-events")):
                response = client.request(method, path)
                testcase.assertNotIn("x-request-id", response.headers)
            for method, path in (("POST", "/api/player/runtime-config"),
                                 ("GET", "/api/player/heartbeat"),
                                 ("GET", "/api/player/playback-events")):
                response = client.request(method, path)
                testcase.assertEqual(response.status_code, 405)
        testcase.assertEqual(len(request_events()), 3)
        engine.dispose()
        return {"requests_diagnosed": 3}

    if scenario == "request_ids":
        valid = str(uuid4())
        invalid = ["invalid-synthetic-id", " " + valid, valid + " ",
                   valid + "\r\nInjected: synthetic", "f" * 2000]
        ids = []
        with TestClient(app) as client:
            response = client.get("/api/player/runtime-config", headers={"x-request-id": valid})
            testcase.assertNotIn("x-request-id", response.headers)
            testcase.assertEqual(request_events()[-1]["request_id"], valid)
            ids.append(valid)
            for supplied in invalid:
                response = client.get("/api/player/runtime-config", headers={"x-request-id": supplied})
                testcase.assertNotIn("x-request-id", response.headers)
                returned = request_events()[-1]["request_id"]
                testcase.assertNotEqual(returned, supplied)
                testcase.assertEqual(str(UUID(returned)), returned)
                ids.append(returned)
            response = client.get("/api/player/runtime-config",
                                  headers=[("x-request-id", valid), ("x-request-id", str(uuid4()))])
            duplicate_result = request_events()[-1]["request_id"]
            testcase.assertNotEqual(duplicate_result, valid)
            testcase.assertEqual(str(UUID(duplicate_result)), duplicate_result)
            ids.append(duplicate_result)
            response = client.get("/api/player/runtime-config")
            generated = request_events()[-1]["request_id"]
            testcase.assertEqual(str(UUID(generated)), generated)
            ids.append(generated)
        testcase.assertEqual(len(set(ids)), len(ids))
        testcase.assertEqual([row["request_id"] for row in request_events()], ids)
        testcase.assertNotIn("Injected", json.dumps(captured))
        # Oversized/unshaped input must be rejected before invoking the UUID
        # parser; only one canonical-size value needs parsing.
        fallback = UUID("00000000-0000-4000-8000-000000000001")
        with patch.object(diagnostic.uuid, "UUID") as parser, \
                patch.object(diagnostic.uuid, "uuid4", return_value=fallback):
            for supplied in (b"", b"short", b"f" * 2000):
                generated = diagnostic._request_uuid([(b"x-request-id", supplied)])
                testcase.assertEqual(str(UUID(generated)), generated)
            parser.assert_not_called()
        engine.dispose()
        return {"checked_ids": len(ids)}

    if scenario == "stages":
        with TestClient(app) as client:
            response = client.get("/api/player/runtime-config")
        testcase.assertEqual(response.status_code, 200)
        testcase.assertEqual(len(request_events()), 1)
        row = request_events()[0]
        testcase.assertEqual(row["endpoint"], "/api/player/runtime-config")
        testcase.assertEqual(row["method"], "GET")
        testcase.assertEqual(row["status_code"], 200)
        testcase.assertFalse(row["failed"])
        testcase.assertTrue(row["response_started"])
        testcase.assertTrue(row["response_completed"])
        testcase.assertGreaterEqual(row["end_ns"], row["start_ns"])
        testcase.assertEqual(set(row["stages"]), set(diagnostic.STAGES))
        for stage in ("AUTH", "HANDLER", "OWNERSHIP", "MEDIA_VALIDATION",
                      "POOL_ACQUIRE", "SQL", "TRANSACTION", "COMMIT", "JSON_RENDER"):
            metrics = row["stages"][stage]
            testcase.assertEqual(metrics["status"], "observed", stage)
            testcase.assertGreater(metrics["count"], 0, stage)
            testcase.assertEqual(metrics["errors"], 0, stage)
            testcase.assertGreaterEqual(metrics["total_ms"], 0, stage)
            for span in metrics["spans"]:
                testcase.assertGreaterEqual(span["start_ns"], row["start_ns"])
                testcase.assertGreaterEqual(span["end_ns"], span["start_ns"])
                testcase.assertLessEqual(span["end_ns"], row["end_ns"])
                testcase.assertEqual(span["outcome"], "ok")
        for stage in ("ADMISSION", "SERIALIZATION"):
            metrics = row["stages"][stage]
            testcase.assertEqual(metrics["status"], "stage_not_applicable", stage)
            testcase.assertEqual(metrics["count"], 0, stage)
            testcase.assertIsNone(metrics["total_ms"], stage)
            testcase.assertEqual(metrics["spans"], [], stage)
        testcase.assertEqual(row["process_at_completion"]["inflight"], 0)
        testcase.assertEqual(row["process_at_completion"]["pool_acquiring"], 0)
        testcase.assertGreater(row["process_at_completion"]["python_thread_count"], 0)
        testcase.assertIn("total_tokens", row["process_at_completion"]["anyio_limiter"])
        testcase.assertIn("borrowed_tokens", row["process_at_completion"]["anyio_limiter"])
        testcase.assertIn("tasks_waiting", row["process_at_completion"]["anyio_limiter"])
        checkpoints = {checkpoint["name"]: checkpoint for checkpoint in row["checkpoints"]}
        for name in ("MIDDLEWARE_ENTER", "RESPONSE_HEADERS_READY"):
            checkpoint = checkpoints[name]
            testcase.assertGreater(checkpoint["counters"]["python_thread_count"], 0)
            testcase.assertGreater(checkpoint["anyio_limiter"]["total_tokens"], 0)
            testcase.assertGreaterEqual(checkpoint["anyio_limiter"]["borrowed_tokens"], 0)
            testcase.assertGreaterEqual(checkpoint["anyio_limiter"]["tasks_waiting"], 0)
        milestones = row["milestones"]
        for key in ("REQUEST_ACCEPTED", "MIDDLEWARE_ENTER", "AUTH_START", "AUTH_END",
                    "HANDLER_ENTER", "HANDLER_START", "HANDLER_END", "OWNERSHIP_START",
                    "OWNERSHIP_END", "MEDIA_VALIDATION_START", "MEDIA_VALIDATION_END",
                    "SQL_TOTAL_START", "SQL_TOTAL_END", "COMMIT_START", "COMMIT_END",
                    "RESPONSE_HEADERS_READY", "REQUEST_END"):
            testcase.assertIn(key, milestones)
            testcase.assertGreaterEqual(milestones[key], row["start_ns"])
            testcase.assertLessEqual(milestones[key], row["end_ns"])
        ordered = ("REQUEST_ACCEPTED", "MIDDLEWARE_ENTER", "AUTH_START", "AUTH_END",
                   "HANDLER_ENTER", "RESPONSE_HEADERS_READY", "REQUEST_END")
        testcase.assertEqual([milestones[key] for key in ordered],
                             sorted(milestones[key] for key in ordered))
        testcase.assertEqual(row["sql_total"]["count"], row["stages"]["SQL"]["count"])
        testcase.assertEqual(row["sql_total"]["execution_ms"], row["stages"]["SQL"]["total_ms"])
        testcase.assertGreaterEqual(row["sql_total"]["elapsed_ms"], row["sql_total"]["execution_ms"])
        testcase.assertAlmostEqual(sum(row["exclusive_breakdown_ms"].values()),
                                   row["duration_ms"], places=4)
        testcase.assertEqual(engine.pool.checkedout(), 0)
        engine.dispose()
        return {"event": row}

    if scenario == "interval_union":
        # Handler/transaction are broad container intervals; subtracting them
        # would hide otherwise unattributed work. SQL is a measured leaf.
        with patch.object(diagnostic.time, "monotonic_ns", side_effect=[
                0, 10_000_000, 20_000_000, 30_000_000, 80_000_000, 100_000_000]):
            trace = diagnostic._Trace(str(uuid4()), "/api/player/heartbeat", "POST")
            handler_span = trace.begin("HANDLER")
            sql_span = trace.begin("SQL")
            trace.end(sql_span)
            trace.end(handler_span)
            row = trace.finish(200, True, True, False)
        testcase.assertEqual(row["duration_ms"], 100)
        testcase.assertEqual(row["stages"]["SQL"]["total_ms"], 10)
        testcase.assertEqual(row["stages"]["HANDLER"]["total_ms"], 70)
        testcase.assertEqual(row["stages"]["OTHER"]["total_ms"], 90)
        testcase.assertEqual(sum(row["exclusive_breakdown_ms"].values()), 100)
        testcase.assertNotIn("HANDLER", row["exclusive_breakdown_ms"])
        testcase.assertNotIn("TRANSACTION", row["exclusive_breakdown_ms"])
        engine.dispose()
        return {"other_ms": row["stages"]["OTHER"]["total_ms"]}

    if scenario == "database_errors":
        with TestClient(app) as client:
            for action, failing_stage in (("sql_error", "SQL"), ("commit_error", "COMMIT")):
                response = client.post("/api/player/heartbeat", headers={"x-test-action": action})
                testcase.assertEqual(response.status_code, 409, response.text)
                failed = request_events()[-1]
                testcase.assertGreater(failed["stages"][failing_stage]["errors"], 0)
                testcase.assertEqual(engine.pool.checkedout(), 0)
                testcase.assertEqual(failed["process_at_completion"]["pool_acquiring"], 0)
                response = client.post("/api/player/heartbeat")
                testcase.assertEqual(response.status_code, 200)
                recovered = request_events()[-1]
                testcase.assertNotEqual(failed["request_id"], recovered["request_id"])
                testcase.assertEqual(recovered["stages"][failing_stage]["errors"], 0)
                testcase.assertEqual(engine.pool.checkedout(), 0)
        with sessions() as db:
            testcase.assertEqual(db.query(FixtureRow).count(), 1)
        # A real pool timeout is measured around acquisition, including its
        # exception, rather than inferred from a successful checkout callback.
        with engine.connect() as held_one, engine.connect() as held_two:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/player/runtime-config")
                testcase.assertEqual(response.status_code, 500)
            timed_out = request_events()[-1]
            testcase.assertGreater(timed_out["stages"]["POOL_ACQUIRE"]["errors"], 0)
            testcase.assertGreater(timed_out["stages"]["POOL_ACQUIRE"]["total_ms"], 100)
            testcase.assertEqual(timed_out["process_at_completion"]["pool_acquiring"], 0)
            testcase.assertEqual(timed_out["process_at_completion"]["pool_errors"], 1)
        testcase.assertEqual(engine.pool.checkedout(), 0)
        with TestClient(app) as client:
            testcase.assertEqual(client.get("/api/player/runtime-config").status_code, 200)
        testcase.assertEqual(diagnostic._process_snapshot(engine)["inflight"], 0)
        testcase.assertEqual(engine.pool.checkedout(), 0)
        engine.dispose()
        return {"failed_sql_commit_and_acquisition": True, "events": len(request_events())}

    if scenario == "privacy":
        secrets = ["synthetic_header_credential_91", "synthetic_query_credential_92",
                   "synthetic_body_credential_93", "synthetic_cookie_credential_94",
                   "synthetic_private_exception_message", "sql_secret_synthetic",
                   "missing_secret_synthetic", "synthetic_commit_secret_value"]
        with TestClient(app, raise_server_exceptions=False) as client:
            for action, expected_status in (("", 200), ("sql_error", 409),
                                            ("commit_error", 409), ("unexpected_error", 500)):
                response = client.post(
                    "/api/player/heartbeat?player_token=" + secrets[1],
                    headers={"authorization": "Bearer " + secrets[0],
                             "cookie": "private=" + secrets[3], "x-test-action": action},
                    json={"password": secrets[2], "channel_id": "synthetic-channel-private"})
                testcase.assertEqual(response.status_code, expected_status)
        testcase.assertEqual(len(request_events()), 4)
        rendered = json.dumps(captured, sort_keys=True)
        for sensitive in secrets + ["synthetic-channel-private", "authorization", "password",
                                     "cookie", "SELECT", "INSERT", "diagnostic_fixture"]:
            testcase.assertNotIn(sensitive, rendered)
        testcase.assertEqual(engine.pool.checkedout(), 0)
        testcase.assertEqual(diagnostic._process_snapshot(engine)["inflight"], 0)
        engine.dispose()
        return {"secrets_checked": len(secrets), "events": len(request_events())}

    if scenario == "concurrency":
        import anyio.to_thread

        async def exercise_concurrency():
            concurrent_app = FastAPI()
            semaphore = asyncio.Semaphore(1)
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            entered = []

            async def admission():
                ticket = diagnostic.admission_wait_start()
                try:
                    await semaphore.acquire()
                    diagnostic.admission_acquired(ticket)
                    try:
                        yield
                    finally:
                        semaphore.release()
                finally:
                    diagnostic.admission_released(ticket)
                    diagnostic.admission_released(ticket)  # cleanup is idempotent

            @diagnostic.observed("AUTH")
            def thread_sql():
                with sessions() as db:
                    return db.scalar(text("SELECT 1"))

            @concurrent_app.post("/api/player/heartbeat")
            @diagnostic.observed("HANDLER")
            async def concurrent_endpoint(admitted=Depends(admission)):
                entered.append(1)
                if len(entered) == 1:
                    first_entered.set()
                    await release_first.wait()
                testcase.assertEqual(await anyio.to_thread.run_sync(thread_sql), 1)
                return {"unchanged": True}

            diagnostic.install(concurrent_app, engine)
            ids = [str(uuid4()) for _ in range(3)]
            async with AsyncClient(transport=ASGITransport(app=concurrent_app),
                                   base_url="http://synthetic-local") as client:
                tasks = [asyncio.create_task(client.post("/api/player/heartbeat",
                         headers={"x-request-id": identifier})) for identifier in ids]
                try:
                    await asyncio.wait_for(first_entered.wait(), 3)
                    for _ in range(100):
                        snapshot = diagnostic._process_snapshot(engine)
                        if snapshot["admission_waiters"] == 2:
                            break
                        await asyncio.sleep(0.01)
                    testcase.assertEqual(snapshot["inflight"], 3)
                    testcase.assertEqual(snapshot["admission_active"], 1)
                    testcase.assertEqual(snapshot["admission_waiters"], 2)
                    tasks[2].cancel()
                    cancelled = await asyncio.gather(tasks[2], return_exceptions=True)
                    testcase.assertIsInstance(cancelled[0], asyncio.CancelledError)
                finally:
                    release_first.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks[:2]), 3)
                testcase.assertEqual([response.status_code for response in responses], [200, 200])
                testcase.assertEqual([response.json() for response in responses],
                                     [{"unchanged": True}, {"unchanged": True}])
            return ids

        ids = asyncio.run(exercise_concurrency())
        snapshot = diagnostic._process_snapshot(engine)
        for metric in ("inflight", "admission_waiters", "admission_active", "pool_acquiring"):
            testcase.assertEqual(snapshot[metric], 0, metric)
        testcase.assertEqual(snapshot["requests_started"], 3)
        testcase.assertEqual(snapshot["requests_completed"], 3)
        testcase.assertEqual(snapshot["admission_started"], 3)
        testcase.assertEqual(snapshot["admission_acquired"], 2)
        testcase.assertEqual(snapshot["admission_released"], 2)
        testcase.assertEqual(snapshot["admission_cancelled"], 1)
        testcase.assertEqual(snapshot["primary_pool"]["checkedout"], 0)
        events_by_id = {row["request_id"]: row for row in request_events()}
        testcase.assertEqual(set(events_by_id), set(ids))
        for identifier in ids[:2]:
            row = events_by_id[identifier]
            testcase.assertFalse(row["failed"])
            testcase.assertEqual(row["stages"]["AUTH"]["count"], 1)
            testcase.assertGreater(row["stages"]["SQL"]["count"], 0)
            testcase.assertEqual(row["stages"]["ADMISSION"]["errors"], 0)
        testcase.assertTrue(events_by_id[ids[2]]["failed"])
        testcase.assertEqual(events_by_id[ids[2]]["stages"]["ADMISSION"]["errors"], 1)
        testcase.assertEqual(events_by_id[ids[2]]["stages"]["AUTH"]["count"], 0)
        for identifier in ids[:2]:
            milestones = events_by_id[identifier]["milestones"]
            for key in ("ADMISSION_WAIT_START", "ADMISSION_ACQUIRED", "ADMISSION_RELEASED"):
                testcase.assertIn(key, milestones)
            testcase.assertLessEqual(milestones["ADMISSION_WAIT_START"], milestones["ADMISSION_ACQUIRED"])
            testcase.assertLessEqual(milestones["ADMISSION_ACQUIRED"], milestones["ADMISSION_RELEASED"])
            checkpoints = {checkpoint["name"]: checkpoint
                           for checkpoint in events_by_id[identifier]["checkpoints"]}
            for name in ("ADMISSION_WAIT_START", "ADMISSION_ACQUIRED", "ADMISSION_RELEASED"):
                testcase.assertGreater(checkpoints[name]["counters"]["python_thread_count"], 0)
                testcase.assertIn("total_tokens", checkpoints[name]["anyio_limiter"])
                testcase.assertIn("borrowed_tokens", checkpoints[name]["anyio_limiter"])
                testcase.assertIn("tasks_waiting", checkpoints[name]["anyio_limiter"])
        engine.dispose()
        return {"counters": snapshot, "correlated_requests": 3}

    if scenario == "sampler":
        async def sample_without_lifespan():
            # HTTPX ASGITransport does not run application startup/lifespan.
            # The production GREEN server also uses --lifespan off.
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://synthetic-local") as client:
                testcase.assertEqual((await client.get("/api/player/runtime-config")).status_code, 200)
                for _ in range(30):
                    samples = [row for row in captured if row["event"] == "player_process_diagnostics"]
                    if samples:
                        return samples[-1]
                    await asyncio.sleep(0.1)
            raise AssertionError("Process sampler did not emit without lifespan")

        sample = asyncio.run(sample_without_lifespan())
        for key in ("rss_bytes", "thread_count", "python_thread_count", "process_cpu_percent",
                    "sample_interval_ms", "monotonic_ns"):
            testcase.assertIn(key, sample)
            testcase.assertGreaterEqual(sample[key], 0, key)
        testcase.assertGreater(sample["rss_bytes"], 0)
        testcase.assertGreater(sample["thread_count"], 0)
        testcase.assertGreaterEqual(sample["sample_interval_ms"], 900)
        testcase.assertEqual(set(sample["loop_lag_ms"]),
                             {"max", "mean", "samples", "tick_interval_ms",
                              "sample_values_ms", "sample_values_truncated"})
        testcase.assertGreater(sample["loop_lag_ms"]["samples"], 0)
        lag_values = sample["loop_lag_ms"]["sample_values_ms"]
        testcase.assertGreater(len(lag_values), 0)
        testcase.assertLessEqual(len(lag_values), 16)
        testcase.assertTrue(all(isinstance(value, (int, float)) and value >= 0 for value in lag_values))
        testcase.assertEqual(sample["loop_lag_ms"]["sample_values_truncated"],
                             sample["loop_lag_ms"]["samples"] > len(lag_values))
        if not sample["loop_lag_ms"]["sample_values_truncated"]:
            testcase.assertEqual(len(lag_values), sample["loop_lag_ms"]["samples"])
            testcase.assertAlmostEqual(max(lag_values), sample["loop_lag_ms"]["max"], places=2)
            testcase.assertAlmostEqual(sum(lag_values) / len(lag_values),
                                       sample["loop_lag_ms"]["mean"], places=2)
        testcase.assertIn("total_tokens", sample["anyio_limiter"])
        testcase.assertIn("borrowed_tokens", sample["anyio_limiter"])
        testcase.assertIn("tasks_waiting", sample["anyio_limiter"])
        testcase.assertEqual(sample["inflight"], 0)
        testcase.assertEqual(sample["primary_pool"]["checkedout"], 0)
        engine.dispose()
        return {"sample": sample}

    if scenario == "sink_failure":
        class BrokenSink:
            def write(self, value):
                raise io.UnsupportedOperation("synthetic-private-sink-error")

            def flush(self):
                raise io.UnsupportedOperation("synthetic-private-sink-flush-error")

        diagnostic._emit = original_emit
        try:
            with patch.object(diagnostic.sys, "stdout", BrokenSink()):
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.get("/api/player/runtime-config")
                    testcase.assertEqual(response.status_code, 200)
                    testcase.assertEqual(response.json(), {"synthetic": "same-payload",
                                                          "path": "/api/player/runtime-config"})
                    response = client.post("/api/player/heartbeat", headers={
                        "x-test-action": "unexpected_error"})
                    testcase.assertEqual(response.status_code, 500)
        finally:
            diagnostic._emit = capture_emit
        testcase.assertEqual(diagnostic._process_snapshot(engine)["inflight"], 0)
        testcase.assertEqual(engine.pool.checkedout(), 0)
        engine.dispose()
        return {"sink_failure_preserved_http": True}

    if scenario == "trace_bound":
        import itertools

        sequence = itertools.count(0, 1_000_000)
        with patch.object(diagnostic.time, "monotonic_ns", side_effect=lambda: next(sequence)):
            trace = diagnostic._Trace(str(uuid4()), "/api/player/heartbeat", "POST")
            for _ in range(600):
                span = trace.begin("SQL")
                trace.end(span)
            row = trace.finish(200, True, True, False)
        testcase.assertEqual(row["stages"]["SQL"]["count"], 600)
        testcase.assertEqual(row["stages"]["SQL"]["total_ms"], 600)
        testcase.assertEqual(len(row["timeline"]), 512)
        testcase.assertTrue(row["timeline_truncated"])
        testcase.assertEqual(row["stages"]["SQL"]["spans_dropped"], 88)
        testcase.assertAlmostEqual(sum(row["exclusive_breakdown_ms"].values()), row["duration_ms"])
        testcase.assertEqual(trace.active, {})
        engine.dispose()
        return {"spans": 600, "stored": len(row["timeline"])}

    if scenario == "callables":
        sentinel = object()
        failure = RuntimeError("synthetic-private-callable-error")

        def synchronous(argument: str, *, optional: int = 7):
            return sentinel

        async def asynchronous(argument: str, *, optional: int = 7):
            return sentinel

        def raises_original():
            raise failure

        for original in (synchronous, asynchronous):
            wrapped = diagnostic.observed("AUTH")(original)
            testcase.assertEqual(inspect.signature(wrapped), inspect.signature(original))
            testcase.assertEqual(inspect.iscoroutinefunction(wrapped), inspect.iscoroutinefunction(original))
            testcase.assertEqual(wrapped.__name__, original.__name__)
            result = asyncio.run(wrapped("synthetic")) if inspect.iscoroutinefunction(wrapped) else wrapped("synthetic")
            testcase.assertIs(result, sentinel)
        try:
            diagnostic.observed("AUTH")(raises_original)()
        except RuntimeError as caught:
            testcase.assertIs(caught, failure)
        else:
            raise AssertionError("Diagnostic wrapper swallowed the original exception")
        testcase.assertEqual(request_events(), [], "Instrumentation outside selected HTTP requests must be silent")
        engine.dispose()
        return {"callable_contract": True}

    raise AssertionError("Unknown diagnostic scenario: " + scenario)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--diagnostic-worker":
        print(RESULT_MARKER + json.dumps(_worker(sys.argv[2]), sort_keys=True))
    else:
        unittest.main()
