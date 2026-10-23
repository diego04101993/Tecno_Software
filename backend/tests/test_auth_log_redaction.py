"""Capture real logging formatters/protocol output in memory, never real tokens."""
import asyncio
from contextlib import contextmanager
import importlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use run_screen_rotation_suite.py: isolated memory DB only")

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from uvicorn.logging import AccessFormatter, DefaultFormatter
from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

from app.core import security_logging
import test_auth_revocation_safety as auth_fixture


USER_SENTINEL = "USER_TOKEN_SENTINEL_1H_R1"
PLAYER_SENTINEL = "PLAYER_TOKEN_SENTINEL_1H_R1"
PASSWORD_SENTINEL = "PASSWORD_SENTINEL_1H_R1"
CODE_SENTINEL = "TC-A1B2-C3D4"


class AuthLogRedactionTests(TestCase):
    def setUp(self):
        previous = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, previous)
        # Verify real app wiring without running lifespan/startup/database init.
        with patch("app.core.database.init_db", side_effect=AssertionError("Startup must not run")):
            import app.main
            importlib.reload(app.main)

    @contextmanager
    def capture(self, name, formatter=None):
        logger = logging.getLogger(name)
        previous = logger.level, logger.propagate, logger.handlers[:]
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(formatter or logging.Formatter("%(levelname)s %(message)s"))
        logger.handlers = [handler]; logger.propagate = False; logger.setLevel(logging.DEBUG)
        try:
            yield logger, output
        finally:
            logger.setLevel(previous[0]); logger.propagate = previous[1]; logger.handlers = previous[2]
            handler.close()

    @contextmanager
    def http_fixture(self):
        case = auth_fixture.AuthRevocationSafetyTests("test_valid_login_me_and_existing_claim_contract")
        case.setUpClass()
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()

    def assert_credentials_absent(self, output):
        for sentinel in (USER_SENTINEL, PLAYER_SENTINEL, PASSWORD_SENTINEL, CODE_SENTINEL):
            self.assertNotIn(sentinel, output)

    def test_uvicorn_websocket_accept_and_reject_logs_hide_query_token(self):
        with self.capture("uvicorn.error", DefaultFormatter("%(levelprefix)s %(message)s", use_colors=False)) as (logger, output):
            async def emit():
                for message in ({"type": "websocket.accept"}, {"type": "websocket.close", "code": 4401}):
                    protocol = SimpleNamespace(
                        handshake_started_event=asyncio.Event(), closed_event=asyncio.Event(), logger=logger,
                        scope={"client": ("127.0.0.1", 1234), "path": "/api/realtime/ws/clients/fixture",
                               "query_string": f"token={USER_SENTINEL}&foo=bar".encode()},
                    )
                    await WebSocketProtocol.asgi_send(protocol, message)
            asyncio.run(emit())
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("foo=bar", rendered)
        self.assertIn("accepted", rendered)
        self.assertIn("403", rendered)

    def test_uvicorn_http_access_formatter_preserves_status_and_safe_query(self):
        fmt = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
        with self.capture("uvicorn.access", fmt) as (logger, output):
            path = f"/api/player/activate?access_token={USER_SENTINEL}&player_token={PLAYER_SENTINEL}&channel_code={CODE_SENTINEL}&foo=bar"
            logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "GET", path, "1.1", 401)
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertIn("foo=bar", rendered)
        self.assertIn("HTTP/1.1", rendered)
        self.assertIn("401", rendered)

    def test_encoded_and_duplicate_sensitive_query_keys_are_redacted(self):
        with self.capture("fixture.auth") as (logger, output):
            logger.warning("request %s", f"/ws?%74oken={USER_SENTINEL}&TOKEN={USER_SENTINEL}&authorization=Bearer+{USER_SENTINEL}&foo=bar&empty=")
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertIn("foo=bar&empty=", rendered)

    def test_password_authorization_and_structured_token_values_are_hidden(self):
        with self.capture("fixture.auth") as (logger, output):
            logger.warning("token=%s, password=%r, foo=bar", USER_SENTINEL, PASSWORD_SENTINEL + " with spaces")
            logger.warning("metadata %r", {"issued_player_token": PLAYER_SENTINEL, "authorization": "Bearer " + USER_SENTINEL})
            logger.warning("Authorization: Bearer %s", USER_SENTINEL)
            logger.warning("activation_code=%s", CODE_SENTINEL)
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertNotIn("with spaces", rendered)
        self.assertIn("foo=bar", rendered)

    def test_exception_type_stack_and_unrelated_message_remain_visible(self):
        with self.capture("uvicorn.error") as (logger, output):
            try:
                raise RuntimeError("fixture failure; token=" + USER_SENTINEL)
            except RuntimeError:
                logger.exception("ASGI application failed, request=fixture")
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("Traceback", rendered)
        self.assertIn("fixture failure", rendered)
        self.assertIn("request=fixture", rendered)

    def test_actual_activation_database_error_does_not_log_player_token(self):
        from app.core.database import engine
        from app.models.entities import Channel
        self.assertTrue(engine.hide_parameters)
        with self.http_fixture() as case, self.capture("uvicorn.error") as (logger, output):
            with case.session() as db:
                other = Channel(client_id=case.cid, branch_id=case.bid, name="Collision fixture")
                db.add(other); db.commit(); other_code = other.channel_code
            # Artificial collision exercises the real error path, not entropy.
            with patch.object(auth_fixture.player, "_generate_player_token", return_value="tpw_" + PLAYER_SENTINEL):
                self.assertEqual(case.activate().status_code, 200)
                with TestClient(case.app) as http:
                    try:
                        http.post("/api/player/activate", json={"channel_code": other_code, "hardware_id": "QA", "device_name": "QA", "app_version": "QA"})
                    except IntegrityError:
                        logger.exception("fixture activation DB error")
                    else:
                        self.fail("The forced duplicate token must fail")
            rendered = output.getvalue()
        self.assert_credentials_absent(rendered)
        self.assertIn("IntegrityError", rendered)
        self.assertIn("SQL parameters hidden", rendered)

    def test_real_login_and_invalid_auth_do_not_log_password_or_bearer(self):
        with self.http_fixture() as case, self.capture("httpx") as (_, output):
            response = case.http.post("/api/auth/login", json={"email": "auth-qa@example.com", "password": PASSWORD_SENTINEL})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(case.me(USER_SENTINEL).status_code, 401)
            self.assert_credentials_absent(output.getvalue())

    def test_redaction_install_is_idempotent_and_preserves_non_secret_messages(self):
        factory = logging.getLogRecordFactory()
        security_logging.install_security_log_redaction()
        self.assertIs(logging.getLogRecordFactory(), factory)
        with self.capture("fixture.normal") as (logger, output):
            logger.info("screen=%s count=%d", "fixture", 3)
            self.assertIn("screen=fixture count=3", output.getvalue())


