"""Bounded/fair waiting with no connection or thread-token acquisition."""
import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4
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

    async def test_heartbeat_two_nonheartbeat_two_and_ingress_two_are_independent(self):
        lanes = PlayerAdmission()
        self.assertEqual(lanes.heartbeat.capacity, 2)
        self.assertEqual(lanes.expensive.capacity, 2)
        self.assertEqual(lanes.heartbeat.capacity + lanes.expensive.capacity, 4)
        self.assertEqual(lanes.ingress.capacity, 2)
        self.assertEqual(lanes.heartbeat_materializer.capacity, 1)
        self.assertEqual(lanes.heartbeat_materializer.limits, {"heartbeat_worker": 0})
        self.assertEqual(lanes.expensive.reservations, {"runtime": 1, "playback_worker": 1})
        release, holders = await self.fill(lanes.expensive, "runtime")
        try:
            async with lanes.heartbeat.admit("heartbeat"), lanes.ingress.admit("ingress"):
                self.assertEqual(lanes.heartbeat.snapshot()["active"], 1)
                self.assertEqual(lanes.ingress.snapshot()["active"], 1)
                self.assertEqual(lanes.expensive.snapshot()["active"], 2)
        finally:
            release.set()
            await asyncio.gather(*holders)
        self.assertEqual(lanes.heartbeat.snapshot()["active"], 0)
        self.assertEqual(lanes.expensive.snapshot()["active"], 0)

    async def test_durable_heartbeat_background_lane_is_single_and_cannot_borrow_http_capacity(self):
        lanes = PlayerAdmission()
        release, background = await self.fill(lanes.heartbeat_materializer, "heartbeat_worker")
        try:
            with self.assertRaises(HTTPException) as rejected:
                async with lanes.heartbeat_materializer.admit("heartbeat_worker"):
                    self.fail("A second materializer must not create an unbounded background queue")
            self.assertEqual(rejected.exception.status_code, 503)
            self.assertEqual(lanes.heartbeat_materializer.snapshot()["active"], 1)
            self.assertEqual(lanes.heartbeat_materializer.snapshot()["waiters"], 0)
            async with lanes.heartbeat.admit("heartbeat"), lanes.expensive.admit("runtime"), lanes.ingress.admit("ingress"):
                self.assertEqual(lanes.heartbeat.snapshot()["active"], 1)
                self.assertEqual(lanes.expensive.snapshot()["active"], 1)
                self.assertEqual(lanes.ingress.snapshot()["active"], 1)
                self.assertEqual(lanes.heartbeat_materializer.snapshot()["active"], 1)
        finally:
            release.set()
            await asyncio.gather(*background)
        self.assertTrue(all(lane["active"] == lane["waiters"] == 0 for lane in lanes.snapshot().values()))

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

    async def test_nonheartbeat_borrowers_cannot_take_or_receive_heartbeat_capacity(self):
        lanes = PlayerAdmission()
        other_release, others = await self.fill(lanes.expensive, "runtime")
        waiting_release, runtime_entered = asyncio.Event(), asyncio.Event()
        waiting = asyncio.create_task(self.hold(lanes.expensive, "runtime", waiting_release, runtime_entered))
        await asyncio.sleep(0)
        heartbeat_release, heartbeats = await self.fill(lanes.heartbeat, "heartbeat")
        try:
            self.assertEqual(lanes.expensive.snapshot()["active"], 2)
            self.assertEqual(lanes.expensive.snapshot()["waiters"], 1)
            self.assertEqual(lanes.heartbeat.snapshot()["active"], 2)
            self.assertFalse(runtime_entered.is_set())
            heartbeat_release.set()
            await asyncio.gather(*heartbeats)
            self.assertFalse(runtime_entered.is_set(), "HB releases cannot lend a permit across lanes")
            self.assertEqual(lanes.expensive.snapshot()["active"], 2)
        finally:
            other_release.set()
            waiting_release.set()
            heartbeat_release.set()
            await asyncio.gather(*others, *heartbeats, waiting)
        self.assertEqual(lanes.expensive.snapshot()["active"], 0)
        self.assertEqual(lanes.expensive.snapshot()["waiters"], 0)
        self.assertEqual(lanes.heartbeat.snapshot()["active"], 0)

    async def test_full_heartbeat_lane_cannot_block_runtime_and_worker(self):
        lanes = PlayerAdmission()
        heartbeat_release, heartbeats = await self.fill(lanes.heartbeat, "heartbeat")
        pending_release = asyncio.Event()
        heartbeat_entered, runtime_entered, worker_entered = (asyncio.Event() for _ in range(3))
        waiting = [
            asyncio.create_task(self.hold(lanes.heartbeat, "heartbeat", pending_release, heartbeat_entered)),
            asyncio.create_task(self.hold(lanes.expensive, "runtime", pending_release, runtime_entered)),
            asyncio.create_task(self.hold(lanes.expensive, "playback_worker", pending_release, worker_entered)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(runtime_entered.wait(), worker_entered.wait()), 1)
            self.assertFalse(heartbeat_entered.is_set())
            self.assertEqual(lanes.heartbeat.snapshot()["active"], 2)
            self.assertEqual(lanes.heartbeat.snapshot()["waiters"], 1)
            self.assertEqual(lanes.expensive.snapshot()["active_by_class"], {"runtime": 1, "playback_worker": 1})
            pending_release.set()
            await asyncio.gather(*waiting[1:])
            self.assertFalse(heartbeat_entered.is_set(), "Non-HB releases cannot lend a permit across lanes")
        finally:
            heartbeat_release.set()
            pending_release.set()
            await asyncio.gather(*heartbeats, *waiting)
        self.assertEqual(lanes.expensive.snapshot()["active"], 0)
        self.assertEqual(lanes.heartbeat.snapshot()["active"], 0)
        self.assertEqual(lanes.heartbeat.snapshot()["waiters"], 0)

    async def test_each_lane_is_work_conserving_with_four_http_expensive_permits(self):
        lanes = PlayerAdmission()
        for gate, kind, capacity in ((lanes.heartbeat, "heartbeat", 2),
                                     (lanes.expensive, "runtime", 2),
                                     (lanes.expensive, "playback_worker", 2)):
            with self.subTest(kind=kind):
                release, holders = await self.fill(gate, kind)
                try:
                    self.assertEqual(gate.snapshot()["active"], capacity)
                finally:
                    release.set()
                    await asyncio.gather(*holders)
        active = {"heartbeat": 0, "nonheartbeat": 0}
        peaks = dict(active)
        peak_total = 0
        completed = []

        async def visit(kind):
            nonlocal peak_total
            lane = "heartbeat" if kind == "heartbeat" else "nonheartbeat"
            gate = lanes.heartbeat if lane == "heartbeat" else lanes.expensive
            async with gate.admit(kind):
                active[lane] += 1
                peaks[lane] = max(peaks[lane], active[lane])
                peak_total = max(peak_total, sum(active.values()))
                self.assertLessEqual(active["heartbeat"], 2)
                self.assertLessEqual(active["nonheartbeat"], 2)
                self.assertLessEqual(sum(active.values()), 4)
                try:
                    await asyncio.sleep(0)
                    completed.append(kind)
                finally:
                    active[lane] -= 1
        kinds = ["heartbeat"] * 12 + ["runtime"] * 6 + ["playback_worker"]
        await asyncio.wait_for(asyncio.gather(*(visit(kind) for kind in kinds)), 2)
        self.assertEqual(peaks, {"heartbeat": 2, "nonheartbeat": 2})
        self.assertEqual(peak_total, 4)
        self.assertCountEqual(completed, kinds)
        for gate in (lanes.heartbeat, lanes.expensive):
            self.assertEqual(gate.snapshot()["active"], 0)
            self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_actual_heartbeat_queue_is_bounded_at_twenty_two(self):
        gate = PlayerAdmission().heartbeat
        release, holders = await self.fill(gate, "heartbeat")
        pending = [asyncio.create_task(self.hold(gate, "heartbeat", release)) for _ in range(22)]
        await asyncio.sleep(0)
        try:
            self.assertEqual(gate.snapshot()["by_class"]["heartbeat"], 22)
            with self.assertRaises(HTTPException) as result:
                async with gate.admit("heartbeat"):
                    self.fail("Twenty-third waiting HB must not execute")
            self.assertEqual(result.exception.status_code, 503)
            self.assertEqual(result.exception.headers["Retry-After"], "1")
            self.assertEqual(result.exception._player_503_reason, "ADMISSION_QUEUE_FULL")
            self.assertEqual(gate.snapshot()["active"], 2)
            self.assertEqual(gate.snapshot()["by_class"]["heartbeat"], 22)
        finally:
            release.set()
            await asyncio.gather(*holders, *pending)
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
        self.assertEqual(app.state.player_admission.heartbeat.snapshot()["active"], 0)
        self.assertEqual(app.state.player_admission.heartbeat.snapshot()["waiters"], 0)
        self.assertEqual(app.state.player_admission.heartbeat.snapshot()["expired_before_handler"], 1)

    async def test_handler_failure_releases_capacity_and_queue_drains(self):
        gate = _FairGate(1, {"heartbeat": 2})
        with self.assertRaisesRegex(ValueError, "synthetic"):
            async with gate.admit("heartbeat"):
                raise ValueError("synthetic")
        async with gate.admit("heartbeat"):
            self.assertEqual(gate.snapshot()["active"], 1)
        self.assertEqual(gate.snapshot()["active"], 0)


    async def test_worker_reclaims_its_nonheartbeat_reserved_share_from_runtime_borrowers(self):
        gate = PlayerAdmission().expensive
        releases, holders = [], []
        for _ in range(2):
            release, entered = asyncio.Event(), asyncio.Event()
            releases.append(release)
            holders.append(asyncio.create_task(self.hold(gate, "runtime", release, entered)))
            await entered.wait()
        pending_release, runtime_entered, worker_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pending = [asyncio.create_task(self.hold(gate, "runtime", pending_release, runtime_entered)),
                   asyncio.create_task(self.hold(gate, "playback_worker", pending_release, worker_entered))]
        await asyncio.sleep(0)
        try:
            releases[0].set()
            await asyncio.wait_for(worker_entered.wait(), 1)
            self.assertFalse(runtime_entered.is_set())
            self.assertEqual(gate.snapshot()["active_by_class"], {"runtime": 1, "playback_worker": 1})
        finally:
            for release in releases:
                release.set()
            pending_release.set()
            await asyncio.gather(*holders, *pending)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_heartbeat_latest_grant_reserves_one_second_of_unchanged_total_budget(self):
        from app.core import player_admission as admission

        self.assertEqual(admission.HEARTBEAT_REQUEST_BUDGET_NS, 5_000_000_000)
        self.assertEqual(admission.HEARTBEAT_HANDLER_RESERVE_NS, 1_000_000_000)
        origin = 10_000_000_000
        clock = {"now": origin + 3_999_999_999}
        controls = PlayerAdmission()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(player_admission=controls)), method="POST",
                                  scope={"route": SimpleNamespace(path="/api/player/heartbeat"),
                                         "_player_admission_started_ns": origin})
        with patch.object(admission, "time", SimpleNamespace(monotonic_ns=lambda: clock["now"])):
            async with player_activity(request):
                self.assertEqual(controls.heartbeat.snapshot()["active"], 1)
                # The reserve controls entry only. Already admitted writes must
                # still finish even if the original five-second budget elapses.
                clock["now"] = origin + 6_000_000_000
                await asyncio.sleep(0)
                self.assertEqual(controls.heartbeat.snapshot()["active"], 1)
        self.assertEqual(controls.heartbeat.snapshot()["active"], 0)
        clock["now"] = origin + 4_000_000_000
        with patch.object(admission, "time", SimpleNamespace(monotonic_ns=lambda: clock["now"])):
            with self.assertLogs("app.core.player_admission", level="WARNING"):
                with self.assertRaises(HTTPException) as error:
                    async with player_activity(request):
                        self.fail("No new handler may start at or after the latest grant boundary")
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception._player_503_reason, "ADMISSION_DEADLINE_AT_ENTRY")
        self.assertEqual(controls.heartbeat.snapshot()["active"], 0)
        self.assertEqual(controls.heartbeat.snapshot()["waiters"], 0)

    async def test_process_diagnostics_aggregate_both_expensive_lanes_and_keep_full_snapshots(self):
        from app.core import player_diagnostics as diagnostic

        lanes = PlayerAdmission()
        hb_release, heartbeats = await self.fill(lanes.heartbeat, "heartbeat")
        other_release, others = await self.fill(lanes.expensive, "runtime")
        background_release, background = await self.fill(lanes.heartbeat_materializer, "heartbeat_worker")
        try:
            with patch.object(diagnostic._state, "admission_snapshot", lanes.snapshot):
                row = diagnostic._counter_snapshot()
            self.assertEqual(row["admission_capacity"], 5)
            self.assertEqual(row["admission_active"], 5)
            self.assertEqual(row["admission_waiters"], 0)
            self.assertEqual(row["player_admission"], lanes.snapshot())
            self.assertEqual(row["player_admission"]["heartbeat"]["active"], 2)
            self.assertEqual(row["player_admission"]["expensive"]["active"], 2)
            self.assertEqual(row["player_admission"]["heartbeat_materializer"]["active"], 1)
        finally:
            hb_release.set()
            other_release.set()
            background_release.set()
            await asyncio.gather(*heartbeats, *others, *background)



