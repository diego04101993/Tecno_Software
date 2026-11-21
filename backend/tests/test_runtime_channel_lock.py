"""Runtime root contention: real PostgreSQL locks, fresh scope, unchanged payload."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.api.routes import player
from app.models.entities import Branch, Channel, Client, PlayerDevice
from app.services import runtime_activity as activity
from app.services.player_runtime import build_player_runtime_from_snapshot
from app.services.tenant_mutations import MutationIntent, PlayerRuntimeRootBusy, player_write_guard
from app.services.tenant_ownership import RowRef
import test_heartbeat_inbox as heartbeat_cases
from test_tenant_gate_performance import GateFixture


class RuntimeActivityUnitTests(GateFixture):
    def test_normal_runtime_shape_and_last_seen_are_preserved(self):
        response = self.http.get("/api/player/runtime-config",
            headers={"Authorization": "Bearer " + self.rows["A"]["token"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["runtime_version"], 3)
        self.assertEqual(response.json()["channel"]["id"], self.ids["A"]["channel"])
        self.assertNotIn("expected_player_identity", response.json())
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)

    def test_no_retry_after_guard_yield_or_uncertain_commit(self):
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            error = PlayerRuntimeRootBusy(RowRef("Channel", channel.id), "share")
            with patch.object(db, "commit", side_effect=error) as commit:
                with self.assertRaises(PlayerRuntimeRootBusy):
                    activity.record_runtime_activity(db, device, channel)
            self.assertEqual(commit.call_count, 1)
            db.rollback()

    def test_admin_snapshot_call_without_player_contract_is_unchanged(self):
        with Session(self.engine) as db:
            channel = db.get(Channel, self.ids["A"]["channel"])
            result = build_player_runtime_from_snapshot(channel, db, "http://qa.invalid")
            self.assertEqual(result["runtime_version"], 3)
            self.assertEqual(result["channel"]["id"], channel.id)

    def test_snapshot_rejects_deleted_expired_orm_instead_of_http500(self):
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            channel, expected = activity.record_runtime_activity(db, device, channel)
            with Session(self.engine) as deleter:
                deleter.execute(delete(Channel).where(Channel.id == self.ids["A"]["channel"]))
                deleter.commit()
            with self.assertRaises(HTTPException) as rejected:
                build_player_runtime_from_snapshot(channel, db, "http://qa.invalid",
                                                    expected_player_identity=expected)
            self.assertEqual(rejected.exception.status_code, 401)


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned isolated PostgreSQL QA required")
class RuntimeChannelLockPostgresTests(GateFixture):
    make_engine = heartbeat_cases.HeartbeatInboxPostgresTests.make_engine

    def request_runtime(self):
        started = time.monotonic()
        response = self.http.get("/api/player/runtime-config",
            headers={"Authorization": "Bearer " + self.rows["A"]["token"]})
        return response, time.monotonic() - started

    @contextmanager
    def waiting_runtime(self, holder_model=Channel, holder_key="channel"):
        waiting = threading.Event()
        original = activity._set_wait_budget

        def observe(db, deadline):
            original(db, deadline)
            waiting.set()

        with Session(self.engine) as holder, ThreadPoolExecutor(max_workers=1) as pool:
            holder.scalar(select(holder_model.id).where(holder_model.id == self.ids["A"][holder_key])
                          .with_for_update())
            with patch.object(activity, "_set_wait_budget", side_effect=observe):
                future = pool.submit(self.request_runtime)
                self.assertTrue(waiting.wait(3), "Runtime never released partial locks for its exact root wait")
                try:
                    yield holder, future
                finally:
                    holder.rollback()
                    future.result(timeout=8)

    def assert_result(self, future, status=200):
        response, elapsed = future.result(timeout=8)
        self.assertEqual(response.status_code, status, response.text)
        self.assertLess(elapsed, 8)
        if status == 200:
            self.assertEqual(response.json()["runtime_version"], 3)
            self.assertEqual(response.json()["channel"]["id"], self.ids["A"]["channel"])
        return response

    def test_short_locks_on_all_four_roots_recover_without_http503(self):
        for model, key in ((Channel, "channel"), (Client, "client"),
                           (Branch, "branch"), (PlayerDevice, "device")):
            with self.subTest(root=model.__name__):
                with self.waiting_runtime(model, key) as (holder, future):
                    holder.rollback()
                    self.assert_result(future)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_blue_device_then_channel_order_releases_all_partial_root_and_tenant_locks(self):
        with self.waiting_runtime(PlayerDevice, "device") as (blue, future):
            # The previous blocking guard retains Channel SHARE and its tenant
            # advisory lock while waiting for BLUE's Device, creating a cycle.
            blue.scalar(select(Channel.id).where(Channel.id == self.ids["A"]["channel"])
                        .with_for_update(nowait=True))
            digest = sha256(("tecno.tenant.write.v1:" + self.ids["A"]["client"]).encode()).digest()[:8]
            key = int.from_bytes(digest, "big", signed=True)
            with Session(self.engine) as check:
                self.assertTrue(check.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}))
                check.rollback()
            blue.commit()
            self.assert_result(future)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_channel_name_update_wins_then_fresh_runtime_observes_it(self):
        with self.waiting_runtime() as (holder, future):
            holder.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"])
                           .values(name="Updated under deliberate lock"))
            holder.commit()
            response = self.assert_result(future)
            self.assertEqual(response.json()["channel"]["name"], "Updated under deliberate lock")

    def test_channel_delete_wins_no_runtime_or_liveness_write(self):
        with self.waiting_runtime() as (holder, future):
            holder.execute(delete(Channel).where(Channel.id == self.ids["A"]["channel"]))
            holder.commit()
            # Channel deletion cascades to its Device: the original bearer
            # no longer identifies an active Player after the wait.
            self.assert_result(future, 401)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]))

    def test_client_and_branch_deactivation_win_fresh_revalidation(self):
        for model, key, change in ((Client, "client", {"status": "inactive"}),
                                   (Branch, "branch", {"is_active": False})):
            with self.subTest(root=model.__name__):
                with self.waiting_runtime() as (holder, future):
                    holder.execute(update(model).where(model.id == self.ids["A"][key]).values(**change))
                    holder.commit()
                    self.assert_result(future, 403)
                with Session(self.engine) as restore:
                    restore.execute(update(model).where(model.id == self.ids["A"][key])
                                    .values(**({"status": "active"} if model is Client else {"is_active": True})))
                    restore.commit()
        with Session(self.engine) as db:
            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)

    def test_token_revocation_and_device_deactivation_win_revalidation(self):
        for change in ({"player_token_hash": "f" * 64}, {"is_active": False}):
            with self.subTest(field=next(iter(change))):
                with self.waiting_runtime() as (holder, future):
                    holder.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"])
                                   .values(**change))
                    holder.commit()
                    self.assert_result(future, 401)
                with Session(self.engine) as restore:
                    restore.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"])
                        .values(is_active=True, player_token_hash=self.rows["A"]["device"].player_token_hash))
                    restore.commit()

    def test_reassignment_to_foreign_branch_cannot_return_foreign_runtime(self):
        with self.waiting_runtime() as (holder, future):
            holder.execute(update(Channel).where(Channel.id == self.ids["A"]["channel"])
                           .values(branch_id=self.ids["B"]["branch"]))
            holder.commit()
            response = self.assert_result(future, 404)
            self.assertNotIn(self.ids["B"]["branch"], response.text)

    def test_prolonged_lock_is_bounded_and_does_not_update_liveness(self):
        with patch.object(activity, "RUNTIME_ROOT_WAIT_BUDGET_NS", 150_000_000):
            with self.waiting_runtime() as (_holder, future):
                response, elapsed = future.result(timeout=3)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertLess(elapsed, 1)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_snapshot_revalidates_mutation_committed_after_activity_before_builder(self):
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            channel, expected = activity.record_runtime_activity(db, device, channel)
            with Session(self.engine) as mutator:
                mutator.execute(update(Branch).where(Branch.id == self.ids["A"]["branch"])
                                .values(is_active=False))
                mutator.commit()
            with self.assertRaises(HTTPException) as rejected:
                build_player_runtime_from_snapshot(channel, db, "http://qa.invalid",
                                                    expected_player_identity=expected)
            self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    test_snapshot_rejects_deleted_expired_orm_instead_of_http500 = (
        RuntimeActivityUnitTests.test_snapshot_rejects_deleted_expired_orm_instead_of_http500)


if __name__ == "__main__":
    unittest.main()