class CaddyLogRedactionTests(TestCase):
    @skipUnless(os.environ.get("SEC1H_CADDY_IMAGE"), "Set SEC1H_CADDY_IMAGE to run isolated local Caddy integration")
    def test_actual_caddy_502_logs_redact_credentials_and_preserve_safe_query(self):
        # Explicit opt-in; never pull an image or connect to a host/production port.
        image = os.environ["SEC1H_CADDY_IMAGE"]
        self.assertEqual(image, "caddy:2.11.4-alpine")
        source = (Path(__file__).resolve().parents[2] / "frontend/Caddyfile.production").read_text(encoding="utf-8")
        config = source.replace("    servers {", "    auto_https off\n    admin off\n    servers {", 1)
        config = config.replace("{$APP_DOMAIN}", "http://127.0.0.1:8080")
        config = config.replace("{$API_DOMAIN}", "http://127.0.0.1:8081")
        config = config.replace("backend:8000", "127.0.0.1:9")
        name = "sec1h-caddy-" + uuid.uuid4().hex[:12]

        def docker(*arguments, check=True):
            result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=30)
            if check:
                self.assertEqual(result.returncode, 0, f"Local Docker command failed: {arguments[:2]}\n{result.stderr}")
            return result

        docker("image", "inspect", image)
        with TemporaryDirectory(prefix="tecno-auth-caddy-") as temporary:
            config_path = Path(temporary) / "Caddyfile"
            config_path.write_text(config, encoding="utf-8")
            mount = f"type=bind,source={temporary},target=/audit,readonly"
            docker("run", "--rm", "--pull=never", "--network=none", "--mount", mount, image,
                   "caddy", "validate", "--config", "/audit/Caddyfile", "--adapter", "caddyfile")
            started = False
            try:
                docker("run", "-d", "--pull=never", "--network=none", "--name", name, "--mount", mount,
                       image, "caddy", "run", "--config", "/audit/Caddyfile", "--adapter", "caddyfile")
                started = True
                self.assertTrue(docker("exec", name, "caddy", "version").stdout.startswith("v2.11.4"))
                for _ in range(30):
                    ready = docker("exec", name, "wget", "-S", "-O", "/dev/null", "http://127.0.0.1:8080/", check=False)
                    if "HTTP/1.1" in ready.stderr:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("Local isolated Caddy did not become ready")

                sensitive = {
                    "token": USER_SENTINEL, "access_token": USER_SENTINEL,
                    "player_token": PLAYER_SENTINEL, "issued_player_token": PLAYER_SENTINEL,
                    "authorization": "Bearer " + USER_SENTINEL, "password": PASSWORD_SENTINEL,
                    "channel_code": CODE_SENTINEL, "activation_code": CODE_SENTINEL,
                }
                query = urlencode({**sensitive, "foo": "bar"}) + "&%74oken=" + USER_SENTINEL
                url = "http://127.0.0.1:8081/api/realtime/ws/clients/fixture?" + query
                response = docker("exec", name, "wget", "-S", "-O", "/dev/null", url, check=False)
                self.assertIn("502 Bad Gateway", response.stderr)
                events = []
                for _ in range(30):
                    logs = docker("logs", name)
                    rendered = logs.stdout + logs.stderr
                    events = [json.loads(line) for line in rendered.splitlines() if line.startswith("{")]
                    events = [event for event in events if event.get("status") == 502 and "request" in event]
                    if events:
                        break
                    time.sleep(0.1)
                self.assertEqual(len(events), 1)
                for sentinel in (USER_SENTINEL, PLAYER_SENTINEL, PASSWORD_SENTINEL, CODE_SENTINEL):
                    self.assertNotIn(sentinel, rendered, "Credential leaked in Caddy logs")
                uri = urlsplit(events[0]["request"]["uri"])
                self.assertEqual(uri.path, "/api/realtime/ws/clients/fixture")
                values = parse_qs(uri.query)
                self.assertEqual(values.pop("foo"), ["bar"])
                self.assertEqual(set(values), set(sensitive))
                for key, entries in values.items():
                    self.assertEqual(entries, ["[REDACTED]"] * (2 if key == "token" else 1))
            finally:
                if started:
                    docker("rm", "-f", name)