class Heartbeat503ReasonTests(unittest.IsolatedAsyncioTestCase):
    async def _hold(self, gate, release, entered):
        async with gate.admit("heartbeat"):
            entered.set()
            await release.wait()

    async def _fill(self, gate):
        release, tasks = asyncio.Event(), []
        for _ in range(gate.capacity):
            entered = asyncio.Event()
            tasks.append(asyncio.create_task(self._hold(gate, release, entered)))
            await entered.wait()
        return release, tasks

    async def _capture(self, gate, deadline):
        try:
            async with gate.admit("heartbeat", deadline_ns=deadline):
                self.fail("This synthetic expired/full request must never enter work")
        except HTTPException as error:
            return error

    def _assert_admission_error(self, error, reason):
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.detail, "Player operation temporarily unavailable")
        self.assertEqual(error.headers, {"Retry-After": "1"})
        self.assertEqual(error._player_503_reason, reason)

    def _request(self, controls, path="heartbeat"):
        async def disconnected():
            return False
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(player_admission=controls)),
            method="GET" if path == "runtime-config" else "POST",
            scope={"route": SimpleNamespace(path="/api/player/" + path)},
            is_disconnected=disconnected,
        )

    async def test_six_real_admission_rejection_branches_preserve_http_and_permits(self):
        from app.core import player_admission as admission

        self.assertEqual(admission.EXPENSIVE_CAPACITY, 2)
        self.assertEqual(admission.HEARTBEAT_ADMISSION_CAPACITY, 2)
        self.assertEqual(admission.HEARTBEAT_MAX_WAITERS, 22)
        self.assertEqual(admission.HEARTBEAT_REQUEST_BUDGET_NS, 5_000_000_000)
        gate = PlayerAdmission().heartbeat
        error = await self._capture(gate, time.monotonic_ns() - 1)
        self._assert_admission_error(error, "ADMISSION_DEADLINE_AT_ENTRY")
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)


        gate = PlayerAdmission().heartbeat
        release, holders = await self._fill(gate)
        pending = [asyncio.create_task(self._capture(
            gate, time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS)) for _ in range(22)]
        await asyncio.sleep(0)
        try:
            self.assertEqual(gate.snapshot()["by_class"]["heartbeat"], 22)
            error = await self._capture(gate, time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS)
            self._assert_admission_error(error, "ADMISSION_QUEUE_FULL")
        finally:
            for task in pending:
                task.cancel()
            release.set()
            await asyncio.gather(*holders, *pending, return_exceptions=True)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

        gate = PlayerAdmission().heartbeat
        release, holders = await self._fill(gate)
        try:
            error = await self._capture(gate, time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS)
            self._assert_admission_error(error, "ADMISSION_WAIT_DEADLINE")
            self.assertEqual(gate.snapshot()["active"], 2)
        finally:
            release.set()
            await asyncio.gather(*holders)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

        for reason, ticks_factory in (
            ("ADMISSION_EXPIRED_HEAD", lambda deadline: [deadline + 1]),
            ("ADMISSION_EXPIRED_AT_GRANT", lambda deadline: [deadline - 1, deadline + 1]),
        ):
            with self.subTest(reason=reason):
                gate = PlayerAdmission().heartbeat
                release, holders = await self._fill(gate)
                deadline = time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS
                waiting = asyncio.create_task(self._capture(gate, deadline))
                await asyncio.sleep(0)
                ticks = ticks_factory(deadline)
                final_tick = ticks[-1]
                clock = SimpleNamespace(monotonic_ns=lambda: ticks.pop(0) if ticks else final_tick)
                try:
                    with patch.object(admission, "time", clock):
                        release.set()
                        await asyncio.gather(*holders)
                        error = await waiting
                    self._assert_admission_error(error, reason)
                finally:
                    release.set()
                    if not waiting.done():
                        waiting.cancel()
                    await asyncio.gather(*holders, waiting, return_exceptions=True)
                self.assertEqual(gate.snapshot()["active"], 0)
                self.assertEqual(gate.snapshot()["waiters"], 0)

        gate = PlayerAdmission().heartbeat
        deadline = time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS
        now = {"value": deadline - 1_000_000}
        clock = SimpleNamespace(monotonic_ns=lambda: now["value"])
        with patch.object(admission, "time", clock), patch.object(
            admission, "admission_grant_recorder", return_value=lambda _: now.update(value=deadline + 1)
        ):
            error = await self._capture(gate, deadline)
        self._assert_admission_error(error, "ADMISSION_EXPIRED_AFTER_GRANT")
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)

    async def test_each_known_heartbeat_reason_logs_once_after_cleanup_without_inputs(self):
        from app.core import player_admission as admission

        for reason, phase in admission._HEARTBEAT_503_PHASES.items():
            with self.subTest(reason=reason):
                controls = PlayerAdmission()
                error = HTTPException(503, {"synthetic": "do-not-log-this-body"}, headers={"X-QA": "preserved"})
                error._player_503_reason = reason
                with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
                    with self.assertRaises(HTTPException) as raised:
                        async with player_activity(self._request(controls)):
                            raise error
                self.assertIs(raised.exception, error)
                self.assertEqual(error.headers, {"X-QA": "preserved"})
                self.assertEqual(len(captured.records), 1)
                row = json.loads(captured.records[0].getMessage())
                self.assertEqual(set(row), {"event", "request_id", "heartbeat_503_reason", "status_code", "retry_after_present", "timestamp", "phase", "gate_state_after_cleanup",
                                           "pg_backend_pid", "db_transaction_id", "stage", "sqlstate", "exception_class"})
                self.assertIsNone(row["pg_backend_pid"])
                self.assertIsNone(row["db_transaction_id"])
                self.assertIsNone(row["sqlstate"])
                self.assertEqual(row["event"], "heartbeat_http_503")
                self.assertEqual(row["heartbeat_503_reason"], reason)
                self.assertEqual(row["phase"], phase)
                self._assert_metadata(row, retry_after_present=False)
                self.assertEqual(row["gate_state_after_cleanup"]["active"], 0)
                self.assertEqual(row["gate_state_after_cleanup"]["waiters"]["heartbeat"], 0)
                self.assertNotIn("do-not-log-this-body", captured.records[0].getMessage())

    async def test_unknown_or_untrusted_reason_is_sanitized_without_exception_body(self):
        from app.core import player_admission as admission

        for untrusted in ("unknown synthetic credential-like text", object(), None):
            with self.subTest(reason_type=type(untrusted).__name__):
                error = HTTPException(503, "synthetic-sensitive-detail")
                error._player_503_reason = untrusted
                controls = PlayerAdmission()
                with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
                    with self.assertRaises(HTTPException) as raised:
                        async with player_activity(self._request(controls)):
                            raise error
                self.assertIs(raised.exception, error)
                self.assertEqual(len(captured.records), 1)
                row = json.loads(captured.records[0].getMessage())
                self.assertEqual(row["heartbeat_503_reason"], "UNCLASSIFIED_HTTP_503")
                self.assertEqual(row["phase"], "unclassified")
                self._assert_metadata(row, retry_after_present=False)
                self.assertNotIn("synthetic-sensitive-detail", captured.records[0].getMessage())
                self.assertNotIn("unknown synthetic credential-like text", captured.records[0].getMessage())

    async def test_non_heartbeat_503_does_not_emit_heartbeat_reason_log(self):
        from app.core import player_admission as admission

        for path in ("runtime-config", "playback-events"):
            controls = PlayerAdmission()
            error = admission.overloaded("ADMISSION_QUEUE_FULL")
            with patch.object(admission.logging, "getLogger") as logger:
                with self.assertRaises(HTTPException) as raised:
                    async with player_activity(self._request(controls, path)):
                        raise error
            self.assertIs(raised.exception, error)
            logger.assert_not_called()
            self.assertEqual(controls.expensive.snapshot()["active"], 0)
            self.assertEqual(controls.heartbeat.snapshot()["active"], 0)
            self.assertEqual(controls.ingress.snapshot()["active"], 0)

    async def test_logging_failure_preserves_original_503_instead_of_500(self):
        from app.core import player_admission as admission

        controls = PlayerAdmission()
        error = admission.overloaded("ADMISSION_QUEUE_FULL")
        with patch.object(admission.logging, "getLogger") as logger:
            logger.return_value.warning.side_effect = RuntimeError("synthetic unavailable log sink")
            with self.assertRaises(HTTPException) as raised:
                async with player_activity(self._request(controls)):
                    raise error
        self.assertIs(raised.exception, error)
        self._assert_admission_error(raised.exception, "ADMISSION_QUEUE_FULL")
        self.assertEqual(controls.expensive.snapshot()["active"], 0)
        self.assertEqual(controls.heartbeat.snapshot()["active"], 0)
        self.assertEqual(controls.expensive.snapshot()["waiters"], 0)

    async def test_real_router_queue_503_logs_once_without_opt_in_diagnostics_or_database(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api.deps import get_current_player_device, get_db
        from app.api.routes.player import router
        from app.core import player_diagnostics

        app = FastAPI()
        controls = app.state.player_admission = PlayerAdmission()
        app.include_router(router, prefix="/qa-custom/player")
        calls = []
        request_uuid = str(uuid4())

        def forbidden_dependency():
            calls.append(True)
            raise AssertionError("Full admission must reject before auth or a database dependency")

        app.dependency_overrides[get_current_player_device] = forbidden_dependency
        app.dependency_overrides[get_db] = forbidden_dependency
        gate = controls.heartbeat
        release, holders = await self._fill(gate)
        pending = [asyncio.create_task(self._capture(gate, time.monotonic_ns() + 5_000_000_000)) for _ in range(22)]
        await asyncio.sleep(0)
        try:
            with patch.object(player_diagnostics, "ENABLED", False):
                with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
                    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                                           base_url="http://testserver") as client:
                        response = await client.post("/qa-custom/player/heartbeat", json={"status": "online"},
                                                     headers={"X-Request-ID": request_uuid})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "Player operation temporarily unavailable"})
            self.assertEqual(response.headers["Retry-After"], "1")
            self.assertEqual(calls, [])
            self.assertEqual(len(captured.records), 1)
            row = json.loads(captured.records[0].getMessage())
            self.assertEqual(row["heartbeat_503_reason"], "ADMISSION_QUEUE_FULL")
            self.assertEqual(row["phase"], "before_admission")
            self._assert_metadata(row, retry_after_present=True)
            self.assertEqual(row["request_id"], request_uuid)
            self.assertNotIn("x-request-id", response.headers)
            self.assertEqual(row["gate_state_after_cleanup"]["active"], 2)
            self.assertEqual(row["gate_state_after_cleanup"]["waiters"]["heartbeat"], 22)
        finally:
            for task in pending:
                task.cancel()
            release.set()
            await asyncio.gather(*holders, *pending, return_exceptions=True)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)




    async def test_legacy_overloaded_call_without_reason_preserves_original_503(self):
        from app.core import player_admission as admission

        error = admission.overloaded()
        self._assert_admission_error(error, "UNCLASSIFIED_HTTP_503")

    async def test_cancellation_after_grant_returns_the_owned_permit(self):
        from app.core import player_admission as admission

        gate = PlayerAdmission().heartbeat
        release, holders = await self._fill(gate)
        waiting = None
        try:
            with patch.object(admission, "admission_grant_recorder", return_value=lambda _: waiting.cancel()):
                waiting = asyncio.create_task(self._capture(
                    gate, time.monotonic_ns() + admission.HEARTBEAT_REQUEST_BUDGET_NS))
                await asyncio.sleep(0)
                self.assertEqual(gate.snapshot()["waiters"], 1)
                release.set()
                await asyncio.gather(*holders)
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
        finally:
            release.set()
            if waiting is not None and not waiting.done():
                waiting.cancel()
            await asyncio.gather(*holders, *((waiting,) if waiting is not None else ()), return_exceptions=True)
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiters"], 0)



    def _assert_metadata(self, row, *, retry_after_present):
        self.assertIs(type(row["status_code"]), int)
        self.assertEqual(row["status_code"], 503)
        self.assertIs(type(row["retry_after_present"]), bool)
        self.assertIs(row["retry_after_present"], retry_after_present)
        self.assertIs(type(row["request_id"]), str)
        self.assertEqual(str(UUID(row["request_id"])), row["request_id"])
        self.assertRegex(row["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLess(abs((datetime.now(UTC) - stamp).total_seconds()), 10)

    async def test_single_valid_request_uuid_is_normalized_without_logging_other_headers(self):
        controls = PlayerAdmission()
        request = self._request(controls)
        supplied = str(uuid4())
        request.scope["headers"] = [
            (b"X-Request-ID", supplied.upper().encode("ascii")),
            (b"authorization", b"Bearer qa-synthetic-sensitive-header-never-log"),
        ]
        error = HTTPException(503, "synthetic-sensitive-body", headers={"rEtRy-AfTeR": "synthetic-sensitive-header-value"})
        with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
            with self.assertRaises(HTTPException) as raised:
                async with player_activity(request):
                    raise error
        self.assertIs(raised.exception, error)
        self.assertEqual(len(captured.records), 1)
        row = json.loads(captured.records[0].getMessage())
        self._assert_metadata(row, retry_after_present=True)
        self.assertEqual(row["request_id"], supplied)
        for secret in ("qa-synthetic-sensitive-header-never-log", "synthetic-sensitive-header-value", "synthetic-sensitive-body"):
            self.assertNotIn(secret, captured.records[0].getMessage())

    async def test_missing_invalid_duplicate_and_nonascii_request_ids_generate_safe_uuid(self):
        supplied = str(uuid4())
        cases = (
            [],
            [(b"x-request-id", b"synthetic-not-a-uuid-never-log")],
            [(b"x-request-id", b"\xff" * 36)],
            [(b"x-request-id", supplied.encode("ascii")), (b"X-Request-ID", supplied.encode("ascii"))],
            [(b"x-request-id", (supplied + " ").encode("ascii"))],
        )
        generated = []
        for headers in cases:
            with self.subTest(header_count=len(headers)):
                request = self._request(PlayerAdmission())
                request.scope["headers"] = headers
                with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
                    with self.assertRaises(HTTPException):
                        async with player_activity(request):
                            raise HTTPException(503, "synthetic-do-not-log")
                self.assertEqual(len(captured.records), 1)
                row = json.loads(captured.records[0].getMessage())
                self._assert_metadata(row, retry_after_present=False)
                self.assertEqual(UUID(row["request_id"]).version, 4)
                self.assertNotEqual(row["request_id"], supplied)
                self.assertNotIn("synthetic-not-a-uuid-never-log", captured.records[0].getMessage())
                self.assertNotIn("synthetic-do-not-log", captured.records[0].getMessage())
                generated.append(row["request_id"])
        self.assertEqual(len(set(generated)), len(cases))

    async def test_existing_diagnostic_trace_uuid_is_reused_for_heartbeat_reason(self):
        from app.core import player_admission as admission

        trace_id, supplied = str(uuid4()), str(uuid4())
        request = self._request(PlayerAdmission())
        request.scope["headers"] = [(b"x-request-id", supplied.encode("ascii"))]
        token = admission._current_trace.set(SimpleNamespace(request_id=trace_id))
        try:
            with self.assertLogs("app.core.player_admission", level="WARNING") as captured:
                with self.assertRaises(HTTPException):
                    async with player_activity(request):
                        raise HTTPException(503, "synthetic")
        finally:
            admission._current_trace.reset(token)
        self.assertEqual(len(captured.records), 1)
        row = json.loads(captured.records[0].getMessage())
        self._assert_metadata(row, retry_after_present=False)
        self.assertEqual(row["request_id"], trace_id)




if __name__ == "__main__":
    unittest.main()
