"""R2B: real routes with isolated SQLite memory fixtures, plus deterministic budgets.

Run through run_screen_rotation_suite.py, or set the same isolated environment.
R2A's single-worker/single-instance precondition remains mandatory. No real DB,
application startup, external HTTP, production credentials or media are used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from itertools import combinations
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use isolated memory DB test configuration")

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import event
from app.api.routes import auth, player
from app.core.config import validate_rate_limit_process_configuration
from app.models.entities import Channel, PlayerDevice, User
from app.schemas.auth import LoginRequest
from app.schemas.domain import PlayerActivationRequest
from app.services import auth_rate_limit as limits
import test_auth_revocation_safety as fixtures


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def request_for(limiter, ip="198.51.100.10", headers=()):
    return Request({"type": "http", "method": "POST", "path": "/api/auth/login",
                    "headers": list(headers), "client": (ip, 12345) if ip else None,
                    "app": SimpleNamespace(state=SimpleNamespace(auth_rate_limiter=limiter))})


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = limits.AuthRateLimiter(clock=self.clock, secret=b"isolated-rate-limit-fixture")

    def failure(self, route="login", ip="198.51.100.10", identifier="qa@example.com"):
        self.limiter.reserve(route, ip, identifier).commit_failure()

    def assert_blocked(self, call, *, scope=None, retry=None):
        with self.assertRaises(limits.RateLimitExceeded) as caught:
            call()
        error = caught.exception
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.detail, limits.DETAIL)
        if scope:
            self.assertEqual(error.scope, scope)
        if retry is not None:
            self.assertEqual(error.headers, {"Retry-After": str(retry)})
        return error

    def check_pair(self, route):
        for _ in range(5):
            self.failure(route)
        self.assert_blocked(lambda: self.failure(route), scope=f"{route}:failure:pair", retry=12)

    def test_login_pair(self):
        self.check_pair("login")

    def test_activation_pair(self):
        self.check_pair("activation")

    def check_identifier(self, route):
        for i in range(10):
            self.failure(route, ip=f"198.51.100.{i}")
        self.assert_blocked(lambda: self.failure(route, ip="203.0.113.99"),
                            scope=f"{route}:failure:" + ("identifier" if route == "login" else "code"),
                            retry=10 if route == "login" else 6)

    def test_login_identifier(self):
        self.check_identifier("login")

    def test_activation_code(self):
        self.check_identifier("activation")

    def check_ip(self, route):
        for i in range(60):
            self.failure(route, identifier=f"id-{i}@example.com")
        self.assert_blocked(lambda: self.failure(route, identifier="new@example.com"),
                            scope=f"{route}:failure:ip", retry=1)
        self.clock.advance(1)
        self.failure(route, identifier="new@example.com")
        self.assert_blocked(lambda: self.failure(route, identifier="next@example.com"), retry=1)

    def test_login_ip(self):
        self.check_ip("login")

    def test_activation_ip(self):
        self.check_ip("activation")

    def test_success_does_not_clear_previous_debt(self):
        for route in ("login", "activation"):
            for _ in range(4):
                self.failure(route)
                self.limiter.reserve(route, "198.51.100.10", "qa@example.com").release()
            self.failure(route)
            self.assert_blocked(lambda: self.failure(route), retry=12)

    def test_rejection_adds_no_debt_and_reserves_nothing(self):
        self.check_pair("login")
        before = {k: (b.debt, b.pending) for k, b in self.limiter._buckets.items()}
        for _ in range(20):
            self.assert_blocked(lambda: self.failure(), retry=12)
        self.assertEqual(before, {k: (b.debt, b.pending) for k, b in self.limiter._buckets.items()})
        self.clock.advance(12)
        self.failure()
        self.assert_blocked(lambda: self.failure(), retry=12)

    def test_retry_after_ceil_and_maximum_of_blocked_budgets(self):
        self.check_pair("login")
        for i in range(5):
            self.failure(ip=f"203.0.113.{i}")
        self.assert_blocked(lambda: self.failure(), retry=12)  # Identifier also needs 10.
        self.clock.advance(0.25)
        self.assert_blocked(lambda: self.failure(), retry=12)
        self.clock.advance(11.75)
        self.failure()

    def test_volume_capacities_refill_and_429_not_penalized(self):
        for route, capacity, interval in (("login", 60, 0.5), ("activation", 100, 0.2)):
            for _ in range(capacity):
                self.limiter.take_volume(route, "198.51.100.10")
            for _ in range(10):
                self.assert_blocked(lambda: self.limiter.take_volume(route, "198.51.100.10"), retry=1)
            self.clock.advance(interval + 0.000001)
            self.limiter.take_volume(route, "198.51.100.10")
            self.assert_blocked(lambda: self.limiter.take_volume(route, "198.51.100.10"), retry=1)

    def test_reservations_release_on_success_error_cancel_and_are_idempotent(self):
        for route in ("login", "activation"):
            for exception in (None, RuntimeError("internal-fixture"), asyncio.CancelledError()):
                if exception is None:
                    with limits.failure_budget(request_for(self.limiter), route, "qa@example.com"):
                        pass
                else:
                    with self.assertRaises(type(exception)) as caught:
                        with limits.failure_budget(request_for(self.limiter), route, "qa@example.com"):
                            raise exception
                    self.assertIs(caught.exception, exception)
                self.assertTrue(all(b.pending == 0 and b.debt == 0 for (ns, _), b in self.limiter._buckets.items()
                                    if ns.startswith(route + ":")))
            reservation = self.limiter.reserve(route, "198.51.100.10", "qa@example.com")
            reservation.commit_failure()
            reservation.release()
            reservation.commit_failure()
            buckets = [b for (ns, _), b in self.limiter._buckets.items() if ns.startswith(route + ":")]
            self.assertTrue(all(b.pending == 0 and b.debt == 1 for b in buckets))
            released = self.limiter.reserve(route, "198.51.100.10", "qa@example.com")
            released.release()
            released.release()
            released.commit_failure()
            self.assertTrue(all(b.pending == 0 and b.debt == 1 for b in buckets))

    def test_failure_status_classification(self):
        for route, codes in (("login", {401, 403}), ("activation", {403, 404})):
            for status in (401, 403, 404, 422, 429, 500):
                limiter = limits.AuthRateLimiter(clock=self.clock)
                with self.assertRaises(HTTPException):
                    with limits.failure_budget(request_for(limiter), route, "qa@example.com"):
                        raise HTTPException(status)
                self.assertTrue(all(b.pending == 0 and b.debt == int(status in codes)
                                    for b in limiter._buckets.values()))

    def check_concurrent(self, route):
        barrier = threading.Barrier(20)
        release = threading.Event()
        attempted = threading.Event()
        mutex = threading.Lock()
        admitted, rejected = [], []
        def operation(i):
            barrier.wait(timeout=10)
            try:
                reservation = self.limiter.reserve(route, "198.51.100.10", "qa@example.com")
            except limits.RateLimitExceeded as exc:
                with mutex:
                    rejected.append(exc.status_code)
                    if len(admitted) + len(rejected) == 20:
                        attempted.set()
                return
            with mutex:
                admitted.append(i)
                if len(admitted) + len(rejected) == 20:
                    attempted.set()
            release.wait(timeout=15)
            reservation.commit_failure()
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(operation, i) for i in range(20)]
            try:
                self.assertTrue(attempted.wait(timeout=12))
                self.assertEqual(len(admitted), 5)
                self.assertEqual(rejected, [429] * 15)
                self.clock.advance(1000)  # Pending slots cannot refill/expire.
                self.assert_blocked(lambda: self.failure(route), retry=1)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=5)
        self.assertTrue(all(b.pending == 0 for b in self.limiter._buckets.values()))

    def test_concurrent_login(self):
        self.check_concurrent("login")

    def test_concurrent_activation(self):
        self.check_concurrent("activation")

    def test_memory_ceiling_fail_closed_no_partial_reservation(self):
        limiter = limits.AuthRateLimiter(clock=self.clock, max_entries=4)
        first = limiter.reserve("login", "198.51.100.1", "one@example.com")
        first.commit_failure()
        self.assert_blocked(lambda: limiter.reserve("login", "198.51.100.1", "two@example.com"), scope="storage")
        self.assertEqual(limiter.entry_count, 3)
        self.assertTrue(all(b.pending == 0 and b.debt == 1 for b in limiter._buckets.values()))

    def test_more_than_20000_identities_remains_bounded(self):
        for i in range(20_000):
            self.limiter.take_volume("login", f"2001:db8:{i:x}::/64")
        before = set(self.limiter._buckets)
        for i in range(20_000, 20_032):
            self.assert_blocked(lambda: self.limiter.take_volume("login", f"2001:db8:{i:x}::/64"), scope="storage")
        self.assertEqual(self.limiter.entry_count, 20_000)
        self.assertEqual(set(self.limiter._buckets), before)
        self.assertTrue(all(b.debt == 1 for b in self.limiter._buckets.values()))

    def test_cleanup_idle_recovered_only_and_preserves_pending_and_debt(self):
        pending = self.limiter.reserve("login", "198.51.100.1", "pending@example.com")
        pending_keys = set(pending._keys)
        self.limiter.take_volume("activation", "198.51.100.2")
        recovered_key = self.limiter._key("activation:volume:ip", "198.51.100.2")
        self.clock.advance(599)
        self.failure("activation", "198.51.100.3", "active")
        debt_keys = {k for k in self.limiter._buckets if k not in pending_keys and k != recovered_key}
        self.clock.advance(2)
        self.limiter.take_volume("login", "198.51.100.4")
        self.assertIn(recovered_key, self.limiter._buckets)  # Not yet next 30s activity scan.
        self.clock.advance(28)
        self.limiter.take_volume("login", "198.51.100.4")
        self.assertNotIn(recovered_key, self.limiter._buckets)
        self.assertTrue(pending_keys <= self.limiter._buckets.keys())
        self.assertTrue(debt_keys <= self.limiter._buckets.keys())  # Still recently used.
        pending.release()
        self.clock.advance(601)
        self.limiter.take_volume("login", "198.51.100.5")
        self.assertEqual(self.limiter.entry_count, 1)
        # Lower idle TTL proves active debt, rather than idle alone, forbids eviction.
        small = limits.AuthRateLimiter(clock=self.clock, max_entries=3, idle_ttl=1, cleanup_interval=1)
        small.reserve("login", "198.51.100.1", "debt").commit_failure()
        self.clock.advance(2)
        small.take_volume("login", "198.51.100.2")  # The recovered IP bucket alone is reclaimable.
        self.assert_blocked(lambda: small.take_volume("login", "198.51.100.3"), scope="storage")
        self.assertTrue(any(b.debt > 0 for b in small._buckets.values()))

    def test_capacity_cleanup_reclaims_eligible_before_rejecting(self):
        small = limits.AuthRateLimiter(clock=self.clock, max_entries=1)
        small.take_volume("login", "198.51.100.1")
        self.clock.advance(601)
        small.take_volume("login", "198.51.100.2")
        self.assertEqual(small.entry_count, 1)

    def test_canonical_ip_and_ipv6_64_policy(self):
        for host in ("2001:db8:abcd:1234::1", "2001:0db8:abcd:1234:0000:0000:0000:0002"):
            self.assertEqual(limits.normalize_ip(host), "2001:db8:abcd:1234::/64")
        self.assertEqual(limits.normalize_ip("::ffff:198.51.100.10"), "198.51.100.10")
        self.assertEqual(limits.normalize_ip("198.51.100.10"), "198.51.100.10")
        for host in (None, "", "testclient", "198.051.100.1", "127.0.0.1:80", "fe80::1%eth0", "unknown"):
            self.assert_blocked(lambda: limits.normalize_ip(host))
        for i in range(5):
            ip = limits.client_ip(request_for(self.limiter, f"2001:db8:abcd:1234::{i}"))
            self.failure(ip=ip)
        self.assert_blocked(lambda: self.failure(ip=limits.normalize_ip("2001:db8:abcd:1234::99")))

    def test_hmac_namespace_privacy_and_ephemeral_restart(self):
        self.failure()
        self.failure("activation")
        self.assertEqual(len({ns for ns, _ in self.limiter._buckets}), 6)
        for namespace, digest in self.limiter._buckets:
            self.assertRegex(digest, r"^[a-f0-9]{64}$")
            self.assertNotIn("qa@example.com", namespace + digest)
            self.assertNotIn("198.51.100.10", namespace + digest)
        fresh = limits.AuthRateLimiter(clock=self.clock)
        other = limits.AuthRateLimiter(clock=self.clock)
        self.assertNotEqual(fresh._secret, other._secret)
        self.assertNotEqual(fresh._key("login:failure:identifier", "qa@example.com"),
                            other._key("login:failure:identifier", "qa@example.com"))
        for _ in range(5):
            fresh.reserve("login", "198.51.100.10", "qa@example.com").commit_failure()

    def test_logs_are_hmac_only_and_frequency_bounded(self):
        self.check_pair("login")
        rejection = self.assert_blocked(lambda: self.failure())
        with self.assertLogs("app.auth_rate_limit", level="WARNING") as captured:
            for _ in range(1000):
                self.limiter.log_rejection("login", rejection)
        self.assertEqual(len(captured.output), 1)
        self.assertRegex(captured.output[0], r"fingerprint=[a-f0-9]{16} retry_after=12")
        for secret in ("qa@example.com", "198.51.100.10", "password", "token"):
            self.assertNotIn(secret, captured.output[0])
        self.clock.advance(10)
        with self.assertLogs("app.auth_rate_limit", level="WARNING") as captured:
            self.limiter.log_rejection("login", rejection)
        self.assertEqual(len(captured.output), 1)

    def test_worker_configuration_guard(self):
        for value in ("2", "0", "garbage", ""):
            with patch.dict(os.environ, WEB_CONCURRENCY=value):
                with self.assertRaisesRegex(RuntimeError, "one backend instance"):
                    validate_rate_limit_process_configuration()
        with patch.dict(os.environ, WEB_CONCURRENCY="1"):
            validate_rate_limit_process_configuration()
        with patch.dict(os.environ):
            os.environ.pop("WEB_CONCURRENCY", None)
            validate_rate_limit_process_configuration()
        # Production's explicit --workers 1 and exact proxy configuration are
        # independently checked by the unchanged nine R2A regression tests.

    def test_state_clock_runs_inside_lock_and_hmac_logging_outside(self):
        observed = []
        def clock():
            observed.append(self.limiter._lock.locked())
            return self.clock()
        self.limiter._clock = clock
        original_hmac = limits.hmac.new
        def hmac_outside(*args, **kwargs):
            self.assertFalse(self.limiter._lock.locked())
            return original_hmac(*args, **kwargs)
        with patch.object(limits.hmac, "new", side_effect=hmac_outside):
            self.failure()
        with patch.object(limits.logger, "warning", side_effect=lambda *args: self.assertFalse(self.limiter._lock.locked())):
            self.limiter.log_rejection("login", limits.RateLimitExceeded())
        self.assertEqual(observed, [True, True, True])


class MiddlewareTests(unittest.TestCase):
    def test_prebody_guard_slash_variants_and_unrelated_routes(self):
        async def run():
            for path, capacity in (("/api/auth/login", 60),):
                limiter = limits.AuthRateLimiter(clock=Clock())
                reached = []
                async def downstream(scope, receive, send):
                    reached.append(scope["path"])
                async def forbidden_receive():
                    self.fail("Middleware must not consume the body")
                messages = []
                async def send(message):
                    messages.append(message)
                middleware = limits.AuthRateLimitMiddleware(downstream)
                scope = dict(request_for(limiter).scope, path=path)
                for i in range(capacity):
                    await middleware(dict(scope, path=path + ("/" if i % 2 else "")), forbidden_receive, send)
                self.assertEqual(len(reached), capacity)
                for suffix in ("", "/", "//"):
                    messages.clear()
                    await middleware(dict(scope, path=path + suffix), forbidden_receive, send)
                    self.assertEqual(len(messages), 2, "Blocked request must produce a response before the route")
                    self.assertEqual(messages[0]["status"], 429)
                    self.assertEqual(json.loads(messages[1]["body"]), {"detail": limits.DETAIL})
                for method, other in (("GET", path), ("OPTIONS", path), ("POST", path + "-other"),
                                      ("POST", "/api/player/heartbeat")):
                    await middleware(dict(scope, method=method, path=other, client=None), forbidden_receive, send)
                self.assertEqual(len(reached), capacity + 4)
        asyncio.run(run())

    def test_activation_bypasses_volume_for_configured_prefixes_and_slashes(self):
        async def run():
            for prefix in ("/api", "/qa/v2/", ""):
                limiter = limits.AuthRateLimiter(clock=Clock())
                reached = []
                async def downstream(scope, receive, send):
                    reached.append(scope["path"])
                async def forbidden(*_):
                    self.fail("Pass-through must not read a body or emit a rejection")
                middleware = limits.AuthRateLimitMiddleware(downstream, api_prefix=prefix)
                base = prefix.rstrip("/")
                self.assertEqual(middleware.paths, {base + "/auth/login": "login"})
                with patch.object(limits, "get_limiter", side_effect=AssertionError("Activation must not access limiter")), \
                     patch.object(limits, "client_ip", side_effect=AssertionError("Activation must not resolve limiter IP")):
                    for i in range(200):
                        path = base + "/player/activate" + ("", "/", "//")[i % 3]
                        scope = dict(request_for(limiter, ip=None).scope, path=path)
                        await middleware(scope, forbidden, forbidden)
                self.assertEqual(len(reached), 200)
                self.assertEqual(limiter.entry_count, 0)
        asyncio.run(run())

    def test_invalid_peer_fails_closed_despite_spoofed_headers(self):
        async def run():
            for origin in (None, "testclient", "unknown"):
                limiter = limits.AuthRateLimiter(clock=Clock())
                async def forbidden(*_):
                    self.fail("Invalid peer must never reach body/route")
                messages = []
                async def send(message):
                    messages.append(message)
                scope = request_for(limiter, origin, [(b"x-forwarded-for", b"198.51.100.1")]).scope
                await limits.AuthRateLimitMiddleware(forbidden)(scope, forbidden, send)
                self.assertEqual(messages[0]["status"], 429)
                self.assertEqual(limiter.entry_count, 0)
        asyncio.run(run())

    def test_main_wiring_and_guard_before_database_startup(self):
        from app import main
        self.assertIn(limits.AuthRateLimitMiddleware, [m.cls for m in main.app.user_middleware])
        async def startup():
            async with main.lifespan(main.app):
                self.fail("Incompatible configuration must not start")
        with patch.dict(os.environ, WEB_CONCURRENCY="2"), patch.object(main, "init_db") as init:
            with self.assertRaises(RuntimeError):
                asyncio.run(startup())
            init.assert_not_called()


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.AuthRevocationSafetyTests.setUpClass()

    def setUp(self):
        self.case = fixtures.AuthRevocationSafetyTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.clock = Clock()
        self.limiter = limits.AuthRateLimiter(clock=self.clock)
        self.case.app.state.auth_rate_limiter = self.limiter
        self.http = self.case.http

    def login(self, email="missing@example.com", password="invalid-fixture", **kwargs):
        return self.http.post("/api/auth/login", json={"email": email, "password": password}, **kwargs)

    def assert_429(self, response, retry=None):
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), {"detail": limits.DETAIL})
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        self.assertFalse(any("ratelimit" in name.lower() for name in response.headers))
        if retry:
            self.assertEqual(response.headers["Retry-After"], str(retry))

    def test_login_five_failures_sixth_rejected_before_lookup(self):
        for _ in range(5):
            response = self.login()
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Invalid email or password"})
        with patch.object(auth, "_login", side_effect=AssertionError("lookup must not run")):
            self.assert_429(self.login(), 12)
        self.assertEqual(self.limiter._buckets[self.limiter._key("login:volume:ip", "198.51.100.10")].debt, 6)

    @contextmanager
    def activation_without_limiter_activity(self):
        before = deepcopy(self.limiter._buckets)
        cleanup, next_log, now = self.limiter._last_cleanup, self.limiter._next_log, self.clock.now
        with ExitStack() as stack:
            guards = []
            for target, names in ((limits, ("get_limiter", "client_ip")),
                                  (self.limiter, ("take_volume", "reserve", "_admit", "_cleanup", "log_rejection"))):
                for name in names:
                    guards.append(stack.enter_context(patch.object(
                        target, name, side_effect=AssertionError("Activation touched limiter: " + name))))
            log = stack.enter_context(patch.object(limits.logger, "warning"))
            yield
            for guard in guards:
                guard.assert_not_called()
            log.assert_not_called()
        self.assertEqual(self.limiter._buckets, before)
        self.assertEqual(self.limiter._last_cleanup, cleanup)
        self.assertEqual(self.limiter._next_log, next_log)
        self.assertEqual(self.clock.now, now, "Limiter clock must stay frozen throughout activation stress")
        self.assertFalse(any(ns.startswith("activation:") for ns, _ in self.limiter._buckets))

    def test_activation_200_repeated_404_without_limiter_state(self):
        with self.activation_without_limiter_activity():
            statuses = [self.case.activate(channel_code="MISSING").status_code for _ in range(200)]
        self.assertEqual(statuses, [404] * 200)
        self.assertEqual(statuses.count(429), 0)
        self.assertEqual(self.limiter.entry_count, 0)

    def test_identifier_distributed_http(self):
        for i in range(10):
            self.case.app.state.fixture_client_ip = f"198.51.100.{i}"
            self.assertEqual(self.login().status_code, 401)
        self.case.app.state.fixture_client_ip = "203.0.113.100"
        self.assert_429(self.login())
        with self.activation_without_limiter_activity():
            for i in range(20):
                self.case.app.state.fixture_client_ip = f"198.51.100.{i}"
                self.assertEqual(self.case.activate(channel_code="MISSING").status_code, 404)

    def test_normalization_matches_lookup_without_alias_rewriting(self):
        for email in ("missing@example.com", "MISSING@example.com", "Missing@example.com", "missing@EXAMPLE.COM", "MISSING@EXAMPLE.COM"):
            self.assertEqual(self.login(email).status_code, 401)
        self.assert_429(self.login("missing@example.com"))
        self.assertEqual(self.login("missing+tag@example.com").status_code, 401)
        self.assertEqual(self.login("mis.sing@example.com").status_code, 401)
        for code in ("missing", " MISSING ", "Missing", "\tmissing\n", "MISSING"):
            self.assertEqual(self.case.activate(channel_code=code).status_code, 404)
        self.assertEqual(self.case.activate(channel_code=" MISSING ").status_code, 404)

    def test_pre_threshold_auth_responses_and_disabled_debt(self):
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login("auth-qa@example.com").status_code, 401)
        self.case.change(User, self.case.uid, is_active=False)
        self.assertEqual(self.login("auth-qa@example.com").status_code, 401)
        for _ in range(3):
            self.assertEqual(self.login("auth-qa@example.com", "isolated-auth-fixture").status_code, 403)
        self.assert_429(self.login("auth-qa@example.com", "isolated-auth-fixture"))
        self.assertEqual(self.case.activate().status_code, 200)
        with self.case.session() as db:
            device = db.query(PlayerDevice).filter_by(channel_id=self.case.chid).one()
            device.is_active = False
            db.commit()
        with self.activation_without_limiter_activity():
            for _ in range(200):
                response = self.case.activate()
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"detail": "Player is disabled"})

    def test_25_valid_players_same_nat_and_repeat_token(self):
        codes = []
        with self.case.session() as db:
            for i in range(25):
                channel = Channel(client_id=self.case.cid, branch_id=self.case.bid, name=f"Isolated NAT {i}")
                db.add(channel)
                db.flush()
                codes.append(channel.channel_code)
            db.commit()
        tokens = []
        for i, code in enumerate(codes):
            response = self.case.activate(channel_code=code, hardware_id=f"isolated-nat-{i}")
            self.assertEqual(response.status_code, 200, response.text)
            tokens.append(response.json()["player_token"])
        self.assertEqual(len(tokens), 25)
        for _ in range(10):
            repeated = self.case.activate(channel_code=codes[0], hardware_id="isolated-nat-0")
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json()["player_token"], tokens[0])
        self.assertEqual(self.limiter.entry_count, 0)

    def test_activation_200_valid_requests_reuse_token_without_limiter_state(self):
        tokens, statuses = [], []
        with self.activation_without_limiter_activity():
            for i in range(200):
                response = self.case.activate()
                statuses.append(response.status_code)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(set(data), {"player_token", "channel_id", "client_id", "branch_id", "activation_status"})
                self.assertEqual(data["activation_status"], "activation_new" if i == 0 else "activation_existing")
                self.assertEqual((data["channel_id"], data["client_id"], data["branch_id"]),
                                 (self.case.chid, self.case.cid, self.case.bid))
                tokens.append(data["player_token"])
        self.assertEqual(statuses.count(429), 0)
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(self.limiter.entry_count, 0)
        with self.case.session() as db:
            device = db.query(PlayerDevice).filter_by(channel_id=self.case.chid).one()
            self.assertEqual(device.player_token_hash, player._hash_player_token(tokens[0]))
            self.assertEqual(device.metadata_json["activation_count"], 200, "Historical business counter stays intact")

    def test_saturated_login_and_full_storage_do_not_affect_activation(self):
        # Saturate each independent login admission path, including shared storage.
        for saturation in ("failure", "volume", "storage"):
            with self.subTest(saturation=saturation):
                self.limiter = limits.AuthRateLimiter(clock=self.clock, max_entries=1 if saturation == "storage" else 20_000)
                self.case.app.state.auth_rate_limiter = self.limiter
                if saturation == "failure":
                    for _ in range(5):
                        self.assertEqual(self.login().status_code, 401)
                elif saturation == "volume":
                    for _ in range(60):
                        self.assertEqual(self.http.post("/api/auth/login", json={}).status_code, 422)
                self.assert_429(self.login())
                with self.activation_without_limiter_activity():
                    self.assertEqual(self.case.activate().status_code, 200)
                    self.assertEqual(self.case.activate(channel_code="MISSING").status_code, 404)
                    self.assertEqual(self.http.post("/api/player/activate", json={}).status_code, 422)
                self.assert_429(self.login())

    def test_activation_custom_prefix_and_trailing_slash_real_routes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        for prefix in ("/api", "/qa/v2"):
            app = FastAPI()
            app.state.auth_rate_limiter = self.limiter
            app.add_middleware(limits.AuthRateLimitMiddleware, api_prefix=prefix + "/")
            app.include_router(player.router, prefix=prefix + "/player")
            app.dependency_overrides.update(self.case.app.dependency_overrides)
            # TestClient's non-IP peer also proves activation does not invoke the IP guard.
            with TestClient(app) as http, self.activation_without_limiter_activity():
                for suffix in ("", "/"):
                    response = http.post(prefix + "/player/activate" + suffix, json=self.activation_payload())
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual([r.status_code for r in response.history], [307] if suffix else [])

    def test_successes_count_volume_but_preserve_previous_failures(self):
        for _ in range(4):
            self.assertEqual(self.login("auth-qa@example.com").status_code, 401)
            self.assertEqual(self.login("auth-qa@example.com", "isolated-auth-fixture").status_code, 200)
        self.assertEqual(self.login("auth-qa@example.com").status_code, 401)
        self.assert_429(self.login("auth-qa@example.com", "isolated-auth-fixture"))
        key = self.limiter._key("login:volume:ip", "198.51.100.10")
        self.assertEqual(self.limiter._buckets[key].debt, 10)

    def test_login_bad_json_schema_volume_only(self):
        cases = (("/api/auth/login", "{", 60), ("/api/auth/login", "{}", 60))
        for path, content, capacity in cases:
            self.limiter = limits.AuthRateLimiter(clock=self.clock)
            self.case.app.state.auth_rate_limiter = self.limiter
            with patch.object(auth, "_login", side_effect=AssertionError("No schema bypass")), \
                 patch.object(player, "_activate_player", side_effect=AssertionError("No schema bypass")):
                for _ in range(capacity):
                    response = self.http.post(path, content=content, headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 422, response.text)
                self.assert_429(self.http.post(path, content=content, headers={"Content-Type": "application/json"}))
            self.assertEqual(self.limiter.entry_count, 1)
            self.clock.advance(0.51)
            self.assertEqual(self.http.post(path, content=content, headers={"Content-Type": "application/json"}).status_code, 422)

    def test_activation_200_repeated_422_per_invalid_payload_without_limiter(self):
        invalid = self.activation_payload()
        invalid["device_name"] = "bad\ud800"
        del invalid["app_version"]
        for content in ("{", "{}", json.dumps(invalid)):
            with self.subTest(content=content), self.activation_without_limiter_activity(), \
                 patch.object(player, "_activate_player", side_effect=AssertionError("No schema bypass")):
                statuses = []
                for _ in range(200):
                    response = self.http.post("/api/player/activate", content=content,
                                              headers={"Content-Type": "application/json"})
                    statuses.append(response.status_code)
                    self.assertEqual(response.status_code, 422, response.text)
                    json.dumps(response.json(), ensure_ascii=False).encode("utf-8", errors="strict")
                self.assertEqual(statuses.count(429), 0)
            self.assertEqual(self.limiter.entry_count, 0)

    def assert_activation_errors_serializable(self, payload, expected_errors):
        original = deepcopy(payload)
        statements = []
        def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(self.case.engine, "before_cursor_execute", record_sql)
        try:
            with patch.object(player, "_activate_player", side_effect=AssertionError("Invalid input reached activation")) as activate:
                response = self.http.post("/api/player/activate", content=json.dumps(payload, ensure_ascii=True),
                                          headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers["content-type"], "application/json")
                decoded = response.content.decode("utf-8", errors="strict")
                result = json.loads(decoded)
                # Escaping a surrogate in JSON is not enough: the parsed error
                # must itself remain serializable as strict UTF-8 with no input.
                json.dumps(result, ensure_ascii=False).encode("utf-8", errors="strict")
                errors = result["detail"]
                self.assertEqual({tuple(error["loc"]): error["type"] for error in errors},
                                 {("body", field): kind for field, kind in expected_errors.items()})
                self.assertEqual(len(errors), len(expected_errors))
                for error in errors:
                    self.assertIsNone(error["input"])
                    self.assertTrue(error["msg"])
                    if error["type"] == "value_error":
                        self.assertIn("valid UTF-8", error["msg"])
                self.assertNotIn(self.case.code, decoded)
                self.assertNotIn("sensitive-fixture", decoded)
                self.assertNotIn("UnicodeEncodeError", decoded)
                self.assertNotIn("Traceback", decoded)
                activate.assert_not_called()
            self.assertEqual(statements, [])
        finally:
            event.remove(self.case.engine, "before_cursor_execute", record_sql)
        self.assertEqual(payload, original, "Reject the original value; do not repair or mutate it")
        self.assertEqual(self.limiter.entry_count, 0, "Invalid activation must not consume any limiter state")

    def activation_payload(self):
        return {"channel_code": self.case.code, "hardware_id": "sensitive-fixture-hardware",
                "device_name": "sensitive-fixture-device", "app_version": "sensitive-fixture-version"}

    def test_activation_surrogate_plus_missing_required(self):
        fields = tuple(self.activation_payload())
        for invalid_field in fields:
            for missing_field in fields:
                if invalid_field == missing_field:
                    continue
                for surrogate in ("\ud800", "\udfff"):
                    with self.subTest(invalid=invalid_field, missing=missing_field, codepoint=ord(surrogate)):
                        payload = self.activation_payload()
                        payload[invalid_field] = "sensitive-fixture" + surrogate
                        del payload[missing_field]
                        self.assert_activation_errors_serializable(payload, {invalid_field: "value_error", missing_field: "missing"})

    def test_activation_surrogate_plus_invalid_type(self):
        fields = tuple(self.activation_payload())
        invalid_types = (123, True, None, ["sensitive-fixture\ud800"], {"sensitive-fixture\udfff": "value"})
        for invalid_field in fields:
            for type_field in fields:
                if invalid_field == type_field:
                    continue
                for value in invalid_types:
                    with self.subTest(invalid=invalid_field, wrong_type=type_field, kind=type(value).__name__):
                        payload = self.activation_payload()
                        payload[invalid_field] = "sensitive-fixture\ud800"
                        payload[type_field] = value
                        self.assert_activation_errors_serializable(payload, {invalid_field: "value_error", type_field: "string_type"})

    def test_activation_multiple_surrogates(self):
        for first, second in combinations(self.activation_payload(), 2):
            with self.subTest(first=first, second=second):
                payload = self.activation_payload()
                payload[first] = "sensitive-fixture\ud800"
                payload[second] = "sensitive-fixture\udfff"
                self.assert_activation_errors_serializable(payload, {first: "value_error", second: "value_error"})

    def test_activation_surrogate_missing_and_invalid_type_together(self):
        fields = tuple(self.activation_payload())
        for i, invalid_field in enumerate(fields):
            missing_field, type_field = fields[(i + 1) % 4], fields[(i + 2) % 4]
            with self.subTest(invalid=invalid_field, missing=missing_field, wrong_type=type_field):
                payload = self.activation_payload()
                payload[invalid_field] = "sensitive-fixture\ud800"
                del payload[missing_field]
                payload[type_field] = ["sensitive-fixture\udfff"]
                self.assert_activation_errors_serializable(payload, {
                    invalid_field: "value_error", missing_field: "missing", type_field: "string_type"})

    def test_activation_nested_invalid_unicode_in_type_errors(self):
        for field in self.activation_payload():
            for value in (["sensitive-fixture\ud800"], {"sensitive-fixture\udfff": "value"}):
                with self.subTest(field=field, kind=type(value).__name__):
                    payload = self.activation_payload()
                    payload[field] = value
                    self.assert_activation_errors_serializable(payload, {field: "string_type"})

    def test_activation_schema_multi_error_inputs_are_omitted_without_mutation(self):
        payload = self.activation_payload()
        payload["device_name"] = "sensitive-fixture\ud800"
        del payload["app_version"]
        original = deepcopy(payload)
        with self.assertRaises(ValidationError) as caught:
            PlayerActivationRequest.model_validate(payload)
        errors = caught.exception.errors(include_context=False, include_url=False)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(error["input"] is None for error in errors))
        json.dumps(errors, ensure_ascii=False).encode("utf-8", errors="strict")
        self.assertEqual(payload, original)

    def test_forwarded_headers_cannot_change_key(self):
        for route in ("login", "activation"):
            for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
                self.case.app.state.auth_rate_limiter = limits.AuthRateLimiter(clock=self.clock)
                for i in range(6):
                    value = f"203.0.113.{i}" if header != "Forwarded" else f"for=203.0.113.{i}"
                    response = (self.login(headers={header: value}) if route == "login" else
                                self.http.post("/api/player/activate", json={"channel_code": "MISSING", "hardware_id": "qa", "device_name": "QA", "app_version": "fixture"}, headers={header: value}))
                    if i < 5 or route == "activation":
                        self.assertEqual(response.status_code, 401 if route == "login" else 404)
                    else:
                        self.assert_429(response)

    def test_internal_route_errors_are_preserved_without_leaks(self):
        for route, helper, payload in ((auth.login, "app.api.routes.auth._login", LoginRequest(email="qa@example.com", password="fixture")),
                                       (player.activate_player, "app.api.routes.player._activate_player", PlayerActivationRequest(channel_code="QA", hardware_id="fixture", device_name="QA", app_version="fixture"))):
            for error in (RuntimeError("isolated-error"), asyncio.CancelledError()):
                def fail_outside_lock(*_):
                    self.assertFalse(self.limiter._lock.locked())
                    raise error
                with patch(helper, side_effect=fail_outside_lock):
                    with self.assertRaises(type(error)) as caught:
                        route(payload=payload, request=request_for(self.limiter), db=None)
                self.assertIs(caught.exception, error)
                self.assertTrue(all(b.debt == 0 and b.pending == 0 for b in self.limiter._buckets.values()))

    def test_concurrent_routes_reserve_before_business_operation(self):
        for route, helper, payload in ((auth.login, "app.api.routes.auth._login", LoginRequest(email="qa@example.com", password="fixture")),
                                       (player.activate_player, "app.api.routes.player._activate_player", PlayerActivationRequest(channel_code="QA", hardware_id="fixture", device_name="QA", app_version="fixture"))):
            start = threading.Barrier(20)
            all_attempted = threading.Event()
            release = threading.Event()
            mutex = threading.Lock()
            admitted, rejected = [], []
            def business(*_):
                with mutex:
                    admitted.append(True)
                    if len(admitted) + len(rejected) == 20:
                        all_attempted.set()
                release.wait(timeout=15)
                raise HTTPException(401 if route is auth.login else 404)
            def invoke():
                start.wait(timeout=10)
                try:
                    route(payload=payload, request=request_for(self.limiter), db=None)
                except (HTTPException, limits.RateLimitExceeded) as exc:
                    if exc.status_code == 429:
                        with mutex:
                            rejected.append(429)
                            if len(admitted) + len(rejected) == 20:
                                all_attempted.set()
                    else:
                        self.assertIn(exc.status_code, (401, 404))
            with patch(helper, side_effect=business), ThreadPoolExecutor(max_workers=20) as pool:
                futures = [pool.submit(invoke) for _ in range(20)]
                try:
                    self.assertTrue(all_attempted.wait(timeout=12))
                    self.assertEqual(len(admitted), 5 if route is auth.login else 20)
                    self.assertEqual(rejected, [429] * 15 if route is auth.login else [])
                finally:
                    release.set()
                for future in futures:
                    future.result(timeout=5)
            self.assertTrue(all(b.pending == 0 for b in self.limiter._buckets.values()))


if __name__ == "__main__":
    unittest.main()
