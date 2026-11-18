"""Tests for legacy-only final-state synchronization, never production waits."""
import asyncio
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes import player
from app.models.entities import Channel, PlayerDevice, PlayerPlaybackEvent, PlayerPlaybackInbox
from app.schemas.domain import PlayerPlaybackEventCreate
from app.services import playback_inbox
from player_event_fixture import install_legacy_player_settlement, settle_legacy_player_event
from test_tenant_gate_performance import GateFixture


class LegacyPlayerEventSettlementTests(GateFixture):
    def accept(self):
        with Session(self.engine, autoflush=False) as db:
            return playback_inbox.enqueue(db, db.get(PlayerDevice, self.ids["A"]["device"]),
                    db.get(Channel, self.ids["A"]["channel"]),
                    PlayerPlaybackEventCreate(started_at=datetime.now(UTC), status="completed"))

    def test_legacy_http_fixture_materializes_with_real_processor_and_same_ack(self):
        b = self.ids["B"]
        with patch.object(playback_inbox, "ensure_worker", return_value=None), patch.object(
                playback_inbox, "materialize_one", wraps=playback_inbox.materialize_one) as process:
            response = self.http.post("/api/player/playback-events",
                headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
                json={"started_at": datetime.now(UTC).isoformat(), "content_id": b["content"],
                      "campaign_id": b["campaign"], "layout_id": b["layout"]})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {"event_id", "recorded_at", "channel_id"})
        self.assertGreaterEqual(process.call_count, 1)
        with Session(self.engine) as db:
            event = db.get(PlayerPlaybackEvent, response.json()["event_id"])
            self.assertIsNotNone(event)
            self.assertEqual((event.content_id, event.campaign_id, event.layout_id), (None, None, None))
            self.assertIsNone(db.get(PlayerPlaybackInbox, event.id))

    def test_failed_receipt_is_reported_and_never_repaired_by_fixture(self):
        ack = self.accept()
        with Session(self.engine) as db:
            row = db.get(PlayerPlaybackInbox, ack.event_id)
            row.status, row.last_error_code = "FAILED", "SYNTHETIC_FAILURE"
            db.commit()
        with patch.object(playback_inbox, "materialize_one") as process:
            with self.assertRaisesRegex(AssertionError, "FAILED"):
                settle_legacy_player_event(self, self.engine, ack.event_id)
            process.assert_not_called()
        with Session(self.engine) as db:
            self.assertEqual(db.get(PlayerPlaybackInbox, ack.event_id).status, "FAILED")

    def test_busy_materializer_retries_only_inside_bounded_fixture_wait(self):
        ack = self.accept()
        with patch.object(playback_inbox, "materialize_one", return_value=False) as process:
            with self.assertRaisesRegex(AssertionError, "internal deadline"):
                settle_legacy_player_event(self, self.engine, ack.event_id, timeout=0.02)
            self.assertGreaterEqual(process.call_count, 1)
        with Session(self.engine) as db:
            row = db.get(PlayerPlaybackInbox, ack.event_id)
            self.assertEqual((row.status, row.attempts, row.last_error_code), ("PENDING", 0, None))
            self.assertIsNone(db.get(PlayerPlaybackEvent, ack.event_id))

    def test_missing_durable_receipt_and_event_is_a_fixture_failure(self):
        with self.assertRaisesRegex(AssertionError, "neither its durable receipt"):
            settle_legacy_player_event(self, self.engine, str(uuid4()))

    def test_non_201_validation_response_is_not_settled(self):
        with patch.object(playback_inbox, "materialize_one") as process:
            response = self.http.post("/api/player/playback-events",
                headers={"Authorization": "Bearer " + self.rows["A"]["token"]}, json={})
            self.assertEqual(response.status_code, 422)
            process.assert_not_called()


class RawPlaybackAcceptanceFixtureTests(GateFixture):
    legacy_playback_settlement = False

    def test_opt_out_preserves_http_acceptance_before_materialization(self):
        with patch.object(playback_inbox, "ensure_worker", return_value=None), patch.object(
                playback_inbox, "materialize_one") as process:
            response = self.http.post("/api/player/playback-events",
                headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
                json={"started_at": datetime.now(UTC).isoformat()})
            self.assertEqual(response.status_code, 201)
            process.assert_not_called()
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(PlayerPlaybackInbox, response.json()["event_id"]))
            self.assertIsNone(db.get(PlayerPlaybackEvent, response.json()["event_id"]))


class PlayerFixtureLifecycleTests(TestCase):
    def test_stop_is_awaited_before_client_exit_and_engine_disposal(self):
        observations = []
        owner = TestCase()
        owner.legacy_playback_settlement = False
        owner.addCleanup(lambda: observations.append("engine_dispose"))

        class TracedClient(TestClient):
            def __exit__(self, *args):
                observations.append("client_exit")
                return super().__exit__(*args)

        async def stop(app, engine):
            await asyncio.sleep(0)
            observations.append("stop_worker")

        http = owner.enterContext(TracedClient(FastAPI()))
        with patch.object(playback_inbox, "stop_worker", stop):
            install_legacy_player_settlement(owner, http, object())
            self.assertTrue(owner.doCleanups())
        self.assertEqual(observations, ["stop_worker", "client_exit", "engine_dispose"])
