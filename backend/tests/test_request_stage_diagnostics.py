"""Request stages and real heartbeat lock observation; no business behavior changes."""
import asyncio
from contextlib import contextmanager
import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.api.deps import get_current_player_device, get_db
from app.api.routes import player
from app.core import player_admission as admission
from app.core import request_pg_diagnostics as diagnostic
from app.models.entities import Client, PlayerDevice, PlayerHeartbeatInbox
from app.services import heartbeat_acceptance as acceptance
from app.services import heartbeat_inbox
from test_heartbeat_inbox import HeartbeatInboxPostgresTests, _HeartbeatInboxCases
from test_tenant_gate_performance import GateFixture


PATH = "/api/player/runtime-config"


class RequestStageMetadataTests(unittest.IsolatedAsyncioTestCase):
    def app(self, *, error=None, dependency_error=None, direct=False):
        app = FastAPI()

        async def admitted(request: Request):
            async with admission.player_activity(request):
                yield

        def dependency():
            if dependency_error is not None:
                raise dependency_error

        @app.get(PATH, dependencies=[Depends(admitted), Depends(dependency)])
        async def runtime(request: Request):
            diagnostic.runtime_handler_entered(request)
            if direct:
                return JSONResponse({"detail": "synthetic-direct-body"}, status_code=503,
                                    headers={"Retry-After": "7", "X-QA": "preserved"})
            with diagnostic.diagnostic_stage("runtime_db"):
                if error is not None:
                    raise error
            with diagnostic.diagnostic_stage("runtime_builder"):
                return {"runtime_version": 3, "legacy": "same-payload"}

        diagnostic.install(app)
        return app

    async def get(self, app, request_id):
        headers = {"Authorization": "Bearer synthetic-header-never-log"}
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://qa.invalid") as client:
            return await client.get(PATH + "?private=synthetic-query-never-log", headers=headers)

    async def test_runtime_success_has_all_stages_with_unchanged_payload(self):
        identity = str(uuid4())
        events = []
        with patch.object(diagnostic, "_emit", side_effect=lambda event: events.append(dict(event))):
            response = await self.get(self.app(), identity)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"runtime_version": 3, "legacy": "same-payload"})
        self.assertNotIn("x-request-id", response.headers)
        names = [row["event"] for row in events]
        for name in ("runtime_request_start", "runtime_admission_start", "runtime_admission_end",
                     "runtime_db_start", "runtime_db_end", "runtime_builder_start", "runtime_builder_end",
                     "runtime_response_ready",
                     "player_request_result"):
            self.assertEqual(names.count(name), 1, (name, names))
        for component in ("FASTAPI_SERIALIZER", "JSON_RENDER"):
            for name in ("runtime_serialization_start", "runtime_serialization_end"):
                self.assertEqual(sum(row["event"] == name and row.get("component") == component
                                     for row in events), 1)
        self.assertTrue(all(row["request_id"] == identity for row in events))
        self.assertEqual(events[-1]["status_code"], 200)

    async def test_runtime_reasons_are_source_specific_and_responses_are_unchanged(self):
        cases = []
        tenant = HTTPException(503, "same-tenant-detail", headers={"Retry-After": "3"})
        tenant._player_503_reason = "TENANT_GUARD_LOCK_UNAVAILABLE"
        cases.append((tenant, "RUNTIME_TENANT_GUARD_LOCK_UNAVAILABLE"))
        cases.append((admission.overloaded("ADMISSION_QUEUE_FULL"), "RUNTIME_ADMISSION_REJECT"))
        for tables, lock, expected in ((["channels"], "FOR_SHARE", "RUNTIME_CHANNEL_LOCK_UNAVAILABLE"),
                                       (["channels", "clients"], "MULTIPLE", "RUNTIME_DB_ERROR"),
                                       (["channels"], "NONE", "RUNTIME_DB_ERROR")):
            error = HTTPException(503, "same-database-detail")
            error._request_pg_failure = {"pg_backend_pid": 12345, "db_transaction_id": str(uuid4()),
                "stage": "RUNTIME_DB", "sqlstate": "55P03", "exception_class": "DBAPI_ERROR",
                "sql_tables": tables, "lock_mode": lock}
            cases.append((error, expected))
        pool = HTTPException(503, "same-pool-detail")
        diagnostic.capture_failure(pool, source_error=PoolTimeout("synthetic-secret-not-for-logs"))
        cases.append((pool, "RUNTIME_DB_ERROR"))
        timeout = HTTPException(503, "same-timeout-detail")
        timeout.__cause__ = TimeoutError("synthetic-private-timeout-text")
        cases.append((timeout, "RUNTIME_TIMEOUT_OR_CANCEL"))
        cases.append((HTTPException(503, "same-other-detail"), "RUNTIME_OTHER"))
        for error, expected in cases:
            with self.subTest(reason=expected):
                identity, events = str(uuid4()), []
                with patch.object(diagnostic, "_emit", side_effect=lambda event: events.append(dict(event))):
                    response = await self.get(self.app(error=error), identity)
                self.assertEqual(response.status_code, error.status_code)
                self.assertEqual(response.json(), {"detail": error.detail})
                for name, value in (error.headers or {}).items():
                    self.assertEqual(response.headers[name], value)
                rows = [row for row in events if row["event"] == "runtime_http_503"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["runtime_503_reason"], expected)
                self.assertEqual(rows[0]["request_id"], identity)
                self.assertEqual(rows[0]["status_code"], 503)
                self.assertIs(rows[0]["retry_after_present"], bool(error.headers and "Retry-After" in error.headers))
                encoded = json.dumps(events)
                for secret in ("same-tenant-detail", "same-database-detail", "same-pool-detail",
                               "same-timeout-detail", "same-other-detail", "synthetic-secret-not-for-logs",
                               "synthetic-private-timeout-text", "synthetic-header-never-log",
                               "synthetic-query-never-log"):
                    self.assertNotIn(secret, encoded)

    async def test_runtime_dependency_and_direct_response_503_keep_their_original_contract(self):
        for app, expected, detail in (
            (self.app(dependency_error=HTTPException(503, "original-dependency")),
             "RUNTIME_DEPENDENCY_UNAVAILABLE", "original-dependency"),
            (self.app(direct=True), "RUNTIME_OTHER", "synthetic-direct-body"),
        ):
            events = []
            with patch.object(diagnostic, "_emit", side_effect=lambda event: events.append(dict(event))):
                response = await self.get(app, str(uuid4()))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": detail})
            errors = [row for row in events if row["event"] == "runtime_http_503"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["runtime_503_reason"], expected)

    async def test_real_admission_reject_has_paired_events_without_entering_handler(self):
        app, events = self.app(), []
        controls = admission.admissions(app)
        controls.expensive = admission._FairGate(1, {"runtime": 0}, reservations={"runtime": 1})
        async with controls.expensive.admit("runtime"):
            with patch.object(diagnostic, "_emit", side_effect=lambda event: events.append(dict(event))):
                response = await self.get(app, str(uuid4()))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Player operation temporarily unavailable"})
        self.assertEqual(response.headers["Retry-After"], "1")
        names = [row["event"] for row in events]
        self.assertEqual(names.count("runtime_admission_start"), 1)
        self.assertEqual(names.count("runtime_admission_end"), 1)
        self.assertNotIn("runtime_db_start", names)
        error = next(row for row in events if row["event"] == "runtime_http_503")
        self.assertEqual(error["runtime_503_reason"], "RUNTIME_ADMISSION_REJECT")

    async def test_missing_invalid_and_valid_ids_are_single_source_for_all_events(self):
        for supplied in (None, "not-a-valid-id-with-sensitive-text", str(uuid4()).upper()):
            with self.subTest(supplied=supplied is not None):
                events = []
                with patch.object(diagnostic, "_emit", side_effect=lambda event: events.append(dict(event))):
                    response = await self.get(self.app(error=HTTPException(503, "original")), supplied)
                self.assertEqual(response.status_code, 503)
                identities = {row["request_id"] for row in events}
                self.assertEqual(len(identities), 1)
                if supplied is not None and len(supplied) == 36:
                    self.assertEqual(identities, {supplied.lower()})
                self.assertNotIn("not-a-valid-id-with-sensitive-text", json.dumps(events))

    async def test_observer_sink_failure_cannot_change_success_or_503(self):
        for error, expected in ((None, 200), (HTTPException(503, "original-error"), 503)):
            with patch.object(diagnostic.logging, "getLogger") as logger:
                logger.return_value.warning.side_effect = RuntimeError("synthetic-sink-error")
                response = await self.get(self.app(error=error), str(uuid4()))
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json(), {"detail": "original-error"} if error else
                             {"runtime_version": 3, "legacy": "same-payload"})


