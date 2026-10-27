"""Bounded, process-local authentication throttling. NO_MULTI_WORKER_SAFE.

R2A must keep one worker and one backend instance. Restarting the process resets
all budgets and the ephemeral HMAC secret; this is intentional for R2B. No DB,
Redis, credential storage or proxy-header parsing belongs in this module.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import secrets
import threading
import time

from fastapi import Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.config import Settings


DETAIL = "Demasiados intentos. Inténtalo de nuevo más tarde."
logger = logging.getLogger("app.auth_rate_limit")


@dataclass(frozen=True, slots=True)
class Budget:
    capacity: int
    refill: float


POLICIES = {
    "login": {
        "volume:ip": Budget(60, 2),
        "failure:ip": Budget(60, 1),
        "failure:identifier": Budget(10, 1 / 10),
        "failure:pair": Budget(5, 1 / 12),
    },
    "activation": {
        "volume:ip": Budget(100, 5),
        "failure:ip": Budget(60, 1),
        "failure:code": Budget(10, 1 / 6),
        "failure:pair": Budget(5, 1 / 12),
    },
}


@dataclass(slots=True)
class Bucket:
    budget: Budget
    updated: float
    used: float
    debt: float = 0.0
    pending: int = 0

    def refill(self, now: float) -> None:
        self.debt = max(0.0, self.debt - max(0.0, now - self.updated) * self.budget.refill)
        self.updated = now

    def wait(self) -> float:
        if self.pending >= self.budget.capacity:
            # Only completion can free these slots; offer a short retry, not an
            # invented completion time. Pending slots do not refill with time.
            return 1.0
        return max(0.0, (self.debt + self.pending + 1 - self.budget.capacity) / self.budget.refill)


class RateLimitExceeded(HTTPException):
    def __init__(self, retry: float = 1, scope: str = "client_ip", fingerprint: str = ""):
        self.retry_after = max(1, math.ceil(retry))
        self.scope = scope
        self.fingerprint = fingerprint[:16]
        super().__init__(429, DETAIL, headers={"Retry-After": str(self.retry_after)})


def normalize_ip(host: str | None) -> str:
    try:
        if not isinstance(host, str) or "%" in host:
            raise ValueError
        address = ipaddress.ip_address(host)
    except ValueError:
        raise RateLimitExceeded() from None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


def client_ip(request: Request) -> str:
    # R2A/Uvicorn already resolves the trusted peer boundary. Never reparse headers.
    return normalize_ip(request.client.host if request.client else None)


class Reservation:
    __slots__ = ("_limiter", "_keys", "_finished")

    def __init__(self, limiter, keys):
        self._limiter, self._keys, self._finished = limiter, keys, False

    def commit_failure(self) -> None:
        self._limiter._finish(self, failure=True)

    def release(self) -> None:
        self._limiter._finish(self, failure=False)


class AuthRateLimiter:
    def __init__(self, *, clock=time.monotonic, secret: bytes | None = None,
                 max_entries=Settings.AUTH_RATE_LIMIT_MAX_ENTRIES,
                 idle_ttl=Settings.AUTH_RATE_LIMIT_IDLE_TTL,
                 cleanup_interval=Settings.AUTH_RATE_LIMIT_CLEANUP_INTERVAL):
        # Clock/key and lower memory ceilings are injectable for isolated tests,
        # never taken from JWT settings, request input or environment overrides.
        if not 1 <= max_entries <= 20_000 or idle_ttl <= 0 or cleanup_interval <= 0:
            raise ValueError("Invalid bounded limiter configuration")
        self._clock = clock
        self._secret = secrets.token_bytes(32) if secret is None else secret
        self._max_entries, self._idle_ttl = max_entries, idle_ttl
        self._cleanup_interval = cleanup_interval
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], Bucket] = {}
        self._last_cleanup = clock()
        self._next_log = float("-inf")

    def _key(self, namespace: str, *values: str) -> tuple[str, str]:
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()
        return namespace, digest

    def _cleanup(self, now: float) -> None:
        expired = []
        for key, bucket in self._buckets.items():
            bucket.refill(now)
            if bucket.pending == 0 and bucket.debt == 0 and now - bucket.used >= self._idle_ttl:
                expired.append(key)
        for key in expired:
            del self._buckets[key]
        self._last_cleanup = now

    def _admit(self, specs, *, failure: bool):
        # All budgets are checked/reserved in one critical section. No SQL,
        # hashing, logging, network, sleep or await executes while holding it.
        with self._lock:
            now = self._clock()
            if now - self._last_cleanup >= self._cleanup_interval:
                self._cleanup(now)
            blocked = []
            for key, budget in specs:
                bucket = self._buckets.get(key)
                if bucket is not None:
                    bucket.refill(now)
                    delay = bucket.wait()
                    if delay > 0:
                        blocked.append((delay, key))
            if blocked:
                retry, key = max(blocked, key=lambda item: item[0])
                raise RateLimitExceeded(retry, key[0], key[1])
            missing = sum(key not in self._buckets for key, _ in specs)
            if len(self._buckets) + missing > self._max_entries:
                self._cleanup(now)
                missing = sum(key not in self._buckets for key, _ in specs)
                if len(self._buckets) + missing > self._max_entries:
                    raise RateLimitExceeded(self._cleanup_interval, "storage")
            for key, budget in specs:
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = self._buckets[key] = Bucket(budget, now, now)
                bucket.used = now
                if failure:
                    bucket.pending += 1
                else:
                    bucket.debt += 1
            return Reservation(self, tuple(key for key, _ in specs)) if failure else None

    def take_volume(self, route: str, origin: str) -> None:
        namespace = "volume:ip"
        self._admit([(self._key(f"{route}:{namespace}", origin), POLICIES[route][namespace])], failure=False)

    def reserve(self, route: str, origin: str, identifier: str) -> Reservation:
        identifier_scope = "failure:identifier" if route == "login" else "failure:code"
        specs = [
            (self._key(f"{route}:failure:ip", origin), POLICIES[route]["failure:ip"]),
            (self._key(f"{route}:{identifier_scope}", identifier), POLICIES[route][identifier_scope]),
            (self._key(f"{route}:failure:pair", origin, identifier), POLICIES[route]["failure:pair"]),
        ]
        return self._admit(specs, failure=True)

    def _finish(self, reservation: Reservation, *, failure: bool) -> None:
        with self._lock:
            if reservation._finished:
                return
            now = self._clock()
            for key in reservation._keys:
                bucket = self._buckets[key]  # Pending reservations cannot be evicted.
                bucket.refill(now)
                bucket.pending -= 1
                if failure:
                    bucket.debt += 1
                bucket.used = now
            reservation._finished = True

    def log_rejection(self, route: str, rejection: RateLimitExceeded) -> None:
        # One process-wide slot per limiter, not a new logging key per attacker.
        with self._lock:
            now = self._clock()
            if now < self._next_log:
                return
            self._next_log = now + 10
        logger.warning("auth_throttled route=%s scope=%s fingerprint=%s retry_after=%d",
                       route, rejection.scope, rejection.fingerprint, rejection.retry_after)

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._buckets)


default_limiter = AuthRateLimiter()


def get_limiter(request: Request) -> AuthRateLimiter:
    return getattr(request.app.state, "auth_rate_limiter", default_limiter)


@contextmanager
def failure_budget(request: Request, route: str, identifier: str):
    limiter = get_limiter(request)
    try:
        reservation = limiter.reserve(route, client_ip(request), identifier)
    except RateLimitExceeded as rejected:
        limiter.log_rejection(route, rejected)
        raise
    try:
        yield
    except HTTPException as exc:
        failures = {401, 403} if route == "login" else {403, 404}
        if exc.status_code in failures:
            reservation.commit_failure()
        raise
    finally:
        # Also handles cancellation/BaseException and preserves the original error.
        reservation.release()


class AuthRateLimitMiddleware:
    """Login-only ASGI volume guard before body parsing; other routes pass through."""
    def __init__(self, app, api_prefix="/api"):
        self.app = app
        prefix = api_prefix.rstrip("/")
        self.paths = {f"{prefix}/auth/login": "login"}

    async def __call__(self, scope, receive, send):
        route = self.paths.get(scope.get("path", "").rstrip("/"))
        if scope["type"] == "http" and scope["method"] == "POST" and route:
            request = Request(scope)
            limiter = get_limiter(request)
            try:
                limiter.take_volume(route, client_ip(request))
            except RateLimitExceeded as rejected:
                limiter.log_rejection(route, rejected)
                response = JSONResponse({"detail": DETAIL}, status_code=429, headers=rejected.headers)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
