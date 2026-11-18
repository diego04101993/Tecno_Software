"""Real HTTP durable acceptance; deliberately no legacy final-state wrapper."""
from contextlib import contextmanager
from datetime import UTC, datetime
import threading
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.routes import player
from app.core.player_admission import PlaybackRecoveryMiddleware, playback_worker_activity
from app.models.entities import PlayerDevice, PlayerPlaybackEvent, PlayerPlaybackInbox
from app.services import playback_inbox
from test_tenant_gate_performance import GateFixture


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PlaybackInboxHTTPTests(GateFixture):
    legacy_playback_settlement = False

    def headers(self):
        return {"Authorization": "Bearer " + self.rows["A"]["token"]}

    def body(self, *, foreign=False):
        refs = self.ids["B" if foreign else "A"]
        return dict(content_id=refs["content"], campaign_id=refs["campaign"], layout_id=refs["layout"],
                    started_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(), status="completed",
                    metadata_json={"synthetic": "real-http-unchanged"})

    def post(self, body=None, *, http=None):
        return (http or self.http).post("/api/player/playback-events", headers=self.headers(),
                                       json=self.body() if body is None else body)

    def count(self, model):
        with Session(self.engine) as db:
            return db.scalar(select(func.count()).select_from(model))

    def wait_for_event(self, identity, *, timeout=10):
        # Observe only. This test never calls a legacy settlement helper or
        # manually materializes a receipt in order to make assertions pass.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with Session(self.engine) as db:
                row = db.get(PlayerPlaybackEvent, identity)
                if row is not None:
                    db.expunge(row)
                    return row
                status = db.scalar(select(PlayerPlaybackInbox.status).where(PlayerPlaybackInbox.id == identity))
                self.assertNotEqual(status, "FAILED", "The real HTTP consumer retained a failed receipt")
            time.sleep(0.01)
        self.fail("The real consumer did not materialize the accepted HTTP receipt")

    @contextmanager
    def blocked_before_heavy_validation(self):
        started, release = threading.Event(), threading.Event()
        original = player._record_player_playback_event

        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic pre-validation barrier timed out")
            return original(*args, **kwargs)

        with patch.object(player, "_record_player_playback_event", blocked):
            try:
                yield started
            finally:
                release.set()

    def assert_event_matches_ack(self, event, ack):
        self.assertEqual(event.id, ack["event_id"])
        self.assertEqual(event.channel_id, ack["channel_id"])
        self.assertEqual(utc(event.created_at), utc(datetime.fromisoformat(ack["recorded_at"])))

    def test_http_201_is_durable_before_heavy_validation_and_final_event(self):
        with self.blocked_before_heavy_validation() as started:
            before = time.perf_counter()
            response = self.post()
            elapsed = time.perf_counter() - before
            self.assertEqual(response.status_code, 201, response.text)
            self.assertLess(elapsed, 1.0, "201 must not wait for the blocked heavy processor")
            ack = response.json()
            self.assertEqual(set(ack), {"event_id", "recorded_at", "channel_id"})
            self.assertTrue(started.wait(2))
            with Session(self.engine) as db:
                receipt = db.get(PlayerPlaybackInbox, ack["event_id"])
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt.status, "PENDING")
                self.assertEqual((receipt.player_device_id, receipt.channel_id, receipt.client_id, receipt.branch_id),
                                 tuple(self.ids["A"][key] for key in ("device", "channel", "client", "branch")))
                self.assertEqual(utc(receipt.accepted_at), utc(datetime.fromisoformat(ack["recorded_at"])))
                self.assertEqual(db.scalar(select(func.count()).select_from(PlayerPlaybackEvent)), 0)
        event = self.wait_for_event(ack["event_id"])
        self.assert_event_matches_ack(event, ack)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)

    def test_six_identical_http_submissions_remain_six_distinct_events(self):
        body = self.body()
        acknowledgments = []
        for _ in range(6):
            response = self.post(body)
            self.assertEqual(response.status_code, 201, response.text)
            acknowledgments.append(response.json())
        identities = {ack["event_id"] for ack in acknowledgments}
        self.assertEqual(len(identities), 6)
        for ack in acknowledgments:
            self.assert_event_matches_ack(self.wait_for_event(ack["event_id"]), ack)
        self.assertEqual(self.count(PlayerPlaybackEvent), 6)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)

    def test_capacity_rejection_is_http_503_without_ack_job_or_event(self):
        with patch.object(playback_inbox, "MAX_PENDING_COUNT", 0), patch.object(
                playback_inbox, "ensure_worker") as start:
            response = self.post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("event_id", response.json())
        start.assert_not_called()
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)

    def test_storage_commit_failure_is_http_503_and_rolls_back_receipt(self):
        original_commit = Session.commit

        def fail_receipt_commit(db):
            if any(isinstance(row, PlayerPlaybackInbox) for row in db.new):
                raise OperationalError("synthetic receipt commit", {}, Exception())
            return original_commit(db)

        with patch.object(Session, "commit", fail_receipt_commit), patch.object(
                playback_inbox, "ensure_worker") as start:
            response = self.post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("event_id", response.json())
        start.assert_not_called()
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def recovery_app(self, *, recover):
        app = FastAPI()
        app.include_router(player.router, prefix="/api/player")

        def database():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = database

        @app.get("/")
        def root():
            return {"status": "ok"}

        if recover:
            app.add_middleware(PlaybackRecoveryMiddleware, start=lambda: playback_inbox.ensure_worker(
                app, self.engine, player._record_player_playback_event,
                activity=lambda: playback_worker_activity(app)))
        return app

    def test_post_commit_wake_failure_recovers_on_new_app_get_without_resubmission(self):
        old_app = self.recovery_app(recover=False)
        with TestClient(old_app) as old_http:
            try:
                with patch.object(playback_inbox, "ensure_worker", side_effect=RuntimeError("synthetic wake failure")):
                    response = self.post(http=old_http)
                self.assertEqual(response.status_code, 201, response.text)
                ack = response.json()
                with Session(self.engine) as db:
                    self.assertIsNotNone(db.get(PlayerPlaybackInbox, ack["event_id"]))
                    self.assertIsNone(db.get(PlayerPlaybackEvent, ack["event_id"]))
            finally:
                old_http.portal.call(playback_inbox.stop_worker, old_app, self.engine)
        self.assertIsNone(old_http.portal)
        new_app = self.recovery_app(recover=True)
        with TestClient(new_app) as new_http:
            try:
                # This is the sole request made to the replacement application.
                response = new_http.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok"})
                self.assert_event_matches_ack(self.wait_for_event(ack["event_id"]), ack)
            finally:
                new_http.portal.call(playback_inbox.stop_worker, new_app, self.engine)
        self.assertEqual(self.count(PlayerPlaybackEvent), 1)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)

    def test_http_foreign_optional_ids_stay_private_until_final_sanitization(self):
        body = self.body(foreign=True)
        with self.blocked_before_heavy_validation() as started:
            response = self.post(body)
            self.assertEqual(response.status_code, 201, response.text)
            ack = response.json()
            self.assertTrue(started.wait(2))
            with Session(self.engine) as db:
                receipt = db.get(PlayerPlaybackInbox, ack["event_id"])
                self.assertEqual(receipt.payload_json["content_id"], self.ids["B"]["content"])
                self.assertEqual(receipt.payload_json["campaign_id"], self.ids["B"]["campaign"])
                self.assertEqual(receipt.payload_json["layout_id"], self.ids["B"]["layout"])
                self.assertEqual(receipt.payload_json["metadata_json"], body["metadata_json"])
                self.assertEqual(receipt.client_id, self.ids["A"]["client"])
                self.assertIsNone(db.get(PlayerPlaybackEvent, ack["event_id"]))
        event = self.wait_for_event(ack["event_id"])
        self.assert_event_matches_ack(event, ack)
        self.assertEqual((event.content_id, event.campaign_id, event.layout_id), (None, None, None))
        self.assertEqual(event.metadata_json, body["metadata_json"])
        self.assertEqual(event.client_id, self.ids["A"]["client"])

    def test_no_auth_and_disabled_player_keep_previous_401_without_enqueue(self):
        response = self.http.post("/api/player/playback-events", json=self.body())
        self.assertEqual(response.status_code, 401)
        with Session(self.engine) as db:
            db.get(PlayerDevice, self.ids["A"]["device"]).is_active = False
            db.commit()
        response = self.post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.count(PlayerPlaybackInbox), 0)
        self.assertEqual(self.count(PlayerPlaybackEvent), 0)
