"""R3 credential hardening; isolated memory DB only, never app.main/startup."""
import ast
from contextlib import redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use the isolated suite runner (SQLite memory and AUTO_SEED=false).")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.routes import auth, clients, team, users
from app.core import config, credential_policy
from app.core.credential_policy import validate_new_password
from app.core.security import hash_password, verify_password
from app.core.security_logging import install_security_log_redaction, redact_credentials
from app.models.base import Base
from app.models.entities import Client, User, UserRole
from app.schemas.auth import LoginRequest
from app.schemas.domain import UserCreate, UserUpdate
from app.scripts import create_superadmin
from app.services import seed
from app.services.auth_rate_limit import AuthRateLimiter, AuthRateLimitMiddleware

BACKEND = Path(__file__).resolve().parents[1]
VALID = "fixture frase larga sin recortar"
HISTORICAL_PLACEHOLDER = 'change-this-secret-in-production'
HISTORICAL_PASSWORD = 'ChangeMe123!'


def configuration(**overrides):
    environment = {"DATABASE_URL": "sqlite://", "SECRET_KEY": "0123456789abcdef" * 4,
                   "MEDIA_ROOT": "unused-r3-test-directory"}
    environment.update(overrides)
    environment = {key: value for key, value in environment.items() if value is not None}
    namespace = {"__file__": str(BACKEND / "app/core/config.py")}
    with patch.object(os, "environ", environment):
        exec(compile((BACKEND / "app/core/config.py").read_text(encoding="utf-8-sig"),
                     str(BACKEND / "app/core/config.py"), "exec"), namespace)
    return namespace


class ConfigurationTests(unittest.TestCase):
    def rejected(self, **values):
        namespace = configuration(**values)
        with patch.object(Path, "mkdir") as mkdir:
            with self.assertRaises(RuntimeError) as caught:
                namespace["get_settings"]()
            mkdir.assert_not_called()
        for key in ("SECRET_KEY", "SEED_PASSWORD"):
            value = values.get(key)
            if value and value.strip():
                self.assertNotIn(value, str(caught.exception))
        return str(caught.exception)

    def test_production_is_default_and_seed_is_off(self):
        ns = configuration()
        with patch.object(Path, "mkdir"):
            settings = ns["get_settings"]()
        self.assertEqual(settings.APP_ENV, "production")
        self.assertFalse(settings.AUTO_SEED)

    def test_missing_empty_whitespace_secret_rejected(self):
        for secret in (None, "", " \t\r\n"):
            with self.subTest(case=repr(secret)):
                self.rejected(SECRET_KEY=secret)

    def test_short_secret_rejected(self):
        self.rejected(SECRET_KEY="x" * 31)
        self.rejected(SECRET_KEY="\u00e9" * 15)

    def test_placeholder_rejected(self):
        self.rejected(SECRET_KEY=HISTORICAL_PLACEHOLDER)
        self.rejected(SECRET_KEY="  " + HISTORICAL_PLACEHOLDER.upper() + "  ")

    def test_valid_secret_bytes_preserved(self):
        for secret in ("0123456789abcdef" * 2, "\u00e9" * 16,
                       "  " + "ABCdef0123456789" * 4 + "  "):
            ns = configuration(SECRET_KEY=secret)
            with patch.object(Path, "mkdir"):
                settings = ns["get_settings"]()
            self.assertTrue(settings.SECRET_KEY == secret)

    def test_explicit_development_and_test(self):
        for mode in ("development", "test"):
            ns = configuration(APP_ENV=mode, SECRET_KEY="fixture-short")
            with patch.object(Path, "mkdir"):
                self.assertEqual(ns["get_settings"]().APP_ENV, mode)

    def test_unknown_environment_rejected(self):
        for mode in ("", "dev", "PRODUCTION", "staging"):
            self.rejected(APP_ENV=mode)

    def test_production_seed_rejected_before_any_side_effect(self):
        self.rejected(AUTO_SEED="true", SEED_PASSWORD=VALID)

    def test_development_seed_requires_explicit_valid_credential(self):
        for value in (None, "", "short"):
            self.rejected(APP_ENV="development", AUTO_SEED="true", SEED_PASSWORD=value)
        ns = configuration(APP_ENV="development", AUTO_SEED="true", SEED_PASSWORD=VALID)
        with patch.object(Path, "mkdir"):
            self.assertTrue(ns["get_settings"]().AUTO_SEED)

    def test_invalid_secret_unicode_does_not_escape_in_error(self):
        self.rejected(SECRET_KEY="fixture-secret-" + "\ud800" + "Z" * 32)


