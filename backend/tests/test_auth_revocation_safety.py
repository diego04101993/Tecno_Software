"""SECURITY-1H-R1 behavior, using real auth and an isolated memory database."""
from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from unittest import TestCase
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use run_screen_rotation_suite.py: isolated memory DB only")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_db
from app.api.routes import auth, player, realtime
from app.core.config import get_settings
from app.core.security import create_access_token, decode_access_token, hash_password
from app.models.base import Base
from app.models.entities import Branch, Channel, Client, PlayerDevice, User, UserRole
from app.services.realtime import ConnectionManager
from app.services.auth_rate_limit import AuthRateLimiter, AuthRateLimitMiddleware


def b64(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed(payload, *, header=None, raw_header=None, raw_payload=None, key=None):
    header_segment = raw_header if raw_header is not None else b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    payload_segment = b64(raw_payload if raw_payload is not None else json.dumps(payload).encode())
    message = f"{header_segment}.{payload_segment}"
    signature = hmac.new((key or get_settings().SECRET_KEY).encode(), message.encode(), hashlib.sha256).digest()
    return message + "." + b64(signature)


class AuthRevocationSafetyTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("isolated-auth-fixture")

    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}, hide_parameters=True)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        self.assertIn(self.engine.url.database, (None, "", ":memory:"))
        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)
        with self.session() as db:
            client = Client(name="Auth fixture", slug="auth-fixture")
            other = Client(name="Other fixture", slug="other-fixture")
            db.add_all([client, other]); db.flush()
            branch = Branch(client_id=client.id, name="A", code="A")
            foreign_branch = Branch(client_id=other.id, name="B", code="B")
            db.add_all([branch, foreign_branch]); db.flush()
            channel = Channel(client_id=client.id, branch_id=branch.id, name="Auth fixture screen")
            foreign_channel = Channel(client_id=other.id, branch_id=foreign_branch.id, name="Other screen")
            user = User(email="auth-qa@example.com", full_name="QA", role=UserRole.CLIENT_ADMIN,
                        client_id=client.id, password_hash=self.password_hash, is_active=True, status="active")
            db.add_all([channel, foreign_channel, user]); db.commit()
            self.uid, self.cid, self.bid, self.chid = user.id, client.id, branch.id, channel.id
            self.other_cid, self.other_chid = other.id, foreign_channel.id
            self.code = channel.channel_code
        self.token = self.user_token()
        app = FastAPI()  # No app.main, startup, seeds or init_db.
        app.state.auth_rate_limiter = AuthRateLimiter()
        app.state.fixture_client_ip = "198.51.100.10"
        app.add_middleware(AuthRateLimitMiddleware)

        @app.middleware("http")
        async def fixture_client_ip(request, call_next):
            # Starlette 0.38 TestClient supplies the non-IP hostname "testclient".
            request.scope["client"] = (app.state.fixture_client_ip, 12345)
            return await call_next(request)

        for router, prefix in ((auth.router, "auth"), (player.router, "player"), (realtime.router, "realtime")):
            app.include_router(router, prefix="/api/" + prefix)

        def isolated_db():
            with self.session() as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        self.app = app
        sessions = patch.object(realtime, "SessionLocal", self.session)
        sessions.start(); self.addCleanup(sessions.stop)
        manager = patch.object(realtime, "manager", ConnectionManager())
        manager.start(); self.addCleanup(manager.stop)
        self.http = TestClient(app, raise_server_exceptions=False)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def session(self):
        return Session(self.engine, autoflush=False)

    def change(self, model, identifier, **fields):
        with self.session() as db:
            obj = db.get(model, identifier)
            for field, value in fields.items():
                setattr(obj, field, value)
            db.commit()

    def user_token(self):
        with self.session() as db:
            user = db.get(User, self.uid)
            return create_access_token(user.id, {"role": user.role.value, "client_id": user.client_id, "branch_id": user.branch_id})

    @staticmethod
    def headers(token):
        return {"Authorization": "Bearer " + token}

    def me(self, token):
        return self.http.get("/api/auth/me", headers=self.headers(token))

    def ws_url(self, token, kind="clients", target=None):
        target = target or (self.cid if kind == "clients" else self.chid)
        return f"/api/realtime/ws/{kind}/{target}?token={token}"

    def assert_ws_rejected(self, token, kind="clients", target=None, code=None):
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.http.websocket_connect(self.ws_url(token, kind, target)):
                pass
        if code is not None:
            self.assertEqual(caught.exception.code, code)

    def activate(self, **overrides):
        payload = {"channel_code": self.code, "hardware_id": "qa-hardware", "device_name": "QA", "app_version": "fixture"}
        payload.update(overrides)
        return self.http.post("/api/player/activate", json=payload)

    def invalid_tokens(self):
        valid = {"sub": self.uid, "exp": int(datetime.now(UTC).timestamp()) + 3600}
        cases = {
            "garbage": "garbage", "truncated": "a.b", "extra_segment": "a.b.c.d",
            "bad_signature_base64": "a.a.a", "empty": "", "empty_segments": "..",
            "invalid_signature": signed(valid, key="different-fixture-key"),
            "none_unsigned": b64(b'{"alg":"none"}') + "." + b64(json.dumps(valid).encode()) + ".",
            "none_signed": signed(valid, header={"alg": "none"}),
            "wrong_algorithm": signed(valid, header={"alg": "HS512"}),
            "missing_algorithm": signed(valid, header={"typ": "JWT"}),
            "header_not_json": signed(valid, raw_header=b64(b"not-json")),
            "header_list": signed(valid, raw_header=b64(b"[]")),
            "header_bad_utf8": signed(valid, raw_header=b64(b"\xff")),
            "payload_not_json": signed(valid, raw_payload=b"not-json"),
            "payload_bad_utf8": signed(valid, raw_payload=b"\xff"),
            "payload_list": signed([]), "payload_null": signed(None),
            "payload_recursive": signed(valid, raw_payload=b"[" * 1200 + b"0" + b"]" * 1200),
            "oversized": "a" * 16385, "undefined": "undefined", "null_literal": "null",
            "double_bearer": "Bearer " + self.token,
        }
        for name, value in (("expired", 1), ("at_now", int(datetime.now(UTC).timestamp())),
                            ("null", None), ("string", "9999999999"), ("bool", True), ("float", 9999999999.0), ("object", {})):
            cases["exp_" + name] = signed({**valid, "exp": value})
        cases["exp_missing"] = signed({"sub": self.uid})
        for name, value in (("empty", ""), ("null", None), ("bool", True), ("object", {}), ("list", [self.uid]),
                            ("integer", 1), ("whitespace", " " + self.uid), ("nul", "bad\x00id"),
                            ("surrogate", "bad\ud800id"), ("unknown", "missing-fixture-user")):
            cases["sub_" + name] = signed({**valid, "sub": value})
        cases["sub_missing"] = signed({"exp": valid["exp"]})
        return cases

    def test_valid_login_me_and_existing_claim_contract(self):
        response = self.http.post("/api/auth/login", json={"email": "auth-qa@example.com", "password": "isolated-auth-fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access_token", "token_type", "user"})
        token = response.json()["access_token"]
        claims = decode_access_token(token)
        self.assertEqual(set(claims), {"sub", "exp", "role", "client_id", "branch_id"})
        self.assertEqual(self.me(token).status_code, 200)
        self.assertAlmostEqual(claims["exp"] - datetime.now(UTC).timestamp(), get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60, delta=2)

    def test_http_negative_token_matrix_has_no_500(self):
        for name, token in self.invalid_tokens().items():
            with self.subTest(case=name):
                response = self.me(token)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertIn(response.json()["detail"], {"Invalid token", "Token expired", "Invalid credentials"})
                self.assertNotIn("Traceback", response.text)

    def test_invalid_signature_encoding_returns_401(self):
        self.assertEqual(self.me("a.a.a").status_code, 401)

    def test_unexpected_auth_errors_are_not_disguised_as_401(self):
        with TestClient(self.app) as http:
            with patch("app.api.deps.decode_access_token", side_effect=RuntimeError("fixture bug")):
                with self.assertRaisesRegex(RuntimeError, "fixture bug"):
                    http.get("/api/auth/me", headers=self.headers(self.token))
        with patch("app.core.security.hmac.new", side_effect=RuntimeError("crypto implementation bug")):
            with self.assertRaisesRegex(RuntimeError, "crypto implementation bug"):
                decode_access_token(self.token)

    def test_ws_valid_active_user_both_rooms(self):
        for kind in ("clients", "channels"):
            with self.subTest(room=kind), self.http.websocket_connect(self.ws_url(self.token, kind)) as ws:
                ws.send_json({"type": "heartbeat", "fixture": True})
                self.assertEqual(ws.receive_json()["type"], "ack" if kind == "clients" else "heartbeat")

    def test_ws_negative_token_matrix_has_no_unhandled_exception(self):
        for kind in ("clients", "channels"):
            for name, token in self.invalid_tokens().items():
                with self.subTest(room=kind, case=name):
                    self.assert_ws_rejected(token, kind, code=4401)

    def test_ws_disabled_user_with_old_token_is_rejected(self):
        self.change(User, self.uid, is_active=False)
        self.assertEqual(self.me(self.token).status_code, 403)
        for kind in ("clients", "channels"):
            self.assert_ws_rejected(self.token, kind, code=4403)
        with self.session() as db:
            self.assertFalse(db.get(User, self.uid).is_active)

    def test_ws_suspended_status_and_client_are_rejected(self):
        for model, identifier, fields, restore in (
            (User, self.uid, {"status": "suspended"}, {"status": "active"}),
            (Client, self.cid, {"status": "suspended"}, {"status": "active"}),
        ):
            self.change(model, identifier, **fields)
            for kind in ("clients", "channels"):
                self.assert_ws_rejected(self.token, kind, code=4403)
            self.change(model, identifier, **restore)

    def test_ws_deleted_user_with_old_token_is_rejected(self):
        with self.session() as db:
            db.delete(db.get(User, self.uid)); db.commit()
        self.assertEqual(self.me(self.token).status_code, 401)
        for kind in ("clients", "channels"):
            self.assert_ws_rejected(self.token, kind, code=4401)

    def test_ws_role_downgrade_uses_current_tenant_and_permissions(self):
        self.change(User, self.uid, role=UserRole.SUPER_ADMIN, client_id=None)
        privileged_token = self.user_token()
        with self.http.websocket_connect(self.ws_url(privileged_token, target=self.other_cid)):
            pass
        self.change(User, self.uid, role=UserRole.CLIENT_ADMIN, client_id=self.cid)
        self.assert_ws_rejected(privileged_token, target=self.other_cid, code=4403)
        self.assert_ws_rejected(privileged_token, "channels", self.other_chid, code=4403)
        with self.http.websocket_connect(self.ws_url(privileged_token)) as ws:
            ws.send_json({"current": True}); self.assertEqual(ws.receive_json()["type"], "ack")
        self.change(User, self.uid, role=UserRole.OPERATOR, branch_id=self.bid)
        self.assert_ws_rejected(privileged_token, code=4403)
        with self.http.websocket_connect(self.ws_url(privileged_token, "channels")):
            pass
        self.change(User, self.uid, branch_id=None)
        self.assert_ws_rejected(privileged_token, "channels", code=4403)

    def test_open_ws_revalidates_disable_delete_and_downgrade_before_messages(self):
        for kind in ("clients", "channels"):
            for operation in ("disable", "delete", "downgrade"):
                with self.subTest(room=kind, operation=operation):
                    # Recreate the fixture identity after the delete variant.
                    with self.session() as db:
                        user = db.get(User, self.uid)
                        if user is None:
                            user = User(id=self.uid, email="auth-qa@example.com", full_name="QA", password_hash=self.password_hash)
                            db.add(user)
                        user.role, user.client_id, user.is_active, user.status = UserRole.SUPER_ADMIN, None, True, "active"
                        db.commit()
                    token = self.user_token()
                    target = self.other_cid if kind == "clients" else self.other_chid
                    with self.http.websocket_connect(self.ws_url(token, kind, target)) as ws:
                        if operation == "delete":
                            with self.session() as db:
                                db.delete(db.get(User, self.uid)); db.commit()
                        elif operation == "disable":
                            self.change(User, self.uid, is_active=False)
                        else:
                            self.change(User, self.uid, role=UserRole.CLIENT_ADMIN, client_id=self.cid)
                        ws.send_json({"type": "heartbeat"})
                        with self.assertRaises(WebSocketDisconnect) as caught:
                            ws.receive_json()
                        self.assertEqual(caught.exception.code, 4401 if operation == "delete" else 4403)

    def test_open_idle_ws_revalidates_without_messages(self):
        with patch.object(realtime, "WS_REVALIDATION_SECONDS", 0.05):
            with self.http.websocket_connect(self.ws_url(self.token)) as ws:
                self.change(User, self.uid, is_active=False)
                with self.assertRaises(WebSocketDisconnect) as caught:
                    ws.receive_json()
                self.assertEqual(caught.exception.code, 4403)
        self.assertFalse(realtime.manager.client_rooms)
        self.assertFalse(realtime.manager.authorizers)

    def test_server_broadcast_revalidates_recipients_without_inbound_message(self):
        for kind in ("clients", "channels"):
            self.change(User, self.uid, is_active=True)
            with self.http.websocket_connect(self.ws_url(self.token, kind)) as ws:
                self.change(User, self.uid, is_active=False)
                broadcast = realtime.manager.broadcast_to_client if kind == "clients" else realtime.manager.broadcast_to_channel
                self.http.portal.call(broadcast, self.cid if kind == "clients" else self.chid, {"must_not_arrive": True})
                with self.assertRaises(WebSocketDisconnect) as caught:
                    ws.receive_json()
                self.assertEqual(caught.exception.code, 4403)

    def test_open_ws_checks_expiration_and_cleans_room(self):
        expires_at = decode_access_token(self.token)["exp"]
        with self.http.websocket_connect(self.ws_url(self.token)) as ws:
            with patch("app.core.security.datetime") as clock:
                clock.now.return_value = datetime.fromtimestamp(expires_at + 1, UTC)
                ws.send_json({"after_expiry": True})
                with self.assertRaises(WebSocketDisconnect) as caught:
                    ws.receive_json()
                self.assertEqual(caught.exception.code, 4401)
        self.assertFalse(realtime.manager.client_rooms)

    def test_unknown_ws_channel_and_wrong_tenant_are_controlled(self):
        self.assert_ws_rejected(self.token, "channels", "missing", code=4404)
        self.assert_ws_rejected(self.token, "channels", self.other_chid, code=4403)
        self.assert_ws_rejected(self.token, target=self.other_cid, code=4403)

    def test_unexpected_ws_db_error_remains_visible(self):
        with patch.object(realtime, "get_current_user", side_effect=RuntimeError("fixture DB bug")):
            with self.assertRaisesRegex(RuntimeError, "fixture DB bug"):
                with self.http.websocket_connect(self.ws_url(self.token)):
                    pass
        with patch.object(realtime, "get_current_user", side_effect=HTTPException(500, "fixture bug")):
            with self.assertRaises(HTTPException):
                realtime._authorize_socket(self.token, self.cid, channel_room=False)

    def test_malformed_ws_messages_close_without_auth_bypass(self):
        with self.http.websocket_connect(self.ws_url(self.token)) as ws:
            ws.send_text("{")
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_json()
            self.assertEqual(caught.exception.code, 1007)
        with self.http.websocket_connect(self.ws_url(self.token, "channels")) as ws:
            ws.send_json([])
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_json()
            self.assertEqual(caught.exception.code, 1003)

    def test_normal_activation_preserves_player_contract_and_token_storage(self):
        activated = self.activate()
        self.assertEqual(activated.status_code, 200, activated.text)
        body = activated.json()
        self.assertEqual(set(body), {"player_token", "channel_id", "client_id", "branch_id", "activation_status"})
        self.assertEqual(body["activation_status"], "activation_new")
        token = body["player_token"]
        self.assertTrue(token.startswith("tpw_")); self.assertEqual(len(token), 47)
        with self.session() as db:
            device = db.scalar(select(PlayerDevice))
            self.assertEqual(device.player_token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(device.metadata_json["issued_player_token"], token)
        headers = self.headers(token)
        runtime = self.http.get("/api/player/runtime-config", headers=headers)
        self.assertEqual(runtime.status_code, 200, runtime.text)
        self.assertEqual(runtime.json()["runtime_version"], 3)
        self.assertEqual(runtime.json()["media_retention"]["schema_version"], 1)
        heartbeat = self.http.post("/api/player/heartbeat", headers=headers, json={"status": "online"})
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(set(heartbeat.json()), {"ok", "received_at", "channel_id"})
        playback = self.http.post("/api/player/playback-events", headers=headers, json={"started_at": datetime.now(UTC).isoformat()})
        self.assertEqual(playback.status_code, 201)

    def test_disabled_player_public_reactivation_rejected_and_unchanged(self):
        token = self.activate().json()["player_token"]
        with self.session() as db:
            device = db.scalar(select(PlayerDevice)); device.is_active = False; db.commit()
            original = deepcopy({column.name: getattr(device, column.name) for column in device.__table__.columns})
            channel = db.get(Channel, self.chid)
            channel_before = {column.name: getattr(channel, column.name) for column in channel.__table__.columns}
        for hardware in ("qa-hardware", "replacement-hardware"):
            response = self.activate(hardware_id=hardware)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"detail": "Player is disabled"})
        with self.session() as db:
            device = db.scalar(select(PlayerDevice))
            self.assertEqual({column.name: getattr(device, column.name) for column in device.__table__.columns}, original)
            channel = db.get(Channel, self.chid)
            self.assertEqual({column.name: getattr(channel, column.name) for column in channel.__table__.columns}, channel_before)
        self.assertEqual(self.http.get("/api/player/runtime-config", headers=self.headers(token)).status_code, 401)
        self.assertEqual(self.http.post("/api/player/heartbeat", headers=self.headers(token), json={}).status_code, 401)

    def test_active_same_hardware_retry_and_hardware_replacement_unchanged(self):
        first = self.activate().json()["player_token"]
        retry = self.activate().json()
        self.assertEqual(retry["player_token"], first)
        self.assertEqual(retry["activation_status"], "activation_existing")
        replacement = self.activate(hardware_id="new-hardware").json()["player_token"]
        self.assertNotEqual(replacement, first)
        self.assertEqual(self.http.get("/api/player/runtime-config", headers=self.headers(first)).status_code, 401)
        self.assertEqual(self.http.get("/api/player/runtime-config", headers=self.headers(replacement)).status_code, 200)
        with self.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PlayerDevice)), 1)

    def test_explicitly_reenabled_fixture_can_activate_again(self):
        token = self.activate().json()["player_token"]
        with self.session() as db:
            identifier = db.scalar(select(PlayerDevice.id))
        self.change(PlayerDevice, identifier, is_active=False)
        self.assertEqual(self.activate().status_code, 403)
        self.change(PlayerDevice, identifier, is_active=True)
        self.assertEqual(self.activate().json()["player_token"], token)

    def test_bad_activation_code_and_malformed_input_are_controlled(self):
        self.assertEqual(self.activate(channel_code="TC-0000-NONE").status_code, 404)
        for field, maximum in (("channel_code", 32), ("hardware_id", 120), ("device_name", 160), ("app_version", 64)):
            for value in (None, True, {}, [], 123, "", "  ", "x" * (maximum + 1)):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assertEqual(self.activate(**{field: value}).status_code, 422)
        for payload in ({}, [], None):
            self.assertEqual(self.http.post("/api/player/activate", json=payload).status_code, 422)
        with self.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PlayerDevice)), 0)
        self.assertEqual(self.activate(channel_code="  " + self.code.lower() + "  ").status_code, 200)

    def test_user_and_player_tokens_cannot_cross_pipelines(self):
        token = self.activate().json()["player_token"]
        self.assertEqual(self.me(token).status_code, 401)
        self.assert_ws_rejected(token, code=4401)
        for candidate in (self.token, "garbage", "a.a.a", "", "undefined", "null", "x" * 16384):
            self.assertEqual(self.http.get("/api/player/runtime-config", headers=self.headers(candidate)).status_code, 401)
            self.assertEqual(self.http.post("/api/player/heartbeat", headers=self.headers(candidate), json={}).status_code, 401)
            self.assertEqual(self.http.post("/api/player/playback-events", headers=self.headers(candidate), json={"started_at": datetime.now(UTC).isoformat()}).status_code, 401)

    def assert_invalid_activation_unicode_rejected(self, field, maximum):
        high, low = chr(0xD800), chr(0xDFFF)
        cases = {
            "high": high, "low": low, "suffix_high": "bad" + high,
            "prefix_high": high + "bad", "suffix_low": "bad" + low,
            "middle_high": "before" + high + "after", "middle_low": "before" + low + "after",
            "overlong": "x" * (maximum + 1) + high,
        }
        statements = []

        def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_sql)
        try:
            for label, value in cases.items():
                with self.subTest(field=field, case=label), \
                        patch.object(player, "_resolve_channel_by_code", wraps=player._resolve_channel_by_code) as lookup, \
                        patch.object(Session, "commit", side_effect=AssertionError("Invalid Unicode reached commit")) as commit:
                    statements.clear()
                    payload = {"channel_code": self.code, "hardware_id": "qa-hardware", "device_name": "QA", "app_version": "fixture", field: value}
                    # Send legal ASCII JSON containing Unicode escapes. Using
                    # httpx's json= would fail in the client before the request.
                    response = self.http.post("/api/player/activate", content=json.dumps(payload, ensure_ascii=True),
                                              headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 422, response.text)
                    details = response.json()["detail"]
                    self.assertEqual(len(details), 1)
                    self.assertEqual(details[0]["loc"], ["body", field])
                    self.assertEqual(details[0]["type"], "value_error")
                    self.assertIn("valid UTF-8", details[0]["msg"])
                    self.assertIsNone(details[0]["input"])
                    self.assertNotIn("Traceback", response.text)
                    lookup.assert_not_called()
                    commit.assert_not_called()
                    self.assertEqual(statements, [], "Invalid Unicode must not execute SELECT/INSERT/UPDATE")
        finally:
            event.remove(self.engine, "before_cursor_execute", record_sql)

    def test_activation_channel_code_invalid_unicode_rejected_before_sql(self):
        self.assert_invalid_activation_unicode_rejected("channel_code", 32)

    def test_activation_hardware_id_invalid_unicode_rejected_before_sql(self):
        self.assert_invalid_activation_unicode_rejected("hardware_id", 120)

    def test_activation_device_name_invalid_unicode_rejected_before_sql(self):
        self.assert_invalid_activation_unicode_rejected("device_name", 160)

    def test_activation_app_version_invalid_unicode_rejected_before_sql(self):
        self.assert_invalid_activation_unicode_rejected("app_version", 64)

    def assert_valid_activation_unicode_preserved(self, field):
        for value in ("M\u00e9xico", "Pantalla \u00d1", "\u00e1\u00e9\u00ed\u00f3\u00fa", "\u6f22\u5b57", "\U0001f600"):
            with self.subTest(field=field, value=value):
                response = self.activate(**{field: value})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["player_token"].startswith("tpw_"))
                with self.session() as db:
                    device = db.scalar(select(PlayerDevice))
                    self.assertEqual(getattr(device, field), value)
                    if field == "hardware_id":
                        self.assertEqual(db.get(Channel, self.chid).hardware_identifier, value)

    def test_activation_valid_unicode_device_name_is_preserved(self):
        self.assert_valid_activation_unicode_preserved("device_name")

    def test_activation_valid_unicode_hardware_id_is_preserved(self):
        self.assert_valid_activation_unicode_preserved("hardware_id")

    def test_activation_valid_unicode_app_version_is_preserved(self):
        self.assert_valid_activation_unicode_preserved("app_version")

    def test_activation_unicode_channel_code_keeps_existing_lookup_rules(self):
        # Valid Unicode is not rejected as ASCII-only. Unknown codes still give
        # 404; a known code retains the existing strip/uppercase lookup behavior.
        self.assertEqual(self.activate(channel_code="M\u00e9xico \u6f22\u5b57 \U0001f600").status_code, 404)
        self.assertEqual(self.activate(channel_code="  " + self.code.lower() + "  ").status_code, 200)
