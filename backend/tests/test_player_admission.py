"""Bounded/fair waiting with no connection or thread-token acquisition."""
import asyncio
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.core.player_admission import _FairGate, PlayerAdmission, player_activity


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
