"""Response-neutral playback503 observation; synthetic inputs, isolated QA DB."""
import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.core import playback_failure_diagnostics as diagnostics
from app.core import player_admission as admission
from app.core import player_diagnostics
from app.api.deps import get_db
from app.api.routes import player
from app.models.entities import PlayerPlaybackEvent, PlayerPlaybackInbox
from app.services import playback_inbox
from app.services.tenant_mutations import PlayerPlaybackReceiptBusy
from test_tenant_gate_performance import GateFixture


LOGGER = "app.core.playback_failure_diagnostics"
PATH = "/api/player/playback-events"


class Playback503MetadataTests(unittest.IsolatedAsyncioTestCase):
    def app(self, *, error=None, dependency_error=None, direct=False, prefix="/api"):
        app = FastAPI()

        async def admitted(request: Request):
            async with admission.player_activity(request):
                yield

        def dependency():
            if dependency_error is not None:
                raise dependency_error

        @app.post(prefix + "/player/playback-events", dependencies=[Depends(admitted), Depends(dependency)])
        async def playback(request: Request):
            diagnostics.handler_entered(request)
            if direct:
                return JSONResponse({"detail": "synthetic-direct-body"}, status_code=503,
                                    headers={"rEtRy-AfTeR": "7", "X-QA": "retained"})
            if error is not None:
                raise error
            return JSONResponse({"ok": True}, status_code=201)

        app.add_middleware(diagnostics.Playback503DiagnosticsMiddleware, api_prefix=prefix)
        return app

    def assert_metadata(self, record, *, request_id, reason, retry=False):
        event = json.loads(record.getMessage())
        self.assertEqual(set(event), {"event", "request_id", "playback_503_reason", "status_code",
                                     "retry_after_present", "timestamp", "internal_cause",
                                     "pg_backend_pid", "db_transaction_id", "stage", "sqlstate", "exception_class"})
        self.assertEqual(event["event"], "playback_http_503")
        self.assertEqual(event["request_id"], request_id)
        self.assertEqual(event["playback_503_reason"], reason)
        self.assertEqual(event["status_code"], 503)
        self.assertIs(event["retry_after_present"], retry)
        stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLess(abs((datetime.now(UTC) - stamp).total_seconds()), 10)
        self.assertIn(event["internal_cause"], diagnostics._INTERNAL_CAUSES)
        return event

    async def post(self, app, identity):
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://qa.invalid") as client:
            return await client.post(PATH, headers={"X-Request-ID": identity,
                "Authorization": "Bearer synthetic-token-never-log"},
                json={"synthetic": "body-value-never-log"})

    async def test_all_safe_tagged_reasons_are_observed_without_response_changes_or_opt_in(self):
        cases = (
            ("PLAYBACK_INBOX_FULL", "COUNT_OR_BYTE_CAPACITY"),
            ("PLAYBACK_DB_LOCK_UNAVAILABLE", "PARENT_SCOPE_LOCK_RETRY_EXHAUSTED"),
            ("PLAYBACK_DB_ERROR", "SQLALCHEMY_ERROR"),
            ("PLAYBACK_ADMISSION_REJECT", "ADMISSION_QUEUE_FULL"),
            ("PLAYBACK_TIMEOUT_OR_CANCEL", "TIMEOUT"),
            ("PLAYBACK_DEPENDENCY_UNAVAILABLE", "DEPENDENCY_HTTP_503"),
            ("PLAYBACK_OTHER", "UNCLASSIFIED_HTTP_503"),
        )
        for reason, cause in cases:
            with self.subTest(reason=reason):
                identity = str(uuid4())
                error = HTTPException(503, {"sensitive": "exception-body-never-log"},
                                      headers={"X-QA": "preserved", "Retry-After": "9"})
                self.assertIs(diagnostics.tagged_503(error, reason, cause), error)
                with patch.object(player_diagnostics, "ENABLED", False), self.assertLogs(LOGGER, level="WARNING") as logs:
                    response = await self.post(self.app(error=error), identity)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": {"sensitive": "exception-body-never-log"}})
                self.assertEqual(response.headers["Retry-After"], "9")
                self.assertEqual(response.headers["X-QA"], "preserved")
                self.assertNotIn("x-request-id", response.headers)
                self.assertEqual(len(logs.records), 1)
                row = self.assert_metadata(logs.records[0], request_id=identity, reason=reason, retry=True)
                self.assertEqual(row["internal_cause"], cause)
                for secret in ("synthetic-token-never-log", "body-value-never-log", "exception-body-never-log"):
                    self.assertNotIn(secret, logs.records[0].getMessage())

    async def test_fastapi_and_starlette_dependency_503_are_distinct_from_handler_other(self):
        for exception_type in (HTTPException, StarletteHTTPException):
            with self.subTest(type=exception_type.__name__):
                identity = str(uuid4())
                error = exception_type(503, "synthetic-dependency-detail")
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    response = await self.post(self.app(dependency_error=error), identity)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": "synthetic-dependency-detail"})
                self.assertEqual(len(logs.records), 1)
                self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_DEPENDENCY_UNAVAILABLE")
        identity = str(uuid4())
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            response = await self.post(self.app(error=HTTPException(503, "synthetic-handler")), identity)
        self.assertEqual(response.status_code, 503)
        self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_OTHER")

    async def test_response_only_fallback_is_logged_once_without_inventing_a_cause(self):
        identity = str(uuid4())
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            response = await self.post(self.app(direct=True), identity)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "synthetic-direct-body"})
        self.assertEqual(response.headers["Retry-After"], "7")
        self.assertEqual(len(logs.records), 1)
        row = self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_OTHER", retry=True)
        self.assertEqual(row["internal_cause"], "RESPONSE_HTTP_503")

    async def test_edge_uuid_and_existing_trace_uuid_remain_correlated(self):
        for opt_in in (False, True):
            identity = str(uuid4())
            app = self.app(error=HTTPException(503, "synthetic"))
            if opt_in:
                app.add_middleware(player_diagnostics.PlayerDiagnosticsMiddleware)
            with patch.object(player_diagnostics, "ENABLED", opt_in), self.assertLogs(LOGGER, level="WARNING") as logs:
                response = await self.post(app, identity.upper())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(len(logs.records), 1)
            self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_OTHER")

    async def test_unknown_internal_metadata_and_malformed_ids_never_leak(self):
        error = HTTPException(503, "sensitive-detail")
        error._playback_503_reason = "credential-like-reason-do-not-log"
        error._playback_503_internal_cause = "credential-like-cause-do-not-log"
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            response = await self.post(self.app(error=error), "invalid-request-id-do-not-log")
        self.assertEqual(response.status_code, 503)
        row = json.loads(logs.records[0].getMessage())
        self.assertEqual(UUID(row["request_id"]).version, 4)
        self.assertEqual(row["playback_503_reason"], "PLAYBACK_OTHER")
        self.assertNotIn("credential-like", logs.records[0].getMessage())
        self.assertNotIn("invalid-request-id", logs.records[0].getMessage())

    async def test_logging_failure_does_not_replace_the_original_503(self):
        with patch.object(diagnostics.logging, "getLogger") as logger:
            logger.return_value.warning.side_effect = RuntimeError("synthetic-log-sink-failure")
            response = await self.post(self.app(error=HTTPException(503, "original", headers={"Retry-After": "1"})), str(uuid4()))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "original"})
        self.assertEqual(response.headers["Retry-After"], "1")

    async def test_normal_201_and_non503_errors_emit_no_503_event(self):
        for error, status in ((None, 201), (HTTPException(401, "synthetic"), 401),
                              (HTTPException(422, "synthetic"), 422), (RuntimeError("synthetic"), 500)):
            with self.subTest(status=status), patch.object(diagnostics.logging, "getLogger") as logger:
                response = await self.post(self.app(error=error), str(uuid4()))
                self.assertEqual(response.status_code, status)
                logger.assert_not_called()

    async def test_timeout_mapped_by_existing_code_to503_is_identified_without_converting_bare_cancellation(self):
        for cause, internal in ((TimeoutError("synthetic-never-log"), "TIMEOUT"),
                                (asyncio.CancelledError("synthetic-never-log"), "CANCELLED")):
            error = HTTPException(503, "original503")
            error.__cause__ = cause
            identity = str(uuid4())
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                response = await self.post(self.app(error=error), identity)
            self.assertEqual(response.status_code, 503)
            row = self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_TIMEOUT_OR_CANCEL")
            self.assertEqual(row["internal_cause"], internal)

        async def cancelled(scope, receive, send):
            raise asyncio.CancelledError()
        app = diagnostics.Playback503DiagnosticsMiddleware(cancelled)
        with patch.object(diagnostics.logging, "getLogger") as logger:
            with self.assertRaises(asyncio.CancelledError):
                await app({"type": "http", "method": "POST", "path": PATH, "headers": []}, None, None)
            logger.assert_not_called()

    async def test_real_ingress_queue_rejection_happens_before_dependencies_and_is_logged_once(self):
        app = self.app()
        controls = app.state.player_admission = admission.PlayerAdmission()
        gate = controls.ingress
        release = asyncio.Event()
        async def holding():
            async with gate.admit("ingress"):
                await release.wait()
        holders = [asyncio.create_task(holding()) for _ in range(2)]
        await asyncio.sleep(0)
        pending = [asyncio.create_task(holding()) for _ in range(64)]
        await asyncio.sleep(0)
        try:
            self.assertEqual(gate.snapshot()["active"], 2)
            self.assertEqual(gate.snapshot()["waiters"], 64)
            identity = str(uuid4())
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                response = await self.post(app, identity)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "Player operation temporarily unavailable"})
            self.assertEqual(response.headers["Retry-After"], "1")
            self.assertEqual(len(logs.records), 1)
            row = self.assert_metadata(logs.records[0], request_id=identity, reason="PLAYBACK_ADMISSION_REJECT", retry=True)
            self.assertEqual(row["internal_cause"], "ADMISSION_QUEUE_FULL")
        finally:
            for task in pending:
                task.cancel()
            release.set()
            await asyncio.gather(*holders, *pending, return_exceptions=True)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)


