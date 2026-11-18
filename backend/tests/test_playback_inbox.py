"""Playback receipt service: isolated fixtures, real current tenant guards.

No production URL, app startup, migration, external service or Player client.
PostgreSQL locking/restart coverage belongs to the separately owned QA suite;
SQLite cases below do not claim distributed-lock or crash durability proof.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import contextvars
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import player
from app.models.entities import Branch, Channel, Client, PlayerDevice, PlayerPlaybackEvent, PlayerPlaybackInbox
from app.schemas.domain import PlayerPlaybackEventCreate
from app.services import playback_inbox as inbox
from test_tenant_gate_performance import GateFixture


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _InboxFixture:
    def payload(self, *, foreign=False, **overrides):
        refs = self.ids["B" if foreign else "A"]
        values = dict(content_id=refs["content"], campaign_id=refs["campaign"], layout_id=refs["layout"],
                      started_at=datetime(2026, 1, 1, tzinfo=UTC), status="completed",
                      metadata_json={"synthetic": "receipt-service"})
        values.update(overrides)
        return PlayerPlaybackEventCreate(**values)

    def accept(self, payload=None):
        with Session(self.engine, autoflush=False) as db:
            return inbox.enqueue(db, db.get(PlayerDevice, self.ids["A"]["device"]),
                                 db.get(Channel, self.ids["A"]["channel"]), payload or self.payload())

    def count(self, model):
        with Session(self.engine) as db:
            return db.scalar(select(func.count()).select_from(model))

    def receipt(self, identity):
        with Session(self.engine) as db:
            row = db.get(PlayerPlaybackInbox, identity)
            if row is not None:
                db.expunge(row)
            return row

    def ready_again(self, identity):
        with Session(self.engine) as db:
            db.execute(update(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id == identity).values(
                available_at=datetime.now(UTC) - timedelta(seconds=1)))
            db.commit()

    def process(self, processor=None):
        return inbox.materialize_one(self.engine, processor or player._record_player_playback_event)


class PlaybackInboxTests(_InboxFixture, GateFixture):
    def assert_deleted_target_is_retained_terminally(self, model, key):
        ack = self.accept()
        before = self.receipt(ack.event_id)
        with self.engine.begin() as connection:
            connection.execute(delete(model).where(model.id == self.ids["A"][key]))
        preserved = self.receipt(ack.event_id)
        self.assertIsNotNone(preserved)
        immutable = ("id", "player_device_id", "channel_id", "client_id", "branch_id",
                     "payload_json", "payload_bytes", "accepted_at")
        self.assertEqual(tuple(getattr(before, f) for f in immutable),
                         tuple(getattr(preserved, f) for f in immutable))
        self.assertEqual(preserved.status, "PENDING")
        self.assertTrue(self.process())
        terminal = self.receipt(ack.event_id)
        self.assertEqual((terminal.status, terminal.attempts, terminal.last_error_code),
                         ("FAILED", 1, "TARGET_DELETED"))
        self.assertEqual(tuple(getattr(before, f) for f in immutable),
                         tuple(getattr(terminal, f) for f in immutable))
        self.assertEqual(terminal.available_at, before.available_at)
        self.assertFalse(self.process(), "A deleted target must not loop/retry forever")
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_deleted_device_keeps_durable_historical_receipt(self):
        self.assert_deleted_target_is_retained_terminally(PlayerDevice, "device")

    def test_deleted_channel_keeps_durable_historical_receipt(self):
        self.assert_deleted_target_is_retained_terminally(Channel, "channel")

    def test_deleted_branch_keeps_durable_historical_receipt(self):
        self.assert_deleted_target_is_retained_terminally(Branch, "branch")

    def test_deleted_client_keeps_durable_historical_receipt(self):
        self.assert_deleted_target_is_retained_terminally(Client, "client")

    def test_scope_lock_retry_releases_transaction_and_accepts_only_once(self):
        original = inbox.player_write_guard
        attempts, slept, identities = [], [], []

        @contextmanager
        def busy_once(db, **kwargs):
            identities.append(kwargs["intents"][1].reference.identity)
            attempts.append(True)
            if len(attempts) == 1:
                raise inbox.PlayerPlaybackReceiptBusy()
            with original(db, **kwargs) as plan:
                yield plan

        with patch.object(inbox, "player_write_guard", busy_once), patch.object(
                inbox.time, "sleep", side_effect=lambda delay: slept.append((delay, self.engine.pool.checkedout()))):
            ack = self.accept()
        self.assertEqual(slept, [(.01, 0)])
        self.assertEqual(identities, [ack.event_id, ack.event_id])
        self.assertEqual(self.count(PlayerPlaybackInbox), 1)
        self.assertEqual(self.receipt(ack.event_id).status, "PENDING")
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_scope_lock_retry_is_finite_and_never_acknowledges_without_commit(self):
        with patch.object(inbox, "player_write_guard", side_effect=inbox.PlayerPlaybackReceiptBusy()) as busy, patch.object(
                inbox.time, "sleep") as sleep:
            with self.assertRaises(HTTPException) as caught:
                self.accept()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(busy.call_count, 9)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.01, .02, .04, .08, .16, .32, .64, 1.28])
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_scope_lock_retry_never_authenticates_against_a_rotated_token(self):
        def rotate(_delay):
            self.assertEqual(self.engine.pool.checkedout(), 0)
            with Session(self.engine) as db:
                db.get(PlayerDevice, self.ids["A"]["device"]).player_token_hash = "0" * 64
                db.commit()

        with patch.object(inbox, "player_write_guard", side_effect=inbox.PlayerPlaybackReceiptBusy()) as busy, patch.object(
                inbox.time, "sleep", side_effect=rotate):
            with self.assertRaises(HTTPException) as caught:
                self.accept()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(busy.call_count, 1)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_deleted_target_during_scope_lock_retry_is_rejected_without_receipt(self):
        def remove(_delay):
            with self.engine.begin() as connection:
                connection.execute(delete(Channel).where(Channel.id == self.ids["A"]["channel"]))

        with patch.object(inbox, "player_write_guard", side_effect=inbox.PlayerPlaybackReceiptBusy()) as busy, patch.object(
                inbox.time, "sleep", side_effect=remove):
            with self.assertRaises(HTTPException) as caught:
                self.accept()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(busy.call_count, 1)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_storage_failure_during_retry_reload_stays_503_without_ack(self):
        get = Session.get
        failed = []

        def reload(db, model, identity, **kwargs):
            if kwargs.get("populate_existing") and model is PlayerDevice:
                failed.append(True)
                raise OperationalError("synthetic retry reload", {}, Exception())
            return get(db, model, identity, **kwargs)

        with patch.object(inbox, "player_write_guard", side_effect=inbox.PlayerPlaybackReceiptBusy()), patch.object(
                inbox.time, "sleep"), patch.object(Session, "get", reload):
            with self.assertRaises(HTTPException) as caught:
                self.accept()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(failed, [True])
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_busy_processor_retains_receipt_and_existing_bounded_retry_policy(self):
        ack = self.accept()

        def locked(*args, **kwargs):
            raise inbox.PlayerPlaybackReceiptBusy()

        self.assertTrue(self.process(locked))
        row = self.receipt(ack.event_id)
        self.assertEqual((row.status, row.attempts, row.last_error_code),
                         ("PENDING", 1, "TARGET_TEMPORARILY_LOCKED"))
        self.ready_again(ack.event_id)
        self.assertTrue(self.process())
        self.assertIsNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)

    def test_ack_follows_committed_receipt_without_final_event(self):
        with Session(self.engine, autoflush=False) as db:
            committed = []
            real_commit = db.commit

            def commit():
                real_commit()
                committed.append(True)

            with patch.object(db, "commit", commit):
                ack = inbox.enqueue(db, db.get(PlayerDevice, self.ids["A"]["device"]),
                                    db.get(Channel, self.ids["A"]["channel"]), self.payload())
            self.assertEqual(committed, [True])
        row = self.receipt(ack.event_id)
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(row.attempts, 0)
        self.assertEqual(utc(row.accepted_at), utc(ack.recorded_at))
        self.assertEqual((row.player_device_id, row.channel_id, row.client_id, row.branch_id),
                         tuple(self.ids["A"][key] for key in ("device", "channel", "client", "branch")))
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_failed_acceptance_commit_never_returns_ack_or_half_receipt(self):
        with Session(self.engine, autoflush=False) as db:
            device, channel = db.get(PlayerDevice, self.ids["A"]["device"]), db.get(Channel, self.ids["A"]["channel"])
            with patch.object(db, "commit", side_effect=OperationalError("synthetic", {}, Exception())):
                with self.assertRaises(HTTPException) as caught:
                    inbox.enqueue(db, device, channel, self.payload())
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_capacity_counts_failed_receipts_and_does_not_overwrite(self):
        with patch.object(inbox, "MAX_PENDING_COUNT", 2):
            first, second = self.accept(), self.accept()
            with Session(self.engine) as db:
                db.execute(update(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id == first.event_id).values(status="FAILED"))
                db.commit()
            with self.assertRaises(HTTPException) as caught:
                self.accept()
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.count(PlayerPlaybackInbox), 2)
        self.assertEqual(self.receipt(first.event_id).status, "FAILED")
        self.assertIsNotNone(self.receipt(second.event_id))

    def test_byte_budget_accepts_exact_limit_and_rejects_next_receipt(self):
        weight = inbox._payload_json(self.payload())[2]
        with patch.object(inbox, "MAX_PENDING_BYTES", weight):
            first = self.accept()
            with self.assertRaises(HTTPException) as caught:
                self.accept()
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.receipt(first.event_id).payload_bytes, weight)
        self.assertEqual(self.count(PlayerPlaybackInbox), 1)

    def test_payload_is_a_private_immutable_copy_and_raw_optional_ids_are_not_owners(self):
        payload = self.payload(foreign=True)
        ack = self.accept(payload)
        payload.metadata_json["synthetic"] = "mutated-after-acceptance"
        row = self.receipt(ack.event_id)
        self.assertEqual(row.payload_json["content_id"], self.ids["B"]["content"])
        self.assertEqual(row.payload_json["metadata_json"], {"synthetic": "receipt-service"})
        self.assertEqual(row.client_id, self.ids["A"]["client"])
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertTrue(self.process())
        with Session(self.engine) as db:
            event = db.get(PlayerPlaybackEvent, ack.event_id)
            self.assertEqual((event.content_id, event.campaign_id, event.layout_id), (None, None, None))
            self.assertEqual(event.client_id, self.ids["A"]["client"])

    def test_identical_http_submissions_remain_distinct_and_one_receipt_replay_is_safe(self):
        acks = [self.accept(self.payload()) for _ in range(6)]
        self.assertEqual(len({ack.event_id for ack in acks}), 6)
        for _ in acks:
            self.assertTrue(self.process())
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 6)
        with Session(self.engine) as db:
            for ack in acks:
                self.assertEqual(utc(db.get(PlayerPlaybackEvent, ack.event_id).created_at), utc(ack.recorded_at))
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_rollback_after_claim_restores_receipt_and_creates_no_final_event(self):
        ack = self.accept()

        def fail_after_claim(payload, device, db, **kwargs):
            claim = kwargs["before_commit"]

            def fail(plan):
                claim(plan)
                raise OperationalError("synthetic", {}, Exception())

            return player._record_player_playback_event(payload, device, db, **dict(kwargs, before_commit=fail))

        self.assertTrue(self.process(fail_after_claim))
        row = self.receipt(ack.event_id)
        self.assertEqual((row.status, row.attempts), ("PENDING", 1))
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.ready_again(ack.event_id)
        self.assertTrue(self.process())
        self.assertIsNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)

    def test_final_commit_failure_rolls_back_both_insert_and_receipt_delete(self):
        ack = self.accept()
        real_commit = Session.commit

        def fail_final_commit(db):
            if any(isinstance(row, PlayerPlaybackEvent) for row in db.new):
                raise OperationalError("synthetic", {}, Exception())
            return real_commit(db)

        with patch.object(Session, "commit", fail_final_commit):
            self.assertTrue(self.process())
        self.assertIsNotNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.ready_again(ack.event_id)
        self.assertTrue(self.process())
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)

    def test_disabled_device_retains_failed_receipt_and_never_inserts_event(self):
        ack = self.accept()
        with Session(self.engine) as db:
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(is_active=False))
            db.commit()
        self.assertTrue(self.process())
        self.assertEqual(self.receipt(ack.event_id).status, "FAILED")
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertFalse(self.process())

    def test_original_scope_is_not_rewritten_to_a_different_branch(self):
        ack = self.accept()
        with Session(self.engine) as db:
            # Deliberately corrupt the isolated fixture to model out-of-band SQL.
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                branch_id=self.ids["B"]["branch"]))
            db.commit()
        self.assertTrue(self.process())
        row = self.receipt(ack.event_id)
        self.assertEqual((row.status, row.branch_id), ("FAILED", self.ids["A"]["branch"]))
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_token_rotation_does_not_revoke_an_already_authenticated_receipt(self):
        ack = self.accept()
        with Session(self.engine) as db:
            db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"]).values(
                player_token_hash="0" * 64))
            db.commit()
        self.assertTrue(self.process())
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertIsNone(self.receipt(ack.event_id))

    def test_five_transient_failures_are_retained_without_silent_eviction(self):
        ack = self.accept()

        def transient(*args, **kwargs):
            raise OperationalError("synthetic", {}, Exception())

        for attempt in range(1, 6):
            self.assertTrue(self.process(transient))
            row = self.receipt(ack.event_id)
            self.assertEqual(row.attempts, attempt)
            self.assertEqual(row.status, "FAILED" if attempt == 5 else "PENDING")
            if attempt < 5:
                self.assertGreater(utc(row.available_at), datetime.now(UTC))
                self.assertFalse(self.process(transient))
                self.ready_again(ack.event_id)
        self.assertEqual(self.count(PlayerPlaybackInbox), 1)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertFalse(self.process(transient))

    def test_frozen_receipt_change_is_rejected_and_rolled_back(self):
        ack = self.accept()
        original = self.receipt(ack.event_id)

        def corrupt_before_claim(payload, device, db, **kwargs):
            claim = kwargs["before_commit"]

            def corrupt(plan):
                db.execute(update(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id == ack.event_id).values(
                    payload_bytes=original.payload_bytes + 1))
                claim(plan)

            return player._record_player_playback_event(payload, device, db, **dict(kwargs, before_commit=corrupt))

        self.assertTrue(self.process(corrupt_before_claim))
        row = self.receipt(ack.event_id)
        self.assertEqual(row.status, "FAILED")
        self.assertEqual(row.payload_bytes, original.payload_bytes)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_connection_pool_disposal_does_not_erase_committed_receipt(self):
        ack = self.accept()
        self.db.close()
        self.engine.dispose()
        self.assertTrue(self.process())
        self.assertIsNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_snapshot_contains_only_aggregate_counts_and_byte_age_metrics(self):
        ack = self.accept()
        with Session(self.engine) as db:
            report = inbox.snapshot(db)
        self.assertEqual(report["pending_count"], 1)
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["pending_payload_bytes"], self.receipt(ack.event_id).payload_bytes)
        self.assertTrue(all(value is None or isinstance(value, (int, float)) for value in report.values()))


class PlaybackInboxWorkerTests(_InboxFixture, GateFixture, unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Synthetic worker did not reach its expected state")

    async def test_lazy_worker_is_singleton_and_has_clean_context(self):
        ack = self.accept()
        app = FastAPI()
        marker = contextvars.ContextVar("synthetic_http_context", default=None)
        observed = []

        def processor(*args, **kwargs):
            observed.append(marker.get())
            return player._record_player_playback_event(*args, **kwargs)

        token = marker.set("synthetic-request-only")
        try:
            first = inbox.ensure_worker(app, self.engine, processor)
            self.assertIs(first, inbox.ensure_worker(app, self.engine, processor))
            await self.wait_for(lambda: self.count(PlayerPlaybackInbox) == 0)
        finally:
            marker.reset(token)
            await inbox.stop_worker(app, self.engine)
        self.assertEqual(observed, [None])
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)


        self.assertIsNone(self.receipt(ack.event_id))

    async def test_heavy_gate_is_entered_before_consumer_opens_db(self):
        self.accept()
        app = FastAPI()
        entered, release = asyncio.Event(), asyncio.Event()
        called = []
        real_materialize = inbox.materialize_one

        @asynccontextmanager
        async def activity():
            entered.set()
            await release.wait()
            yield

        def materialize(*args):
            called.append(True)
            return real_materialize(*args)

        with patch.object(inbox, "materialize_one", materialize):
            inbox.ensure_worker(app, self.engine, player._record_player_playback_event, activity=activity)
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                self.assertEqual(called, [])
                self.assertEqual(self.engine.pool.checkedout(), 0)
                release.set()
                await self.wait_for(lambda: self.count(PlayerPlaybackInbox) == 0)
            finally:
                release.set()
                await inbox.stop_worker(app, self.engine)
        self.assertTrue(called)

    async def test_worker_cancel_waits_for_atomic_processing_and_receipt_is_not_lost(self):
        self.accept()
        app = FastAPI()
        started, release = threading.Event(), threading.Event()

        def processor(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic test barrier timed out")
            return player._record_player_playback_event(*args, **kwargs)

        task = inbox.ensure_worker(app, self.engine, processor)
        try:
            await self.wait_for(started.is_set)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "Cancellation must await its running SQL ownership")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await inbox.stop_worker(app, self.engine)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)


    async def test_parent_and_child_cancellation_waits_for_owned_transaction(self):
        self.accept()
        app = FastAPI()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        children = []
        pool_at_gate_release = []
        real_run_sync = inbox.anyio.to_thread.run_sync

        @asynccontextmanager
        async def activity():
            try:
                yield
            finally:
                pool_at_gate_release.append(self.engine.pool.checkedout())

        async def track_child(*args, **kwargs):
            children.append(asyncio.current_task())
            return await real_run_sync(*args, **kwargs)

        def processor(*args, **kwargs):
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("Synthetic double-cancellation barrier timed out")
                return player._record_player_playback_event(*args, **kwargs)
            finally:
                finished.set()

        with patch.object(inbox.anyio.to_thread, "run_sync", track_child):
            task = inbox.ensure_worker(app, self.engine, processor, activity=activity)
            try:
                await self.wait_for(started.is_set)
                self.assertEqual(len(children), 1)
                task.cancel()
                children[0].cancel()
                await asyncio.sleep(.05)
                self.assertFalse(task.done(), "Owned SQL thread must outlive neither canceled task")
            finally:
                release.set()
                await self.wait_for(finished.is_set)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await inbox.stop_worker(app, self.engine)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)
        self.assertEqual(pool_at_gate_release, [0], "Heavy admission must outlive its owned Session")


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Verified owned local PostgreSQL QA required")
class PlaybackInboxPostgresTests(PlaybackInboxTests):
    """Same receipt semantics plus real cross-connection advisory-lock cases."""
    def make_engine(self):
        from test_player_connection_pool import PlayerConnectionPoolTests
        return PlayerConnectionPoolTests.make_engine(self)

    def parallel_engine(self):
        with self.engine.connect() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
        # The reused fixture already checked local hostname, dedicated QA DB,
        # system identifier and marker before creating this UUID-owned schema.
        self.assertTrue(schema.startswith("player_pool_"))
        value = create_engine(self.engine.url, pool_size=4, max_overflow=0, hide_parameters=True,
                              connect_args={"options": "-csearch_path=" + schema})
        self.addCleanup(value.dispose)
        return value

    def test_concurrent_capacity_is_serialized_across_connections(self):
        engine = self.parallel_engine()

        def accept_one(_):
            with Session(engine, autoflush=False) as db:
                try:
                    inbox.enqueue(db, db.get(PlayerDevice, self.ids["A"]["device"]),
                                  db.get(Channel, self.ids["A"]["channel"]), self.payload())
                    return 201
                except HTTPException as exc:
                    return exc.status_code

        with patch.object(inbox, "MAX_PENDING_COUNT", 4), ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(accept_one, range(10)))
        self.assertEqual(statuses.count(201), 4)
        self.assertEqual(statuses.count(503), 6)
        self.assertEqual(self.count(PlayerPlaybackInbox), 4)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(engine.pool.checkedout(), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_only_one_materializer_runs_across_engines(self):
        self.accept()
        engine = self.parallel_engine()
        entered, release = threading.Event(), threading.Event()

        def processor(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic materializer barrier timed out")
            return player._record_player_playback_event(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(inbox.materialize_one, self.engine, processor)
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(inbox.materialize_one(engine, player._record_player_playback_event))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=10))
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(engine.pool.checkedout(), 0)

    def test_forced_consumer_process_death_preserves_receipt_and_restarts_once(self):
        """SIGKILL on Linux; TerminateProcess on Windows. No app startup."""
        ack = self.accept()
        with self.engine.connect() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
        marker = self.root / "consumer-claimed.marker"
        child = r'''
import os, re, time
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from app.api.routes import player
from app.services.playback_inbox import materialize_one

url = make_url(os.environ["PLAYBACK_INBOX_QA_URL"])
schema = os.environ["PLAYBACK_INBOX_QA_SCHEMA"]
assert url.get_backend_name() == "postgresql"
assert url.host in {"127.0.0.1", "localhost"} and url.database == "security_r5_qa"
assert not url.query.get("options") and re.fullmatch(r"player_pool_[0-9a-f]{32}", schema)
engine = create_engine(url, pool_size=1, max_overflow=0, hide_parameters=True,
                       connect_args={"options": "-csearch_path=" + schema})
with engine.connect() as connection:
    assert connection.scalar(text("SELECT current_database()")) == "security_r5_qa"
    assert connection.scalar(text("SELECT current_setting('vwflex.qa_worker', true)")) == os.environ["QA_EXPECT_PG_MARKER"]
    assert connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")) == os.environ["SECURITY_R5_PG_SYSTEM_IDENTIFIER"]

def hold_after_claim(payload, device, db, **kwargs):
    claim = kwargs["before_commit"]
    def hold(plan):
        claim(plan)
        Path(os.environ["PLAYBACK_INBOX_QA_CLAIM_MARKER"]).write_text("claimed", encoding="ascii")
        while True:
            time.sleep(.1)
    return player._record_player_playback_event(payload, device, db, **dict(kwargs, before_commit=hold))

materialize_one(engine, hold_after_claim)
raise RuntimeError("Synthetic child unexpectedly completed before forced process death")
'''
        environment = dict(os.environ,
                           PLAYBACK_INBOX_QA_URL=self.engine.url.render_as_string(hide_password=False),
                           PLAYBACK_INBOX_QA_SCHEMA=schema,
                           PLAYBACK_INBOX_QA_CLAIM_MARKER=str(marker),
                           MEDIA_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1",
                           PYTHONPATH=os.pathsep.join(filter(None, (
                               str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH")))))
        process = subprocess.Popen([sys.executable, "-B", "-c", child], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 12
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "Owned QA child did not reach its pre-COMMIT receipt claim")
            process.kill()
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            if os.name != "nt":
                import signal
                self.assertEqual(process.returncode, -signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertIsNotNone(self.receipt(ack.event_id))
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertTrue(self.process())
        self.assertFalse(self.process())
        self.assertIsNone(self.receipt(ack.event_id))
        with Session(self.engine) as db:
            event = db.get(PlayerPlaybackEvent, ack.event_id)
            self.assertIsNotNone(event)
            self.assertEqual(utc(event.created_at), utc(ack.recorded_at))
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)
