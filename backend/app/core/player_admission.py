"""Finite process-local admission; playback persistence has a separate lane.

The expensive lane still has four permits. Round-robin queued classes prevent
one polling route from owning the complete FIFO. No DB connection/thread token
is acquired while a caller is waiting here. Only a disconnected, not-yet-
admitted runtime GET may be abandoned. Admitted writes are never cancelled.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import RLock
import time
import logging

from fastapi import HTTPException


EXPENSIVE_CAPACITY = 4
INGRESS_CAPACITY = 2
HTTP_PENDING_PER_CLASS = 16
INGRESS_PENDING = 64
_KINDS = {
    "heartbeat": "heartbeat",
    "runtime-config": "runtime",
    "playback-events": "ingress",
}


def overloaded():
    # A failure before persistence is never an acceptance/201. Legacy Players
    # need their own durable retry to cover these unaccepted attempts.
    return HTTPException(503, "Player operation temporarily unavailable", headers={"Retry-After": "1"})


@dataclass(eq=False)
class _Waiter:
    kind: str
    future: asyncio.Future
    started_ns: int
    granted: bool = False


class _FairGate:
    def __init__(self, capacity, limits):
        self.capacity = capacity
        self.limits = dict(limits)
        self.queues = {kind: deque() for kind in limits}
        self.order = deque(limits)
        self.active = 0
        self.peak_pending = 0
        self.rejected = 0
        self.disconnected = 0
        self.lock = RLock()

    def _dispatch(self):
        while self.active < self.capacity:
            selected = None
            for _ in range(len(self.order)):
                kind = self.order[0]
                self.order.rotate(-1)
                queue = self.queues[kind]
                while queue and queue[0].future.done():
                    queue.popleft()
                if queue:
                    selected = queue.popleft()
                    break
            if selected is None:
                return
            selected.granted = True
            self.active += 1
            selected.future.set_result(None)

    def snapshot(self):
        with self.lock:
            queued = {kind: sum(not item.future.done() for item in queue)
                      for kind, queue in self.queues.items()}
            return {"capacity": self.capacity, "active": self.active,
                    "waiters": sum(queued.values()), "by_class": queued,
                    "pending_limit": sum(self.limits.values()), "peak_waiters": self.peak_pending,
                    "rejected": self.rejected, "disconnected_pending_get": self.disconnected}

    @asynccontextmanager
    async def admit(self, kind, request=None):
        loop = asyncio.get_running_loop()
        item = _Waiter(kind, loop.create_future(), time.monotonic_ns())
        with self.lock:
            if kind not in self.queues:
                raise RuntimeError("Unknown Player work class")
            queued = sum(not waiter.future.done() for waiter in self.queues[kind])
            # Idle capacity can serve a request immediately; the finite bound
            # concerns callers that would actually have to wait.
            if self.active >= self.capacity and queued >= self.limits[kind]:
                self.rejected += 1
                raise overloaded()
            self.queues[kind].append(item)
            self._dispatch()
            pending = sum(sum(not waiter.future.done() for waiter in queue) for queue in self.queues.values())
            self.peak_pending = max(self.peak_pending, pending)
        try:
            # request.is_disconnected consumes receive; use it ONLY for the
            # body-less GET route and only before any authentication or write.
            if request is None:
                await item.future
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
            yield
        finally:
            with self.lock:
                if item.granted:
                    self.active -= 1
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
        self.expensive = _FairGate(EXPENSIVE_CAPACITY, {
            "heartbeat": HTTP_PENDING_PER_CLASS, "runtime": HTTP_PENDING_PER_CLASS,
            "playback_worker": 1,
        })
        self.ingress = _FairGate(INGRESS_CAPACITY, {"ingress": INGRESS_PENDING})

    def snapshot(self):
        return {"expensive": self.expensive.snapshot(), "ingress": self.ingress.snapshot()}


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
    gate = controls.ingress if kind == "ingress" else controls.expensive
    observer = request if kind == "runtime" and request.method == "GET" else None
    async with gate.admit(kind, observer):
        yield


def playback_worker_activity(app):
    return admissions(app).expensive.admit("playback_worker")


class PlaybackRecoveryMiddleware:
    """Resume durable receipts on the first HTTP request, even lifespan=off."""
    def __init__(self, app, start):
        self.app = app
        self.start = start

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            try:
                self.start()
            except RuntimeError:
                # A later health request can retry the wake. No persistence or
                # acceptance is invented when a consumer cannot be scheduled.
                logging.getLogger(__name__).warning("PLAYBACK_INBOX_START_DEFERRED")
        await self.app(scope, receive, send)