class Playback503ActualIngressTests(GateFixture):
    legacy_playback_settlement = False

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(player.router, prefix="/api/player")
        def database():
            with Session(self.engine, autoflush=False) as db:
                yield db
        app.dependency_overrides[get_db] = database
        app.add_middleware(diagnostics.Playback503DiagnosticsMiddleware)
        self.diag_http = self.enterContext(TestClient(app, raise_server_exceptions=False))

    def post(self):
        return self.diag_http.post(PATH, headers={"Authorization": "Bearer " + self.rows["A"]["token"],
            "X-Request-ID": "09b31ad5-e622-418a-bea6-f14e63518736"},
            json={"started_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "status": "completed"})

    def assert_unaccepted(self, response, records, reason, expected_detail):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": expected_detail})
        self.assertNotIn("retry-after", response.headers)
        self.assertEqual(len(records), 1)
        row = json.loads(records[0].getMessage())
        self.assertEqual(row["playback_503_reason"], reason)
        self.assertEqual(row["request_id"], "09b31ad5-e622-418a-bea6-f14e63518736")
        self.assertFalse(row["retry_after_present"])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PlayerPlaybackInbox)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(PlayerPlaybackEvent)), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)
        return row

    def test_real_inbox_capacity_rejection_retains_body_without_false201(self):
        with patch.object(playback_inbox, "MAX_PENDING_COUNT", 0), self.assertLogs(LOGGER, level="WARNING") as logs:
            response = self.post()
        row = self.assert_unaccepted(response, logs.records, "PLAYBACK_INBOX_FULL",
                                     "Playback receipt storage is temporarily unavailable")
        self.assertEqual(row["internal_cause"], "COUNT_OR_BYTE_CAPACITY")

    def test_real_parent_lock_exhaustion_retains_retry_loop_and_response(self):
        with patch.object(playback_inbox, "player_write_guard", side_effect=PlayerPlaybackReceiptBusy()), \
                patch.object(playback_inbox.time, "sleep") as sleep, self.assertLogs(LOGGER, level="WARNING") as logs:
            response = self.post()
        row = self.assert_unaccepted(response, logs.records, "PLAYBACK_DB_LOCK_UNAVAILABLE",
                                     "Player operation temporarily unavailable")
        self.assertEqual(row["internal_cause"], "PARENT_SCOPE_LOCK_RETRY_EXHAUSTED")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], list(playback_inbox._PARENT_LOCK_BACKOFF[1:]))

    def test_real_storage_failure_and_pool_timeout_have_safe_different_internal_codes(self):
        for error, internal in ((OperationalError("synthetic-secret-SQL", {}, RuntimeError("synthetic-never-log")), "SQLALCHEMY_ERROR"),
                                (SQLAlchemyTimeoutError("synthetic-pool-secret"), "SQLALCHEMY_POOL_TIMEOUT")):
            with self.subTest(internal=internal), patch.object(playback_inbox, "player_write_guard", side_effect=error), \
                    self.assertLogs(LOGGER, level="WARNING") as logs:
                response = self.post()
            row = self.assert_unaccepted(response, logs.records, "PLAYBACK_DB_ERROR",
                                         "Playback receipt storage is temporarily unavailable")
            self.assertEqual(row["internal_cause"], internal)
            self.assertNotIn("synthetic-", logs.records[0].getMessage())

    def test_real_storage_sqlstate55p03_is_tagged_only_when_actual_exception_has_it(self):
        orig = RuntimeError("synthetic-do-not-log")
        orig.sqlstate = "55P03"
        error = OperationalError("synthetic-secret-SQL", {}, orig)
        with patch.object(playback_inbox, "player_write_guard", side_effect=error), self.assertLogs(LOGGER, level="WARNING") as logs:
            response = self.post()
        row = self.assert_unaccepted(response, logs.records, "PLAYBACK_DB_LOCK_UNAVAILABLE",
                                     "Playback receipt storage is temporarily unavailable")
        self.assertEqual(row["internal_cause"], "SQLSTATE_55P03")


if __name__ == "__main__":
    unittest.main()