class HeartbeatGuardStageUnitTests(unittest.TestCase):
    def test_wait_revalidate_observers_preserve_prewrite_transaction_protocol(self):
        timeline, events = [], []

        class Connection:
            def get_isolation_level(self): return "READ COMMITTED"
            def execute(self, *_args, **_kwargs): timeline.append("barrier")

        class DB:
            def connection(self): timeline.append("connection"); return connection
            def rollback(self): timeline.append("rollback")
            def scalar(self, _statement): timeline.append("single_root_wait"); return "synthetic-id"

        def observe(name, **values):
            events.append((name, values)); timeline.append(values["stage"])

        connection, db = Connection(), DB()
        roots = tuple(object() for _ in range(4))
        busy = acceptance._HeartbeatRootBusy(Client, "synthetic-client-target", True)
        with patch.object(acceptance, "diag_event", side_effect=observe), \
                patch.object(acceptance, "_set_lock_budget"), \
                patch.object(acceptance, "_locked_roots", side_effect=(busy, roots)) as lock_roots:
            result = acceptance._postgres_roots(db, ("device",), ("channel", "client", "branch"),
                                                time.monotonic_ns() + 5_000_000_000)
        self.assertIs(result, roots)
        self.assertEqual(lock_roots.call_count, 2)
        self.assertEqual(timeline.count("rollback"), 2)
        self.assertEqual(timeline.count("single_root_wait"), 1)
        stages = [values["stage"] for _, values in events]
        for stage in ("INITIAL_LOCK", "RELEASE_FOR_WAIT", "WAIT_BEGIN", "WAIT_END",
                      "REVALIDATE_BEGIN", "REVALIDATE_END"):
            self.assertIn(stage, stages)
        self.assertLess(timeline.index("rollback"), timeline.index("WAIT_BEGIN"))
        self.assertLess(timeline.index("WAIT_END"), timeline.index("REVALIDATE_BEGIN"))
        for _, values in events:
            self.assertGreaterEqual(values["remaining_budget_ms"], 0)
            self.assertLessEqual(values["remaining_budget_ms"], 5000)


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned isolated PostgreSQL QA required")
class HeartbeatGuardStagePostgresTests(GateFixture):
    make_engine = HeartbeatInboxPostgresTests.make_engine
    payload = _HeartbeatInboxCases.payload

    def app(self):
        app = FastAPI()
        app.include_router(player.router, prefix="/api/player")

        def database():
            with Session(self.engine, autoflush=False) as db:
                yield db

        def authenticated(db=Depends(get_db)):
            return db.get(PlayerDevice, self.ids["A"]["device"])

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_player_device] = authenticated
        app.add_middleware(admission.HeartbeatRecoveryMiddleware, start=lambda: None)
        diagnostic.install(app)
        return app

    def test_real_http_wait_revalidate_and_commit_share_request_and_real_pg_pid(self):
        identity, events, waiting = str(uuid4()), [], threading.Event()
        lock = threading.Lock()

        def collect(row):
            with lock:
                events.append(dict(row))
            if row.get("event") == "heartbeat_guard_stage" and row.get("stage") == "WAIT_BEGIN":
                waiting.set()

        with Session(self.engine) as holder:
            holder.scalar(select(Client.id).where(Client.id == self.ids["A"]["client"]).with_for_update())
            with patch.object(diagnostic, "_emit", side_effect=collect), \
                    patch.object(heartbeat_inbox, "ensure_worker"), TestClient(self.app()) as client, \
                    ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.post, "/api/player/heartbeat", headers={"X-Request-ID": identity},
                                     json=self.payload().model_dump(mode="json"))
                self.assertTrue(waiting.wait(3), "Real heartbeat did not reach the identified row wait")
                self.assertFalse(future.done())
                holder.rollback()
                response = future.result(timeout=5)
        self.assertEqual(response.status_code, 200)
        rows = [row for row in events if row.get("event") == "heartbeat_guard_stage"]
        stages = [row["stage"] for row in rows]
        for stage in ("INITIAL_LOCK", "RELEASE_FOR_WAIT", "WAIT_BEGIN", "WAIT_END",
                      "REVALIDATE_BEGIN", "REVALIDATE_END", "COMMIT"):
            self.assertIn(stage, stages)
        self.assertTrue(all(row["request_id"] == identity for row in rows))
        relevant = [row for row in rows if row["stage"] != "INITIAL_LOCK" or row.get("entity") == "Client"]
        self.assertTrue(all(type(row["pg_backend_pid"]) is int for row in relevant))
        for row in relevant:
            self.assertRegex(row["db_transaction_id"], r"^[0-9a-f-]{36}$")
            self.assertIn("remaining_budget_ms", row)
        pids = {row["pg_backend_pid"] for row in relevant}
        with self.engine.connect() as connection:
            actual = set(connection.scalars(text("SELECT pid FROM pg_stat_activity WHERE pid=ANY(:pids)"),
                                            {"pids": list(pids)}))
            self.assertEqual(actual, pids)
            self.assertEqual(connection.scalar(select(func.count()).select_from(PlayerHeartbeatInbox)), 1)
        encoded = json.dumps(events)
        for value in self.ids["A"].values():
            if type(value) is str:
                self.assertNotIn(value, encoded)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_actual_http_lock_timeout_retains_failure_pid_entity_and_unchanged_503(self):
        identity, events = str(uuid4()), []
        with Session(self.engine) as holder:
            holder.scalar(select(PlayerDevice.id).where(PlayerDevice.id == self.ids["A"]["device"]).with_for_update())
            with patch.object(diagnostic, "_emit", side_effect=lambda row: events.append(dict(row))), \
                    patch.object(heartbeat_inbox, "ensure_worker"), TestClient(self.app()) as client, \
                    self.assertLogs("app.core.player_admission", level="WARNING") as logs:
                response = client.post("/api/player/heartbeat", headers={"X-Request-ID": identity},
                                       json=self.payload().model_dump(mode="json"))
            holder.rollback()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Player operation temporarily unavailable"})
        self.assertNotIn("retry-after", response.headers)
        rows = [json.loads(record.getMessage()) for record in logs.records]
        row = next(row for row in rows if row.get("event") == "heartbeat_http_503")
        self.assertEqual(row["request_id"], identity)
        self.assertEqual(row["heartbeat_503_reason"], "TENANT_GUARD_LOCK_UNAVAILABLE")
        self.assertEqual(row["sqlstate"], "55P03")
        self.assertEqual(row["entity"], "PlayerDevice")
        self.assertEqual(row["stage"], "WAIT_BEGIN")
        self.assertIs(type(row["pg_backend_pid"]), int)
        self.assertRegex(row["target_hmac"], r"^[0-9a-f]{64}$")
        self.assertFalse(any(row.get("event") == "heartbeat_guard_stage" and row.get("stage") == "COMMIT" for row in events))
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(PlayerHeartbeatInbox.id)), None)
        self.assertEqual(self.engine.pool.checkedout(), 0)


if __name__ == "__main__":
    unittest.main()
