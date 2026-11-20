"""Finite process-local admission; playback persistence has a separate lane.

Heartbeat acceptance has two dedicated permits and twenty-two bounded waiters. Runtime and
the playback worker share two separate permits with one reserved share each;
they cannot borrow heartbeat's permits. Durable ingress remains independent.
No DB connection/thread token is acquired while waiting here. The five-second
heartbeat budget reserves its final second for the unchanged handler. Admitted
writes are never cancelled by a deadline.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
import time
import logging
import json
from typing import Callable

from fastapi import HTTPException

from app.core.player_diagnostics import admission_grant_recorder, _current_trace, _request_uuid


EXPENSIVE_CAPACITY = 2
INGRESS_CAPACITY = 2
HTTP_PENDING_PER_CLASS = 16
HEARTBEAT_ADMISSION_CAPACITY = 2
HEARTBEAT_MAX_WAITERS = 22
HEARTBEAT_REQUEST_BUDGET_NS = 5_000_000_000
HEARTBEAT_HANDLER_RESERVE_NS = 1_000_000_000
INGRESS_PENDING = 64
_KINDS = {
    "heartbeat": "heartbeat",
    "runtime-config": "runtime",
    "playback-events": "ingress",
}


def overloaded(reason="UNCLASSIFIED_HTTP_503"):
    # A failure before persistence is never an acceptance/201. Legacy Players
    # need their own durable retry to cover these unaccepted attempts.
    error = HTTPException(503, "Player operation temporarily unavailable", headers={"Retry-After": "1"})
    error._player_503_reason = reason
    return error


_HEARTBEAT_503_PHASES = {
    "ADMISSION_DEADLINE_AT_ENTRY": "before_admission",
    "ADMISSION_QUEUE_FULL": "before_admission",
    "ADMISSION_WAIT_DEADLINE": "waiting_before_grant",
    "ADMISSION_EXPIRED_HEAD": "waiting_before_grant",
    "ADMISSION_EXPIRED_AT_GRANT": "selected_before_grant",
    "ADMISSION_EXPIRED_AFTER_GRANT": "granted_before_handler",
    "TENANT_GUARD_LOCK_UNAVAILABLE": "handler_after_admission",
}


def _record_heartbeat_503(error, gate, request):
    """Always-on reason only; no request data or exception text enters the log."""
    try:
        reason = getattr(error, "_player_503_reason", None)
        if type(reason) is not str or reason not in _HEARTBEAT_503_PHASES:
            reason = "UNCLASSIFIED_HTTP_503"
        trace = _current_trace.get()
        request_id = trace.request_id if trace is not None else _request_uuid(request.scope.get("headers", []))
        state = gate.snapshot()
        logging.getLogger(__name__).warning(json.dumps({
            "event": "heartbeat_http_503",
            "request_id": request_id,
            "heartbeat_503_reason": reason,
            "status_code": error.status_code,
            "retry_after_present": any(name.lower() == "retry-after" for name in (error.headers or {})),
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "phase": _HEARTBEAT_503_PHASES.get(reason, "unclassified"),
            # Admission's original finally has already run. This is explicitly
            # the remaining gate state, not a claimed snapshot at rejection.
            "gate_state_after_cleanup": {
                "active": state["active"],
                "active_by_class": state["active_by_class"],
                "waiters": state["by_class"],
                "rejected": state["rejected"],
                "expired_before_handler": state["expired_before_handler"],
            },
        }, separators=(",", ":"), allow_nan=False))
    except Exception:
        # A diagnostic sink must never replace the original Player response.
        pass


@dataclass(eq=False)
class _Waiter:
    kind: str
    future: asyncio.Future
    started_ns: int
    granted: bool = False
    deadline_ns: int | None = None
    granted_ns: int | None = None
    on_granted: Callable[[int], None] | None = None


class _FairGate:
    def __init__(self, capacity, limits, *, reservations=None):
        self.capacity = capacity
        self.limits = dict(limits)
        self.reservations = {kind: 0 for kind in limits}
        if reservations:
            if set(reservations) - set(limits):
                raise ValueError("Unknown reserved Player work class")
            self.reservations.update(reservations)
        if (any(type(value) is not int or value < 0 for value in self.reservations.values())
                or sum(self.reservations.values()) > capacity):
            raise ValueError("Reserved Player capacity exceeds the global budget")
        self.queues = {kind: deque() for kind in limits}
        self.order = deque(limits)
        self.active = 0
        self.active_by_class = {kind: 0 for kind in limits}
        self.peak_pending = 0
        self.rejected = 0
        self.disconnected = 0
        self.expired = 0
        self.lock = RLock()

    def _dispatch(self):
        while self.active < self.capacity:
            now_ns = time.monotonic_ns()
            for queue in self.queues.values():
                while queue:
                    head = queue[0]
                    if head.future.done():
                        queue.popleft()
                    elif head.deadline_ns is not None and now_ns >= head.deadline_ns:
                        queue.popleft()
                        self.expired += 1
                        head.future.set_exception(overloaded("ADMISSION_EXPIRED_HEAD"))
                    else:
                        break
            selected = None
            # Recover a waiting class's reserved share before lending spare
            # permits. Borrowers already running are never interrupted.
            for reserved_only in (True, False):
                for _ in range(len(self.order)):
                    kind = self.order[0]
                    self.order.rotate(-1)
                    if (self.queues[kind] and (not reserved_only
                            or self.active_by_class[kind] < self.reservations[kind])):
                        selected = self.queues[kind].popleft()
                        break
                if selected is not None:
                    break
            if selected is None:
                return
            granted_ns = time.monotonic_ns()
            if selected.deadline_ns is not None and granted_ns >= selected.deadline_ns:
                self.expired += 1
                selected.future.set_exception(overloaded("ADMISSION_EXPIRED_AT_GRANT"))
                continue
            selected.granted = True
            selected.granted_ns = granted_ns
            self.active += 1
            self.active_by_class[selected.kind] += 1
            if selected.on_granted is not None:
                try:
                    selected.on_granted(granted_ns)
                except BaseException:
                    # Diagnostics cannot prevent delivery of an owned permit.
                    pass
            selected.future.set_result(None)

    def snapshot(self):
        with self.lock:
            queued = {kind: sum(not item.future.done() for item in queue)
                      for kind, queue in self.queues.items()}
            return {"capacity": self.capacity, "active": self.active,
                    "reserved": dict(self.reservations), "active_by_class": dict(self.active_by_class),
                    "waiters": sum(queued.values()), "by_class": queued,
                    "pending_limit": sum(self.limits.values()), "peak_waiters": self.peak_pending,
                    "rejected": self.rejected, "disconnected_pending_get": self.disconnected,
                    "expired_before_handler": self.expired}

    @asynccontextmanager
    async def admit(self, kind, request=None, *, deadline_ns=None):
        loop = asyncio.get_running_loop()
        item = _Waiter(kind, loop.create_future(), time.monotonic_ns(),
                       deadline_ns=deadline_ns, on_granted=admission_grant_recorder())
        with self.lock:
            if kind not in self.queues:
                raise RuntimeError("Unknown Player work class")
            if deadline_ns is not None and item.started_ns >= deadline_ns:
                self.expired += 1
                raise overloaded("ADMISSION_DEADLINE_AT_ENTRY")
            queued = sum(not waiter.future.done() for waiter in self.queues[kind])
            # Idle capacity can serve a request immediately; the finite bound
            # concerns callers that would actually have to wait.
            if self.active >= self.capacity and queued >= self.limits[kind]:
                self.rejected += 1
                raise overloaded("ADMISSION_QUEUE_FULL")
            self.queues[kind].append(item)
            self._dispatch()
            pending = sum(sum(not waiter.future.done() for waiter in queue) for queue in self.queues.values())
            self.peak_pending = max(self.peak_pending, pending)
        try:
            # request.is_disconnected consumes receive; use it ONLY for the
            # body-less GET route and only before any authentication or write.
            if request is None:
                if deadline_ns is None:
                    await item.future
                else:
                    remaining = max(0, deadline_ns - time.monotonic_ns()) / 1_000_000_000
                    try:
                        await asyncio.wait_for(asyncio.shield(item.future), remaining)
                    except TimeoutError:
                        with self.lock:
                            self.expired += 1
                        raise overloaded("ADMISSION_WAIT_DEADLINE") from None
            else:
                while not item.future.done():
                    if await request.is_disconnected():
                        with self.lock:
                            self.disconnected += 1
                        raise HTTPException(499, "Client disconnected before admission")
                    await asyncio.wait({item.future}, timeout=.05)
            await item.future
            if request is not None and await request.is_disconnected():
                with self.lock:
                    self.disconnected += 1
                raise HTTPException(499, "Client disconnected before admission")
            if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                with self.lock:
                    self.expired += 1
                raise overloaded("ADMISSION_EXPIRED_AFTER_GRANT")
            yield
        finally:
            with self.lock:
                if item.granted:
                    self.active -= 1
                    self.active_by_class[item.kind] -= 1
                else:
                    if not item.future.done():
                        item.future.cancel()
                    try:
                        self.queues[kind].remove(item)
                    except ValueError:
                        pass
                self._dispatch()


class PlayerAdmission:
    def __init__(self):
        self.heartbeat = _FairGate(HEARTBEAT_ADMISSION_CAPACITY, {
            "heartbeat": HEARTBEAT_MAX_WAITERS,
        })
        self.expensive = _FairGate(EXPENSIVE_CAPACITY, {
            "runtime": HTTP_PENDING_PER_CLASS,
            "playback_worker": 1,
        }, reservations={"runtime": 1, "playback_worker": 1})
        self.ingress = _FairGate(INGRESS_CAPACITY, {"ingress": INGRESS_PENDING})
        self.heartbeat_materializer = _FairGate(1, {"heartbeat_worker": 0})

    def snapshot(self):
        return {"heartbeat": self.heartbeat.snapshot(), "expensive": self.expensive.snapshot(),
                "ingress": self.ingress.snapshot(), "heartbeat_materializer": self.heartbeat_materializer.snapshot()}


def admissions(app):
    value = getattr(app.state, "player_admission", None)
    if value is None:
        # Called on this application's event loop before the first await.
        value = app.state.player_admission = PlayerAdmission()
    return value


@asynccontextmanager
async def player_activity(request):
    # Use the server-selected route, independent of API_PREFIX or a mount path.
    kind = _KINDS[request.scope["route"].path.rsplit("/", 1)[-1]]
    controls = admissions(request.app)
    gate = (controls.heartbeat if kind == "heartbeat" else
            controls.ingress if kind == "ingress" else controls.expensive)
    observer = request if kind == "runtime" and request.method == "GET" else None
    deadline_ns = None
    if kind == "heartbeat":
        # This origin is captured by the always-on HTTP middleware, not a
        # client-supplied header or the opt-in diagnostic trace.
        started_ns = request.scope.get("_player_admission_started_ns", time.monotonic_ns())
        # Latest grant, not a new client/handler timeout. Retain enough of the
        # original five seconds for auth, validation and the atomic commit.
        deadline_ns = started_ns + HEARTBEAT_REQUEST_BUDGET_NS - HEARTBEAT_HANDLER_RESERVE_NS
    try:
        async with gate.admit(kind, observer, deadline_ns=deadline_ns):
            yield
    except HTTPException as error:
        if kind == "heartbeat" and error.status_code == 503:
            _record_heartbeat_503(error, gate, request)
        raise


def playback_worker_activity(app):
    return admissions(app).expensive.admit("playback_worker")


def heartbeat_worker_activity(app):
    return admissions(app).heartbeat_materializer.admit("heartbeat_worker")


class HeartbeatRecoveryMiddleware:
    """Resume durable heartbeat reports independently of playback recovery."""
    def __init__(self, app, start):
        self.app, self.start = app, start

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope.setdefault("_player_admission_started_ns", time.monotonic_ns())
            try:
                self.start()
            except RuntimeError:
                logging.getLogger(__name__).warning("HEARTBEAT_INBOX_START_DEFERRED")
        await self.app(scope, receive, send)


class PlaybackRecoveryMiddleware:
    """Resume durable receipts on the first HTTP request, even lifespan=off."""
    def __init__(self, app, start):
        self.app = app
        self.start = start

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope.setdefault("_player_admission_started_ns", time.monotonic_ns())
            try:
                self.start()
            except RuntimeError:
                # A later health request can retry the wake. No persistence or
                # acceptance is invented when a consumer cannot be scheduled.
                logging.getLogger(__name__).warning("PLAYBACK_INBOX_START_DEFERRED")
        await self.app(scope, receive, send)
