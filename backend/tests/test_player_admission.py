"""Bounded/fair waiting with no connection or thread-token acquisition."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from fastapi import HTTPException

from app.core.player_admission import _FairGate, PlayerAdmission, PlaybackRecoveryMiddleware, player_activity


class PlayerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def hold(self, gate, kind, release, entered=None):
        async with gate.admit(kind):
            if entered is not None:
                entered.set()
            await release.wait()

    async def fill(self, gate, kind):
        release = asyncio.Event()
        tasks = []
        for _ in range(gate.capacity):
            entered = asyncio.Event()
            task = asyncio.create_task(self.hold(gate, kind, release, entered))
            tasks.append(task)
            await asyncio.wait_for(entered.wait(), 1)
        return release, tasks

    async def test_expensive_capacity_stays_four_and_ingress_is_separate(self):
        lanes = PlayerAdmission()
        self.assertEqual(lanes.expensive.capacity, 4)
        self.assertEqual(lanes.ingress.capacity, 2)
        release, holders = await self.fill(lanes.expensive, "runtime")
        try:
            async with lanes.ingress.admit("ingress"):
                self.assertEqual(lanes.ingress.snapshot()["active"], 1)
                self.assertEqual(lanes.expensive.snapshot()["active"], 4)
        finally:
            release.set()
            await asyncio.gather(*holders)

    async def test_finite_pending_limit_rejects_before_execution(self):
        gate = _FairGate(1, {"heartbeat": 2, "runtime": 2})
        release, holders = await self.fill(gate, "heartbeat")
        pending = [asyncio.create_task(self.hold(gate, "heartbeat", release)) for _ in range(2)]
        await asyncio.sleep(0)
        try:
            with self.assertRaises(HTTPException) as result:
                async with gate.admit("heartbeat"):
                    self.fail("Overflow must not execute")
            self.assertEqual(result.exception.status_code, 503)
            self.assertEqual(result.exception.headers["Retry-After"], "1")
            self.assertEqual(gate.snapshot()["waiters"], 2)
            self.assertEqual(gate.snapshot()["rejected"], 1)
        finally:
            release.set()
            await asyncio.gather(*holders, *pending)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_round_robin_prevents_one_route_fifo_monopoly(self):
        gate = _FairGate(1, {"heartbeat": 8, "runtime": 8, "playback_worker": 1})
        release, holders = await self.fill(gate, "heartbeat")
        order = []
        async def visit(kind):
            async with gate.admit(kind):
                order.append(kind)
                await asyncio.sleep(0)
        tasks = [asyncio.create_task(visit("heartbeat")) for _ in range(4)]
        tasks += [asyncio.create_task(visit("runtime")), asyncio.create_task(visit("playback_worker"))]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*holders, *tasks)
        self.assertLess(order.index("runtime"), 4)
        self.assertLess(order.index("playback_worker"), 4)
        self.assertEqual(order.count("heartbeat"), 4)

    async def test_one_class_borrows_all_idle_expensive_permits(self):
        gate = _FairGate(4, {"heartbeat": 16, "runtime": 16, "playback_worker": 1})
        release, tasks = await self.fill(gate, "heartbeat")
        self.assertEqual(gate.snapshot()["active"], 4)
        release.set()
        await asyncio.gather(*tasks)

    async def test_cancelled_waiter_never_leaks_or_steals_a_permit(self):
        gate = _FairGate(1, {"runtime": 2})
        release, holders = await self.fill(gate, "runtime")
        waiting = asyncio.create_task(self.hold(gate, "runtime", release))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(gate.snapshot()["waiters"], 0)
        self.assertEqual(gate.snapshot()["active"], 1)
        release.set()
        await asyncio.gather(*holders)
        async with gate.admit("runtime"):
            self.assertEqual(gate.snapshot()["active"], 1)
        self.assertEqual(gate.snapshot()["active"], 0)

    async def test_disconnected_pending_get_is_removed_without_running(self):
        gate = _FairGate(1, {"runtime": 2})
        release, holders = await self.fill(gate, "runtime")
        async def disconnected():
            return True
        try:
            with self.assertRaises(HTTPException) as result:
                async with gate.admit("runtime", SimpleNamespace(is_disconnected=disconnected)):
                    self.fail("Disconnected pending GET must not run")
            self.assertEqual(result.exception.status_code, 499)
            self.assertEqual(gate.snapshot()["waiters"], 0)
            self.assertEqual(gate.snapshot()["active"], 1)
        finally:
            release.set()
            await asyncio.gather(*holders)

    async def test_disconnect_at_grant_releases_exactly_one_permit(self):
        gate = _FairGate(1, {"runtime": 2})
        async def disconnected():
            return True
        with self.assertRaises(HTTPException):
            async with gate.admit("runtime", SimpleNamespace(is_disconnected=disconnected)):
                self.fail("No work after a proven disconnect")
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)
        async with gate.admit("runtime"):
            self.assertEqual(gate.snapshot()["active"], 1)

    async def test_active_work_is_not_interrupted_by_later_disconnect(self):
        gate = _FairGate(1, {"runtime": 2})
        state = {"disconnected": False, "calls": 0}
        async def disconnected():
            state["calls"] += 1
            return state["disconnected"]
        async with gate.admit("runtime", SimpleNamespace(is_disconnected=disconnected)):
            state["disconnected"] = True
            await asyncio.sleep(.01)
            completed = True
        self.assertTrue(completed)
        self.assertEqual(state["calls"], 1)

    async def test_post_routes_never_consume_the_request_receive_channel(self):
        app = SimpleNamespace(state=SimpleNamespace())
        async def forbidden():
            raise AssertionError("Do not read POST receive for cancellation")
        for path in ("/api/player/heartbeat", "/api/player/playback-events"):
            request = SimpleNamespace(app=app, method="POST", url=SimpleNamespace(path=path),
                                      scope={"route": SimpleNamespace(path=path)},
                                      is_disconnected=forbidden)
            async with player_activity(request):
                self.assertTrue(True)
        self.assertEqual(app.state.player_admission.expensive.snapshot()["active"], 0)
        self.assertEqual(app.state.player_admission.ingress.snapshot()["active"], 0)

    async def test_real_router_preserves_auth_errors_under_custom_api_prefixes(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api.deps import get_current_player_device
        from app.api.routes.player import router

        def denied():
            raise HTTPException(401, "Synthetic missing player token")

        for prefix in ("/api", "/qa-custom", "/qa/nested/v2"):
            app = FastAPI()
            app.state.player_admission = PlayerAdmission()
            app.include_router(router, prefix=prefix + "/player")
            app.dependency_overrides[get_current_player_device] = denied
            async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                                   base_url="http://testserver") as client:
                for method, path, body in (
                    ("GET", "runtime-config", None),
                    ("POST", "heartbeat", {"status": "online"}),
                    ("POST", "playback-events", {"started_at": "2026-01-01T00:00:00Z"}),
                ):
                    with self.subTest(prefix=prefix, path=path):
                        response = await client.request(method, prefix + "/player/" + path, json=body)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.json(), {"detail": "Synthetic missing player token"})
            self.assertEqual(app.state.player_admission.expensive.snapshot()["active"], 0)
            self.assertEqual(app.state.player_admission.expensive.snapshot()["waiters"], 0)
            self.assertEqual(app.state.player_admission.ingress.snapshot()["active"], 0)
            self.assertEqual(app.state.player_admission.ingress.snapshot()["waiters"], 0)

    async def test_reserved_heartbeat_precedes_another_borrower(self):
        # Three runtimes borrow otherwise idle capacity. The last admitted HB
        # leaves ordinary round-robin pointing at runtime; a second HB must
        # still receive its reserved share when one borrowed slot is released.
        gate = PlayerAdmission().expensive
        releases, holders = [], []
        for kind in ("runtime", "runtime", "runtime", "heartbeat"):
            release, entered = asyncio.Event(), asyncio.Event()
            releases.append(release)
            holders.append(asyncio.create_task(self.hold(gate, kind, release, entered)))
            await asyncio.wait_for(entered.wait(), 1)
        pending_release = asyncio.Event()
        runtime_entered, heartbeat_entered = asyncio.Event(), asyncio.Event()
        waiting = [
            asyncio.create_task(self.hold(gate, "runtime", pending_release, runtime_entered)),
            asyncio.create_task(self.hold(gate, "heartbeat", pending_release, heartbeat_entered)),
        ]
        await asyncio.sleep(0)
        try:
            self.assertEqual(gate.snapshot()["active"], 4)
            self.assertEqual(gate.snapshot()["waiters"], 2)
            releases[0].set()
            await asyncio.wait_for(heartbeat_entered.wait(), 1)
            self.assertFalse(runtime_entered.is_set(), "A borrower must not take HB's reserved share")
            self.assertEqual(gate.snapshot()["active"], 4)
        finally:
            for release in releases:
                release.set()
            pending_release.set()
            await asyncio.wait_for(asyncio.gather(*holders, *waiting), 2)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_runtime_and_worker_reclaim_reserved_slots_during_heartbeat_burst(self):
        gate = PlayerAdmission().expensive
        releases, holders = [], []
        for _ in range(4):
            release, entered = asyncio.Event(), asyncio.Event()
            releases.append(release)
            holders.append(asyncio.create_task(self.hold(gate, "heartbeat", release, entered)))
            await asyncio.wait_for(entered.wait(), 1)
        pending_release = asyncio.Event()
        heartbeat_entered, runtime_entered, worker_entered = (asyncio.Event() for _ in range(3))
        waiting = [
            asyncio.create_task(self.hold(gate, "heartbeat", pending_release, heartbeat_entered)),
            asyncio.create_task(self.hold(gate, "runtime", pending_release, runtime_entered)),
            asyncio.create_task(self.hold(gate, "playback_worker", pending_release, worker_entered)),
        ]
        await asyncio.sleep(0)
        observers = [asyncio.create_task(runtime_entered.wait()), asyncio.create_task(worker_entered.wait())]
        try:
            releases[0].set()
            # The reservation policy can choose either deficient class first.
            completed, _ = await asyncio.wait(
                observers,
                timeout=1, return_when=asyncio.FIRST_COMPLETED,
            )
            self.assertTrue(completed, "Runtime or consumer must reclaim its share")
            self.assertFalse(heartbeat_entered.is_set())
            releases[1].set()
            await asyncio.wait_for(asyncio.gather(runtime_entered.wait(), worker_entered.wait()), 1)
            self.assertFalse(heartbeat_entered.is_set(), "HB must not starve runtime or durable processing")
            self.assertEqual(gate.snapshot()["active"], 4)
        finally:
            for release in releases:
                release.set()
            pending_release.set()
            await asyncio.wait_for(asyncio.gather(*holders, *waiting), 2)
            for observer in observers:
                if not observer.done():
                    observer.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_reserved_gate_stays_work_conserving_with_global_four(self):
        gate = PlayerAdmission().expensive
        for kind in ("heartbeat", "runtime", "playback_worker"):
            with self.subTest(kind=kind):
                release, holders = await self.fill(gate, kind)
                try:
                    self.assertEqual(gate.snapshot()["active"], 4,
                                     "Uncontended work may borrow every idle permit")
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*holders), 2)
        active = peak = 0
        completed = []

        async def visit(kind):
            nonlocal active, peak
            async with gate.admit(kind):
                active += 1
                peak = max(peak, active)
                self.assertLessEqual(active, 4)
                try:
                    await asyncio.sleep(0)
                    completed.append(kind)
                finally:
                    active -= 1
        kinds = ["heartbeat"] * 12 + ["runtime"] * 6 + ["playback_worker"]
        await asyncio.wait_for(asyncio.gather(*(visit(kind) for kind in kinds)), 2)
        self.assertEqual(peak, 4)
        self.assertCountEqual(completed, kinds)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_actual_heartbeat_queue_is_bounded_at_sixteen(self):
        gate = PlayerAdmission().expensive
        release, holders = await self.fill(gate, "heartbeat")
        pending = [asyncio.create_task(self.hold(gate, "heartbeat", release)) for _ in range(16)]
        await asyncio.sleep(0)
        try:
            self.assertEqual(gate.snapshot()["by_class"]["heartbeat"], 16)
            with self.assertRaises(HTTPException) as result:
                async with gate.admit("heartbeat"):
                    self.fail("Seventeenth waiting HB must not execute")
            self.assertEqual(result.exception.status_code, 503)
            self.assertEqual(result.exception.headers["Retry-After"], "1")
            self.assertEqual(gate.snapshot()["active"], 4)
            self.assertEqual(gate.snapshot()["by_class"]["heartbeat"], 16)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*holders, *pending), 2)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_already_expired_deadline_never_runs_or_borrows_capacity(self):
        gate = _FairGate(1, {"heartbeat": 2})
        with self.assertRaises(HTTPException) as result:
            async with gate.admit("heartbeat", deadline_ns=time.monotonic_ns() - 1):
                self.fail("An expired, unadmitted request must not execute")
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(result.exception.headers["Retry-After"], "1")
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)
        async with gate.admit("heartbeat"):
            self.assertEqual(gate.snapshot()["active"], 1)

    async def test_queued_deadline_expires_without_releasing_the_active_holder(self):
        gate = _FairGate(1, {"heartbeat": 2})
        release, holders = await self.fill(gate, "heartbeat")
        executed = False

        async def waiting():
            nonlocal executed
            async with gate.admit("heartbeat", deadline_ns=time.monotonic_ns() + 50_000_000):
                executed = True
        task = asyncio.create_task(waiting())
        try:
            with self.assertRaises(HTTPException) as result:
                await asyncio.wait_for(task, 1)
            self.assertEqual(result.exception.status_code, 503)
            self.assertFalse(executed)
            self.assertEqual(gate.snapshot()["active"], 1)
            self.assertEqual(gate.snapshot()["waiters"], 0)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*holders), 2)
        async with gate.admit("heartbeat"):
            self.assertEqual(gate.snapshot()["active"], 1)
        self.assertEqual(gate.snapshot()["active"], 0)

    async def test_deadline_does_not_cancel_already_admitted_work(self):
        gate = _FairGate(1, {"heartbeat": 2})
        clock = {"now": time.monotonic_ns()}
        deadline = clock["now"] + 1_000_000_000
        with patch("app.core.player_admission.time.monotonic_ns", side_effect=lambda: clock["now"]):
            async with gate.admit("heartbeat", deadline_ns=deadline):
                clock["now"] = deadline + 1
                await asyncio.sleep(0)
                self.assertEqual(gate.snapshot()["active"], 1)
                completed = True
        self.assertTrue(completed)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_heartbeat_budget_starts_at_asgi_entry_without_diagnostics(self):
        # No diagnostic middleware/app startup/database is installed. An
        # artificial recovery delay consumes the original server-side budget;
        # the handler cannot obtain a fresh five seconds at dependency entry.
        app = SimpleNamespace(state=SimpleNamespace())
        scope = {"type": "http", "method": "POST", "path": "/api/player/heartbeat",
                 "route": SimpleNamespace(path="/api/player/heartbeat"),
                 "headers": [(b"x-request-start", b"999999999999999999999")]}
        clock = {"now": 10_000_000_000}
        wake_calls = []
        executed = False

        def start():
            wake_calls.append(True)
            clock["now"] += 6_000_000_000

        async def forbidden_receive():
            raise AssertionError("POST receive must remain owned by the HTTP handler")

        async def unused_send(message):
            self.fail("Admission rejection propagates before any downstream HTTP work")

        async def downstream(current_scope, receive, send):
            nonlocal executed
            request = SimpleNamespace(app=app, method="POST", scope=current_scope,
                                      is_disconnected=forbidden_receive)
            async with player_activity(request):
                executed = True

        middleware = PlaybackRecoveryMiddleware(downstream, start)
        with patch("app.core.player_admission.time.monotonic_ns", side_effect=lambda: clock["now"]), \
                patch("app.core.player_admission.admission_grant_recorder", return_value=None):
            with self.assertRaises(HTTPException) as result:
                await middleware(scope, forbidden_receive, unused_send)
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(result.exception.headers["Retry-After"], "1")
        self.assertEqual(scope["_player_admission_started_ns"], 10_000_000_000)
        self.assertEqual(len(wake_calls), 1)
        self.assertFalse(executed)
        self.assertEqual(app.state.player_admission.expensive.snapshot()["active"], 0)
        self.assertEqual(app.state.player_admission.expensive.snapshot()["waiters"], 0)
        self.assertEqual(app.state.player_admission.expensive.snapshot()["expired_before_handler"], 1)

    async def test_handler_failure_releases_capacity_and_queue_drains(self):
        gate = _FairGate(1, {"heartbeat": 2})
        with self.assertRaisesRegex(ValueError, "synthetic"):
            async with gate.admit("heartbeat"):
                raise ValueError("synthetic")
        async with gate.admit("heartbeat"):
            self.assertEqual(gate.snapshot()["active"], 1)
        self.assertEqual(gate.snapshot()["active"], 0)


if __name__ == "__main__":
    unittest.main()