class PolicyTests(unittest.TestCase):
    def test_short_new_password_rejected(self):
        with self.assertRaises(ValidationError):
            UserCreate(email="fixture@example.com", full_name="Fixture", password="abcdefghijklmN")

    def test_boundaries_and_exact_value(self):
        for value in ("abcdefghijklmno", "ab" * 64, "  " + VALID + "  ",
                      "\U0001f680" * 15, "\U0001f680" * 128, "palabras sin simbolos obligatorios"):
            self.assertTrue(validate_new_password(value) == value)
            self.assertTrue(UserCreate(email="fixture@example.com", full_name="Fixture", password=value).password == value)
        for value in ("", "a" * 14, "a" * 129, " " * 15, "\t" * 15, "\ud800" * 15):
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    validate_new_password(value)

    def test_defaults_and_common_passwords_rejected(self):
        for value in (HISTORICAL_PASSWORD, "        " + HISTORICAL_PASSWORD + "        ", "passwordpassword", "1234567890123456",
                      "  PASSWORDPASSWORD  "):
            with self.assertRaises(ValueError):
                validate_new_password(value)

    def test_update_omission_null_and_empty_are_distinct(self):
        self.assertNotIn("password", UserUpdate().model_fields_set)
        self.assertIsNone(UserUpdate(password=None).password)
        with self.assertRaises(ValidationError):
            UserUpdate(password="")
        self.assertTrue(UserUpdate(password=VALID).password == VALID)

    def test_legacy_hash_and_login_schema_accept_short_password(self):
        encoded = hash_password("old")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$210000$"))
        self.assertTrue(verify_password("old", encoded))
        self.assertEqual(LoginRequest(email="fixture@example.com", password="old").password, "old")


class CredentialRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy_hash = hash_password("old")

    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False}, hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        self.assertIn(self.engine.url.database, (None, "", ":memory:"))
        Base.metadata.create_all(self.engine)
        with self.session() as db:
            client = Client(name="R3 fixture", slug="r3-fixture")
            admin = User(email="admin-r3@example.com", full_name="R3 admin",
                         role=UserRole.SUPER_ADMIN, password_hash=self.legacy_hash, is_active=True, status="active")
            db.add_all([client, admin]); db.commit()
            self.cid, self.uid = client.id, admin.id
        self.writes = []
        @event.listens_for(self.engine, "before_cursor_execute")
        def capture_write(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                self.writes.append(statement.split()[0])
        app = FastAPI()
        app.state.auth_rate_limiter = AuthRateLimiter()
        app.add_middleware(AuthRateLimitMiddleware)
        @app.middleware("http")
        async def fixture_ip(request, call_next):
            request.scope["client"] = ("198.51.100.37", 12345)
            return await call_next(request)
        for router, prefix in ((auth.router, "auth"), (users.router, "users"),
                               (clients.router, "clients"), (team.router, "team")):
            app.include_router(router, prefix="/api/" + prefix)
        def db_dependency():
            with self.session() as db:
                yield db
        def admin_dependency():
            with self.session() as db:
                yield db.get(User, self.uid)
        app.dependency_overrides[get_db] = db_dependency
        app.dependency_overrides[get_current_user] = admin_dependency
        self.http = TestClient(app, raise_server_exceptions=False)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def session(self):
        return Session(self.engine, autoflush=False)

    def payload(self, role="client_admin", **extra):
        return {"email": "new-r3@example.com", "full_name": "New fixture", "password": VALID,
                "client_id": self.cid, "role": role, **extra}

    def assert_safe_422(self, response, *markers):
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertTrue(detail)
        for error in detail:
            self.assertIsNone(error.get("input"))
            self.assertTrue(all(key in error for key in ("loc", "type", "msg")))
        for marker in markers:
            self.assertNotIn(marker, response.text)
            self.assertNotIn(json.dumps(marker)[1:-1], response.text)
        self.assertFalse(self.writes)

    def test_legacy_login_remains_successful(self):
        response = self.http.post("/api/auth/login", json={"email": "admin-r3@example.com", "password": "old"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

    def test_create_all_routes_valid_and_invalid_no_write(self):
        routes = [("/api/users/", "client_admin"), ("/api/users/", "staff_operator"),
                  (f"/api/clients/{self.cid}/users", "client_admin"), ("/api/team/users", "staff_operator")]
        for i, (path, role) in enumerate(routes):
            self.writes.clear()
            invalid = self.http.post(path, json=self.payload(role, password="short"))
            self.assert_safe_422(invalid, "short")
            response = self.http.post(path, json=self.payload(role, email=f"new{i}@example.com"))
            self.assertEqual(response.status_code, 201)
            self.assertNotIn("password_hash", response.json())
            with self.session() as db:
                stored = db.get(User, response.json()["id"])
                self.assertTrue(verify_password(VALID, stored.password_hash))

    def test_updates_preserve_or_change_only_on_valid_explicit_password(self):
        for index, (create_path, role) in enumerate([
            (f"/api/clients/{self.cid}/users", "client_admin"), ("/api/team/users", "staff_operator")
        ]):
            response = self.http.post(create_path, json=self.payload(role, email=f"edit{index}@example.com"))
            self.assertEqual(response.status_code, 201)
            uid = response.json()["id"]
            path = create_path + "/" + uid
            with self.session() as db:
                before = db.get(User, uid).password_hash
            for payload in ({"full_name": "Renamed"}, {"password": None}):
                self.assertEqual(self.http.patch(path, json=payload).status_code, 200)
                with self.session() as db:
                    self.assertTrue(db.get(User, uid).password_hash == before)
            for password in ("", "short", "z" * 129, "\ud800" * 16):
                self.writes.clear()
                raw = json.dumps({"password": password}).encode()
                rejected = self.http.patch(path, content=raw, headers={"Content-Type": "application/json"})
                self.assert_safe_422(rejected, *([password] if password else []))
                with self.session() as db:
                    self.assertTrue(db.get(User, uid).password_hash == before)
            new = "  updated fixture frase larga  "
            self.assertEqual(self.http.patch(path, json={"password": new}).status_code, 200)
            with self.session() as db:
                stored = db.get(User, uid).password_hash
                self.assertTrue(verify_password(new, stored))
                self.assertFalse(verify_password(new.strip(), stored))

    def test_validation_never_reflects_sensitive_input(self):
        marker = "sensitive-fixture-value-only"
        cases = [
            ("/api/auth/login", {"password": marker}),
            ("/api/users/", {"email": "a@example.com", "password": marker}),
            ("/api/users/", {"email": "a@example.com", "full_name": "A", "password": [marker]}),
            ("/api/auth/login", [{"password": marker, "SECRET_KEY": marker, "token": marker}]),
            ("/api/users/", {"password": marker, "password_hash": marker, "SECRET_KEY": marker}),
        ]
        for path, payload in cases:
            self.writes.clear()
            self.assert_safe_422(self.http.post(path, json=payload), marker)

    def test_surrogate_multi_error_and_malformed_are_serializable(self):
        for path, payload in [
            ("/api/auth/login", {"password": "\ud800"}),
            ("/api/users/", {"email": "bad", "password": "\ud800"}),
            ("/api/users/", {"password": "\ud800", "full_name": 123}),
            ("/api/team/users", {"password": ["\ud800"], "email": "bad"}),
            ("/api/users/", {"email": "a@example.com", "full_name": "\ud800", "password": VALID}),
            ("/api/auth/login", {"email": "admin-r3@example.com", "password": "\ud800"}),
        ]:
            raw = json.dumps(payload).encode()
            self.assert_safe_422(self.http.post(path, content=raw, headers={"Content-Type": "application/json"}), "\ud800")
        response = self.http.post("/api/auth/login", content=b'{"password":"fixture",',
                                  headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("fixture", response.text)
        response.json()


class BootstrapSeedLoggingTests(unittest.TestCase):
    def test_seed_internal_production_guard(self):
        db = MagicMock()
        settings = SimpleNamespace(APP_ENV="production", AUTO_SEED=True, SEED_PASSWORD=VALID)
        with patch.object(seed, "get_settings", return_value=settings):
            with self.assertRaises(RuntimeError):
                seed.seed_initial_data(db)
        db.scalar.assert_not_called()
        db.commit.assert_not_called()

    def test_seed_requires_opt_in_and_preserves_existing_account(self):
        db = MagicMock()
        for mode, enabled in (("development", False), ("test", False), ("unknown", True)):
            with patch.object(seed, "get_settings", return_value=SimpleNamespace(APP_ENV=mode, AUTO_SEED=enabled, SEED_PASSWORD=VALID)):
                with self.assertRaises(RuntimeError):
                    seed.seed_initial_data(db)
        for mode in ("development", "test"):
            db.reset_mock()
            with patch.object(seed, "get_settings", return_value=SimpleNamespace(APP_ENV=mode, AUTO_SEED=True, SEED_PASSWORD=VALID)):
                seed.seed_initial_data(db)  # Existing admin: no writes.
            db.add.assert_not_called()
            db.commit.assert_not_called()

    def test_seed_explicit_fixture_uses_supplied_password(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        settings = SimpleNamespace(APP_ENV="test", AUTO_SEED=True, SEED_PASSWORD=VALID)
        with Session(engine) as db, patch.object(seed, "get_settings", return_value=settings):
            seed.seed_initial_data(db)
            accounts = list(db.scalars(select(User)))
            self.assertEqual(len(accounts), 2)
            self.assertTrue(all(verify_password(VALID, user.password_hash) for user in accounts))
            before = {user.id: user.password_hash for user in accounts}
            seed.seed_initial_data(db)
            self.assertTrue(before == {user.id: user.password_hash for user in db.scalars(select(User))})

    def test_bootstrap_prompt_policy_confirmation_no_echo_no_strip(self):
        value = "  " + VALID + "  "
        output = io.StringIO()
        with patch.object(create_superadmin.sys.stdin, "isatty", return_value=True), \
             patch.object(create_superadmin.sys.stderr, "isatty", return_value=True), \
             patch.object(create_superadmin, "getpass", side_effect=["short", value, "mismatch", value, value]) as prompt, \
             redirect_stdout(output):
            actual = create_superadmin.prompt_password()
        self.assertTrue(actual == value)
        self.assertEqual(prompt.call_count, 5)
        for sensitive in ("short", value, "mismatch"):
            self.assertNotIn(sensitive, output.getvalue())

    def test_bootstrap_rejects_unsafe_tty_before_db(self):
        with patch.object(create_superadmin.sys.stdin, "isatty", return_value=False), \
             patch.object(create_superadmin, "init_db") as init, redirect_stdout(io.StringIO()):
            self.assertEqual(create_superadmin.main(), 1)
        init.assert_not_called()

    def test_bootstrap_getpass_warning_is_not_echo_fallback(self):
        def unsafe(_):
            import warnings
            warnings.warn("fixture-only", create_superadmin.GetPassWarning)
        with patch.object(create_superadmin.sys.stdin, "isatty", return_value=True), \
             patch.object(create_superadmin.sys.stderr, "isatty", return_value=True), \
             patch.object(create_superadmin, "getpass", side_effect=unsafe):
            with self.assertRaises(RuntimeError):
                create_superadmin.prompt_password()

    def test_bootstrap_does_not_overwrite_or_add_another_superadmin(self):
        existing = SimpleNamespace(role=UserRole.SUPER_ADMIN, email="existing@example.com")
        for answers in ([existing], [None, existing]):
            db = MagicMock()
            db.scalar.side_effect = answers
            with patch.object(create_superadmin.sys.stdin, "isatty", return_value=True), \
                 patch.object(create_superadmin.sys.stderr, "isatty", return_value=True), \
                 patch.object(create_superadmin, "prompt_non_empty", side_effect=["new@example.com", "Fixture"]), \
                 patch.object(create_superadmin, "prompt_password", return_value=VALID), \
                 patch.object(create_superadmin, "init_db"), \
                 patch.object(create_superadmin, "SessionLocal", return_value=db), redirect_stdout(io.StringIO()):
                self.assertEqual(create_superadmin.main(), 0)
            db.add.assert_not_called()
            db.commit.assert_not_called()

    def test_bootstrap_creates_one_user_with_exact_valid_password(self):
        db = MagicMock()
        db.scalar.return_value = None
        value = "  " + VALID + "  "
        output = io.StringIO()
        with patch.object(create_superadmin.sys.stdin, "isatty", return_value=True), \
             patch.object(create_superadmin.sys.stderr, "isatty", return_value=True), \
             patch.object(create_superadmin, "prompt_non_empty", side_effect=["new@example.com", "Fixture"]), \
             patch.object(create_superadmin, "getpass", side_effect=[value, value]), \
             patch.object(create_superadmin, "init_db"), \
             patch.object(create_superadmin, "SessionLocal", return_value=db), redirect_stdout(output):
            self.assertEqual(create_superadmin.main(), 0)
        created = db.add.call_args.args[0]
        self.assertTrue(verify_password(value, created.password_hash))
        self.assertFalse(verify_password(value.strip(), created.password_hash))
        db.commit.assert_called_once()
        self.assertNotIn(value, output.getvalue())

    def test_bootstrap_db_failure_is_sanitized(self):
        marker = "fixture-sensitive-bootstrap-error"
        output = io.StringIO()
        with patch.object(create_superadmin.sys.stdin, "isatty", return_value=True), \
             patch.object(create_superadmin.sys.stderr, "isatty", return_value=True), \
             patch.object(create_superadmin, "prompt_non_empty", side_effect=["new@example.com", "Fixture"]), \
             patch.object(create_superadmin, "prompt_password", return_value=VALID), \
             patch.object(create_superadmin, "init_db", side_effect=RuntimeError(marker)), redirect_stdout(output):
            self.assertEqual(create_superadmin.main(), 1)
        self.assertNotIn(marker, output.getvalue())

    def test_secret_and_existing_credentials_are_redacted(self):
        for key in ("SECRET_KEY", "secret_key", "SEED_PASSWORD", "password", "password_hash", "authorization", "access_token"):
            marker = "fixture-sensitive-marker"
            self.assertNotIn(marker, redact_credentials(f"{key}={marker}"))
            self.assertNotIn(marker, redact_credentials(f"/path?{key}={marker}"))


class LogRedactionBoundaryTests(unittest.TestCase):
    KEYS = (
        "password", "password_hash", "SECRET_KEY", "SEED_PASSWORD", "authorization",
        "access_token", "player_token", "token", "issued_player_token", "channel_code",
        "activation_code",
    )

    def test_unquoted_spaces_for_every_sensitive_key(self):
        for key in self.KEYS:
            for separator in ("=", ": "):
                for value in ("fixture first private tail", "fixture\u00a0private\u00a0tail", "Bearer fixture private tail"):
                    with self.subTest(key=key, separator=separator, value=value):
                        self.assertEqual(
                            redact_credentials(f"{key}{separator}{value}"),
                            f"{key}{separator}[REDACTED]",
                        )

    def test_single_word_and_quoted_values_keep_field_boundaries(self):
        for key in self.KEYS:
            for value in ("fixture-only", '"fixture words role=private"', "'fixture words role=private'"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(
                        redact_credentials(f"user=diego {key}={value} role=admin"),
                        f"user=diego {key}=[REDACTED] role=admin",
                    )

    def test_multiple_sensitive_and_normal_fields(self):
        source = (
            "user=diego password=fixture first tail role=admin "
            "SECRET_KEY=fixture second tail status=ok SEED_PASSWORD=fixture third tail"
        )
        expected = (
            "user=diego password=[REDACTED] role=admin "
            "SECRET_KEY=[REDACTED] status=ok SEED_PASSWORD=[REDACTED]"
        )
        self.assertEqual(redact_credentials(source), expected)
        self.assertEqual(redact_credentials(expected), expected)

    def test_normal_fields_with_equals_colons_and_quoted_keys(self):
        for next_field in ("role=admin", "role: admin", '"role": "admin"', "'role': 'admin'",
                           "request.id=42", "request-id=42", "count = 3"):
            with self.subTest(next_field=next_field):
                self.assertEqual(
                    redact_credentials("user=diego password=fixture private tail  " + next_field),
                    "user=diego password=[REDACTED]  " + next_field,
                )

    def test_existing_separators_preserve_unrelated_text(self):
        for separator in (", ", "; ", "&", "}", "]", "\n", "\r\n"):
            with self.subTest(separator=separator):
                self.assertEqual(
                    redact_credentials("password=fixture private tail" + separator + "normal message"),
                    "password=[REDACTED]" + separator + "normal message",
                )

    def test_timestamps_levels_tabs_and_independent_lines(self):
        prefix = "2026-10-27T12:34:56Z INFO user=diego "
        suffix = "\trole=admin\n2026-10-27T12:34:57Z WARNING independent message"
        self.assertEqual(
            redact_credentials(prefix + "password=fixture\tprivate\ttail" + suffix),
            prefix + "password=[REDACTED]" + suffix,
        )
        empty_line = "password=\n2026-10-27T12:34:57Z INFO independent message"
        self.assertEqual(redact_credentials(empty_line), empty_line)

    def test_normal_messages_are_byte_identical(self):
        for value in (
            "2026-10-27T12:34:56Z INFO user=diego role=admin count=3",
            "password policy checked; secret_key_length=32; token_count=2",
            '{"user": "diego", "role": "admin", "message": "normal words"}',
            "/api/contents?foo=bar&count=3",
        ):
            self.assertEqual(redact_credentials(value), value)

    def test_structured_quoted_escaped_and_query_formats(self):
        for source, expected in (
            ('{"password": "fixture \\"private\\" tail", "role": "admin"}',
             '{"password": [REDACTED], "role": "admin"}'),
            ("{'password': 'fixture \\'private\\' tail', 'role': 'admin'}",
             "{'password': [REDACTED], 'role': 'admin'}"),
            ('{"password":\n  "fixture private tail", "role": "admin"}',
             '{"password":\n  [REDACTED], "role": "admin"}'),
            ("/ws?%74oken=fixture+private+tail&foo=bar", "/ws?%74oken=[REDACTED]&foo=bar"),
            ("Authorization: Bearer fixture-token, status=401", "Authorization: [REDACTED], status=401"),
        ):
            self.assertEqual(redact_credentials(source), expected)

    def test_real_logger_formats_redact_complete_values_and_keep_records(self):
        previous_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, previous_factory)
        install_security_log_redaction()
        logger = logging.getLogger("r3.logging.boundary.fixture")
        previous_state = logger.handlers[:], logger.level, logger.propagate
        self.addCleanup(setattr, logger, "handlers", previous_state[0])
        self.addCleanup(logger.setLevel, previous_state[1])
        self.addCleanup(setattr, logger, "propagate", previous_state[2])
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        self.addCleanup(handler.close)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        expected = []
        for key in self.KEYS:
            for formatting in ("%s", "%r"):
                logger.info("user=diego " + key + "=" + formatting + " role=admin", "fixture private tail")
                expected.append("INFO user=diego " + key + "=[REDACTED] role=admin\n")
        logger.warning("independent message count=%d", 3)
        expected.append("WARNING independent message count=3\n")
        self.assertEqual(output.getvalue(), "".join(expected))


# Run via --mutations with the same explicit isolated test environment.
# Only assertion failures count as kills; import errors/skips do not.
MUTATIONS = [
    ("short_secret", "app/core/config.py", "if len(encoded) < 32:", "if False:",
     "ConfigurationTests.test_short_secret_rejected"),
    ("placeholder", "app/core/config.py", "if digest in _SECRET_PLACEHOLDER_DIGESTS:", "if False:",
     "ConfigurationTests.test_placeholder_rejected"),
    ("legacy_login_policy", "app/schemas/auth.py", "    password: str\n",
     '    password: str\n\n    @__import__("pydantic").field_validator("password")\n'
     '    @classmethod\n    def wrong_login_policy(cls, value):\n'
     '        from app.core.credential_policy import validate_new_password\n'
     '        return validate_new_password(value)\n',
     "CredentialRouteTests.test_legacy_login_remains_successful"),
    ("short_password", "app/core/credential_policy.py", "MIN_PASSWORD_LENGTH = 15", "MIN_PASSWORD_LENGTH = 14",
     "PolicyTests.test_short_new_password_rejected"),
    ("password_reflection", "app/schemas/common.py", 'error["input"] = None', "pass",
     "CredentialRouteTests.test_validation_never_reflects_sensitive_input"),
    ("production_seed", "app/services/seed.py",
     'if settings.APP_ENV not in {"development", "test"} or not settings.AUTO_SEED:',
     "if not settings.AUTO_SEED:", "BootstrapSeedLoggingTests.test_seed_internal_production_guard"),
]


def run_mutations():
    child = """
import json, os, sys, unittest
from pathlib import Path
root = Path.cwd()
os.environ.update(APP_ENV='test', DATABASE_URL='sqlite://', TEST_DATABASE_URL='', AUTO_SEED='false',
                  SECRET_KEY='isolated-r3-mutation-fixture-secret', MEDIA_ROOT=str(root/'media'),
                  PUBLIC_BASE_URL='http://testserver')
sys.path[:0] = [str(root), str(root/'tests')]
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(stream=__import__('io').StringIO()).run(suite)
print(json.dumps(dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))))
sys.exit(0 if result.wasSuccessful() else 1)
"""
    results = []
    with TemporaryDirectory(prefix="tecno-r3-mutations-") as temp:
        root = Path(temp).resolve()
        assert not root.is_relative_to(BACKEND)
        for mutation in [None, *MUTATIONS]:
            name = mutation[0] if mutation else "baseline"
            dest = root / name
            for folder in ("app", "tests"):
                shutil.copytree(BACKEND/folder, dest/folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if mutation:
                _, path, old, new, target = mutation
                path = dest/path
                source = path.read_text(encoding="utf-8-sig")
                if source.count(old) != 1:
                    raise RuntimeError("Mutation anchor mismatch: " + name)
                path.write_text(source.replace(old, new, 1), encoding="utf-8")
                targets = [target]
            else:
                targets = [m[4] for m in MUTATIONS]
            done = subprocess.run([sys.executable, "-B", "-c", child,
                                   *["test_credential_policy."+t for t in targets]],
                                  cwd=dest, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                  capture_output=True, text=True, timeout=120)
            try:
                summary = json.loads(done.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                summary = dict(run=0, errors=1, failures=0, skipped=0)
            success = (summary["run"] == len(targets) and not summary["errors"] and not summary["skipped"]
                       and (done.returncode == 1 and summary["failures"] > 0 if mutation
                            else done.returncode == 0 and summary["failures"] == 0))
            result = dict(case=name, passed=success, **summary)
            results.append(result)
            print(json.dumps(result), flush=True)
            if not success:
                break
    return 0 if len(results) == 7 and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    if "--mutations" in sys.argv:
        raise SystemExit(run_mutations())
    unittest.main()
