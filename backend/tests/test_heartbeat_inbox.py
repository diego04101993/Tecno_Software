"""Heartbeat durable acceptance and derived processing in isolated owned data.

The HTTP hard-kill/restart PostgreSQL gate is an external real-process QA test.
SQLite/process-reopen and worker-recreation cases here do not replace that gate.
No application startup, production URL, Player client or playback modification.
"""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, delete, func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import player
from app.models.entities import (
    Branch, CampaignChannelAssignment, Channel, ChannelConnectionStatus, Client,
    PlayerDevice, PlayerHeartbeat, PlayerHeartbeatInbox,
)
from app.schemas.domain import PlayerHeartbeatCreate
from app.services import heartbeat_inbox as inbox
from app.services import heartbeat_acceptance as acceptance
from test_tenant_gate_performance import GateFixture


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _HeartbeatInboxCases:
    def payload(self, **changes):
        a = self.ids["A"]
        values = dict(status="online", current_content_id=a["content"],
                      current_campaign_id=a["campaign"], current_layout_id=a["layout"],
                      app_version="durable-heartbeat-synthetic",
                      resolution={"width": 1920, "height": 1080}, mode="normal",
                      cache_status={"synthetic": "raw-cache"}, errors=[{"synthetic": "raw-error"}],
                      local_time=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                      playback_position={"position": 19})
        values.update(changes)
        return PlayerHeartbeatCreate(**values)

    def accept(self, payload=None):
        with Session(self.engine, autoflush=False) as db:
            before = set(db.scalars(select(PlayerHeartbeatInbox.id)))
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            ack = inbox.enqueue(db, device, channel, payload or self.payload())
        with Session(self.engine) as db:
            new_ids = set(db.scalars(select(PlayerHeartbeatInbox.id))) - before
        self.assertEqual(len(new_ids), 1, "Every accepted submission needs its own committed receipt")
        return ack, new_ids.pop()

    def count(self, model):
        with Session(self.engine) as db:
            return db.scalar(select(func.count()).select_from(model))

    def receipt(self, identity):
        with Session(self.engine) as db:
            row = db.get(PlayerHeartbeatInbox, identity)
            if row is not None:
                db.expunge(row)
            return row

    def history(self, identity):
        with Session(self.engine) as db:
            row = db.get(PlayerHeartbeat, identity)
            if row is not None:
                db.expunge(row)
            return row

    def process(self, processor=None):
        return inbox.materialize_one(self.engine, processor or player._materialize_player_heartbeat)

    def ready_again(self, identity):
        with Session(self.engine) as db:
            db.execute(update(PlayerHeartbeatInbox).where(PlayerHeartbeatInbox.id == identity).values(
                available_at=datetime.now(UTC) - timedelta(seconds=1)))
            db.commit()

    def immutable(self, row):
        return (row.id, row.player_device_id, row.channel_id, row.client_id, row.branch_id,
                row.payload_json, row.payload_bytes, utc(row.accepted_at))

    def certificate(self, db):
        now = datetime.now(UTC)
        a = self.ids["A"]
        payload, _, payload_bytes = inbox._payload_json(self.payload())
        return dict(device=db.get(PlayerDevice, a["device"]), channel=db.get(Channel, a["channel"]),
            channel_values={"status": ChannelConnectionStatus.ONLINE, "last_ping_at": now},
            device_values={"last_seen_at": now, "app_version": "certified-heartbeat"},
            receipt_id=str(uuid4()), receipt_values=dict(player_device_id=a["device"], channel_id=a["channel"],
                client_id=a["client"], branch_id=a["branch"], payload_json=payload, payload_bytes=payload_bytes,
                accepted_at=now, available_at=now, status="PENDING", attempts=0, last_error_code=None))

    def stage_certificate(self, db, arguments, plan, *, apply_liveness=True):
        if apply_liveness:
            for row, values in ((arguments["channel"], plan.channel_values),
                                (arguments["device"], plan.device_values)):
                for key, value in values.items():
                    setattr(row, key, value)
        row = PlayerHeartbeatInbox(id=arguments["receipt_id"], **arguments["receipt_values"])
        db.add(row)
        return row

    def assert_no_acceptance(self):
        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_certificate_requires_both_liveness_writes_even_when_roots_are_not_dirty(self):
        with Session(self.engine, autoflush=False) as db:
            arguments = self.certificate(db)
            with self.assertRaises(HTTPException) as rejected:
                with acceptance.heartbeat_accept_guard(db, **arguments) as plan:
                    self.stage_certificate(db, arguments, plan, apply_liveness=False)
                    db.flush()
            self.assertEqual(rejected.exception.status_code, 409)
            db.rollback()
        self.assert_no_acceptance()
        self.accept()
        self.assertEqual(self.count(PlayerHeartbeatInbox), 1, "A correctly applied certificate must remain usable")

    def test_certificate_cannot_authorize_metadata_token_or_relationship_repointing(self):
        cases = (("device", "metadata_json", {"synthetic": "not-liveness"}),
                 ("device", "player_token_hash", "e" * 64),
                 ("device", "channel_id", self.ids["B"]["channel"]),
                 ("device", "client_id", self.ids["B"]["client"]),
                 ("channel", "branch_id", self.ids["B"]["branch"]),
                 ("channel", "current_playback", "not-preack-authorized"))
        for row_key, field, value in cases:
            with self.subTest(row=row_key, field=field):
                with Session(self.engine, autoflush=False) as db:
                    arguments = self.certificate(db)
                    with self.assertRaises(HTTPException) as rejected:
                        with acceptance.heartbeat_accept_guard(db, **arguments) as plan:
                            self.stage_certificate(db, arguments, plan)
                            setattr(arguments[row_key], field, value)
                            db.flush()
                    self.assertEqual(rejected.exception.status_code, 409)
                    db.rollback()
                self.assert_no_acceptance()
        with Session(self.engine) as db:
            self.assertEqual(db.get(PlayerDevice, self.ids["A"]["device"]).player_token_hash,
                             self.rows["A"]["device"].player_token_hash)
            self.assertEqual(db.get(PlayerDevice, self.ids["A"]["device"]).metadata_json, {})

    def test_certificate_freezes_raw_report_and_rejects_bulk_dml(self):
        for mode in ("raw", "scope", "attempts", "bulk"):
            with self.subTest(mode=mode):
                with Session(self.engine, autoflush=False) as db:
                    arguments = self.certificate(db)
                    with self.assertRaises(HTTPException) as rejected:
                        with acceptance.heartbeat_accept_guard(db, **arguments) as plan:
                            row = self.stage_certificate(db, arguments, plan)
                            if mode == "raw":
                                row.payload_json["errors"].append({"synthetic": "post-certificate-mutation"})
                            elif mode == "scope":
                                row.client_id = self.ids["B"]["client"]
                            elif mode == "attempts":
                                row.attempts = 1
                            else:
                                db.execute(update(PlayerDevice).where(PlayerDevice.id == self.ids["A"]["device"])
                                           .values(metadata_json={"synthetic": "bulk-bypass"}))
                            db.flush()
                    self.assertEqual(rejected.exception.status_code, 409)
                    db.rollback()
                self.assert_no_acceptance()

    def test_certificate_rejects_forged_transient_and_wrong_session_actors(self):
        for kind in ("fake", "transient", "wrong_session"):
            with self.subTest(kind=kind), Session(self.engine, autoflush=False) as db:
                arguments = self.certificate(db)
                if kind == "fake":
                    arguments["device"] = SimpleNamespace(**{key: getattr(arguments["device"], key) for key in
                        ("id", "channel_id", "client_id", "branch_id", "player_token_hash", "is_active")})
                elif kind == "transient":
                    arguments["device"] = PlayerDevice(id=self.ids["A"]["device"])
                else:
                    arguments["device"] = self.db.get(PlayerDevice, self.ids["A"]["device"])
                with self.assertRaises(HTTPException) as rejected:
                    with acceptance.heartbeat_accept_guard(db, **arguments):
                        self.fail("An unowned ORM identity must not receive a liveness certificate")
                self.assertEqual(rejected.exception.status_code, 409)
                db.rollback()
        self.assert_no_acceptance()

    def test_fresh_locked_roots_recheck_post_auth_token_scope_client_and_branch(self):
        original = acceptance._locked_roots
        a, b = self.ids["A"], self.ids["B"]
        free_channel = Channel(client_id=b["client"], branch_id=b["branch"], name="Unoccupied race target")
        self.db.add(free_channel)
        self.db.commit()
        cases = ((PlayerDevice, a["device"], {"player_token_hash": "d" * 64}, 409),
                 (PlayerDevice, a["device"], {"is_active": False}, 409),
                 (PlayerDevice, a["device"], {"channel_id": free_channel.id}, 409),
                 (Channel, a["channel"], {"branch_id": b["branch"]}, 409),
                 (Client, a["client"], {"status": "suspended"}, 403),
                 (Branch, a["branch"], {"is_active": False}, 403))
        for model, identity, values, status in cases:
            with self.subTest(model=model.__name__, fields=tuple(values)):
                def changed_after_auth(db, device_identity, channel_identity):
                    db.connection().execute(update(model).where(model.id == identity).values(**values))
                    return original(db, device_identity, channel_identity)
                with patch.object(acceptance, "_locked_roots", changed_after_auth):
                    with self.assertRaises(HTTPException) as rejected:
                        self.accept()
                self.assertEqual(rejected.exception.status_code, status)
                self.assert_no_acceptance()

    def test_externally_owned_connection_cannot_receive_a_false_durable_ack(self):
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, autoflush=False) as db:
                    device = db.get(PlayerDevice, self.ids["A"]["device"])
                    channel = db.get(Channel, self.ids["A"]["channel"])
                    with patch.object(db, "commit", wraps=db.commit) as commit:
                        with self.assertRaises(HTTPException) as rejected:
                            inbox.enqueue(db, device, channel, self.payload())
                    self.assertEqual(rejected.exception.status_code, 409)
                    commit.assert_not_called()
                    self.assertTrue(transaction.is_active)
            finally:
                if transaction.is_active:
                    transaction.rollback()
        self.assert_no_acceptance()

    def test_acceptance_commits_liveness_and_full_raw_receipt_without_history(self):
        payload = self.payload()
        with Session(self.engine, autoflush=False) as db:
            committed = []
            commit = db.commit
            def actual_commit():
                commit()
                committed.append(True)
            with patch.object(db, "commit", actual_commit):
                ack = inbox.enqueue(db, db.get(PlayerDevice, self.ids["A"]["device"]),
                                    db.get(Channel, self.ids["A"]["channel"]), payload)
            self.assertEqual(committed, [True])
        self.assertEqual(set(ack.model_dump()), {"ok", "received_at", "channel_id"})
        self.assertIs(ack.ok, True)
        self.assertEqual(ack.channel_id, self.ids["A"]["channel"])
        with Session(self.engine) as db:
            row = db.scalar(select(PlayerHeartbeatInbox))
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "PENDING")
            self.assertEqual(row.payload_json, payload.model_dump(mode="json"))
            self.assertEqual(utc(row.accepted_at), utc(ack.received_at))
            self.assertEqual((row.player_device_id, row.channel_id, row.client_id, row.branch_id),
                             tuple(self.ids["A"][key] for key in ("device", "channel", "client", "branch")))
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            self.assertEqual(utc(device.last_seen_at), utc(ack.received_at))
            self.assertEqual(utc(channel.last_ping_at), utc(ack.received_at))
            self.assertEqual(channel.status, ChannelConnectionStatus.ONLINE)
            self.assertEqual(device.app_version, payload.app_version)
            self.assertEqual(channel.current_playback, self.rows["A"]["channel"].current_playback)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_failed_acceptance_commit_does_not_ack_or_leave_partial_liveness(self):
        with Session(self.engine, autoflush=False) as db:
            device = db.get(PlayerDevice, self.ids["A"]["device"])
            channel = db.get(Channel, self.ids["A"]["channel"])
            with patch.object(db, "commit", side_effect=OperationalError("synthetic commit failure", {}, Exception())):
                with self.assertRaises(HTTPException) as caught:
                    inbox.enqueue(db, device, channel, self.payload())
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_real_http_200_means_committed_receipt_and_state_with_worker_stopped(self):
        payload = self.payload()
        with patch.object(inbox, "ensure_worker", return_value=None) as wake:
            response = self.http.post("/api/player/heartbeat",
                headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
                json=payload.model_dump(mode="json"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"ok", "received_at", "channel_id"})
        self.assertIs(response.json()["ok"], True)
        self.assertTrue(wake.called, "Only the post-commit wake was stopped for this case")
        accepted = utc(datetime.fromisoformat(response.json()["received_at"].replace("Z", "+00:00")))
        with Session(self.engine) as db:
            row = db.scalar(select(PlayerHeartbeatInbox))
            self.assertIsNotNone(row)
            self.assertEqual(row.payload_json, payload.model_dump(mode="json"))
            self.assertEqual(utc(row.accepted_at), accepted)
            self.assertEqual(row.status, "PENDING")
            self.assertEqual(utc(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at), accepted)
            self.assertEqual(utc(db.get(Channel, self.ids["A"]["channel"]).last_ping_at), accepted)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_real_http_auth_and_required_scope_failures_do_not_accept_or_update_liveness(self):
        payload = self.payload().model_dump(mode="json")
        with patch.object(inbox, "ensure_worker", return_value=None):
            for authorization in (None, "Bearer synthetic-unknown-heartbeat-token"):
                with self.subTest(authorization_present=authorization is not None):
                    response = self.http.post("/api/player/heartbeat",
                        headers={} if authorization is None else {"Authorization": authorization}, json=payload)
                    self.assertEqual(response.status_code, 401, response.text)
            cases = (("device", "is_active", False, 401),
                     ("device", "player_token_hash", "0" * 64, 401),
                     ("client", "status", "suspended", 403),
                     ("branch", "is_active", False, 403),
                     ("device", "client_id", self.ids["B"]["client"], 403),
                     ("device", "branch_id", self.ids["B"]["branch"], 403),
                     ("channel", "branch_id", self.ids["B"]["branch"], 403))
            for model, field, value, status_code in cases:
                with self.subTest(model=model, field=field):
                    row = self.rows["A"][model]
                    old = getattr(row, field)
                    setattr(row, field, value)
                    self.db.commit()
                    try:
                        response = self.http.post("/api/player/heartbeat",
                            headers={"Authorization": "Bearer " + self.rows["A"]["token"]}, json=payload)
                        self.assertEqual(response.status_code, status_code, response.text)
                        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
                        self.assertEqual(self.count(PlayerHeartbeat), 0)
                        with Session(self.engine) as db:
                            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)
                            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
                    finally:
                        setattr(row, field, old)
                        self.db.commit()

    def test_real_http_storage_commit_failure_cannot_return_200(self):
        with patch.object(inbox, "ensure_worker", return_value=None), patch.object(
                Session, "commit", side_effect=OperationalError("synthetic storage failure", {}, Exception())):
            response = self.http.post("/api/player/heartbeat",
                headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
                json=self.payload().model_dump(mode="json"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)
            self.assertIsNone(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at)

    def test_identical_submissions_are_distinct_receipts_and_distinct_history(self):
        identities = [self.accept()[1] for _ in range(6)]
        self.assertEqual(len(set(identities)), 6)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 6)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        for _ in identities:
            self.assertTrue(self.process())
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
        self.assertEqual(self.count(PlayerHeartbeat), 6)
        self.assertTrue(all(self.history(identity) is not None for identity in identities))

    def test_raw_payload_is_a_private_immutable_copy_not_owner_authority(self):
        foreign = self.ids["B"]
        payload = self.payload(current_content_id=foreign["content"],
                               cache_status={"client_id": foreign["client"], "synthetic": [1, 2]})
        expected = payload.model_dump(mode="json")
        _, identity = self.accept(payload)
        payload.cache_status["synthetic"].append(3)
        row = self.receipt(identity)
        self.assertEqual(row.payload_json, expected)
        self.assertEqual(row.client_id, self.ids["A"]["client"])
        self.assertEqual(row.branch_id, self.ids["A"]["branch"])
        self.assertEqual(row.channel_id, self.ids["A"]["channel"])
        self.assertNotEqual(row.client_id, foreign["client"])
        with Session(self.engine) as db:
            snapshot = inbox.snapshot(db)
        encoded = json.dumps(snapshot)
        self.assertNotIn(identity, encoded)
        self.assertNotIn(foreign["client"], encoded)

    def test_foreign_optional_reference_is_sanitized_only_after_durable_acceptance(self):
        payload = self.payload(current_content_id=self.ids["B"]["content"])
        _, identity = self.accept(payload)
        self.assertEqual(self.receipt(identity).payload_json["current_content_id"], self.ids["B"]["content"])
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertTrue(self.process())
        row = self.history(identity)
        self.assertIsNotNone(row)
        self.assertIsNone(row.current_content_id)
        self.assertEqual(row.current_campaign_id, self.ids["A"]["campaign"])
        self.assertEqual(row.current_layout_id, self.ids["A"]["layout"])
        self.assertEqual(row.report_json["payload"], payload.model_dump(mode="json"))
        self.assertEqual(row.client_id, self.ids["A"]["client"])

    def test_optional_opaque_foreign_media_is_not_authorized_by_its_receipt(self):
        content = self.rows["A"]["content"]
        content.metadata_json = {"label(/m%65dia/" + self.ids["B"]["client"] + "/owned.png)": "synthetic"}
        self.db.commit()
        payload = self.payload(current_campaign_id=None)
        _, identity = self.accept(payload)
        self.assertEqual(self.receipt(identity).status, "PENDING")
        self.assertTrue(self.process())
        row = self.history(identity)
        self.assertIsNotNone(row)
        self.assertIsNone(row.current_content_id)
        self.assertEqual(row.current_layout_id, self.ids["A"]["layout"])
        self.assertEqual(row.report_json["payload"], payload.model_dump(mode="json"))

    def test_unrelated_foreign_incoming_edge_does_not_turn_raw_acceptance_into_authority(self):
        self.db.add(CampaignChannelAssignment(channel_id=self.ids["A"]["channel"],
                                              campaign_id=self.ids["B"]["campaign"]))
        self.db.commit()
        _, identity = self.accept()
        before = self.receipt(identity)
        self.assertEqual(before.status, "PENDING")
        self.assertTrue(self.process())
        failed = self.receipt(identity)
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(self.immutable(failed), self.immutable(before))
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.assertFalse(self.process())

    def test_capacity_includes_failed_receipts_without_half_liveness(self):
        with patch.object(inbox, "MAX_PENDING_COUNT", 2):
            _, first = self.accept()
            _, second = self.accept()
            with Session(self.engine) as db:
                db.get(PlayerHeartbeatInbox, first).status = "FAILED"
                old_seen = db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at
                db.commit()
            with self.assertRaises(HTTPException) as caught:
                self.accept()
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 2)
        self.assertEqual(self.receipt(first).status, "FAILED")
        self.assertEqual(self.receipt(second).status, "PENDING")
        with Session(self.engine) as db:
            self.assertEqual(db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at, old_seen)

    def test_finite_byte_capacity_never_evicts_an_accepted_raw_report(self):
        payload = self.payload()
        weight = len(json.dumps(payload.model_dump(mode="json"), sort_keys=True,
                                separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode())
        with patch.object(inbox, "MAX_PENDING_BYTES", weight):
            _, identity = self.accept(payload)
            with self.assertRaises(HTTPException) as caught:
                self.accept(payload)
            self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.receipt(identity).payload_bytes, weight)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 1)

    def test_single_report_size_bound_never_acknowledges_or_changes_liveness(self):
        payload = self.payload(cache_status={"synthetic": "X" * (inbox.MAX_PAYLOAD_BYTES + 1)})
        with self.assertRaises(HTTPException) as caught:
            self.accept(payload)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Channel, self.ids["A"]["channel"]).last_ping_at)

    def test_materialization_uses_receipt_identity_once_and_preserves_full_raw_report(self):
        payload = self.payload()
        ack, identity = self.accept(payload)
        self.assertTrue(self.process())
        self.assertIsNone(self.receipt(identity))
        row = self.history(identity)
        self.assertIsNotNone(row)
        self.assertEqual(row.report_json["payload"], payload.model_dump(mode="json"))
        self.assertEqual(utc(row.received_at), utc(ack.received_at))
        for _ in range(3):
            self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerHeartbeat), 1)
        self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_precommit_interruption_rolls_back_history_and_receipt_consumption_together(self):
        _, identity = self.accept()
        original = self.receipt(identity)

        def fail_after_flushed_claim(payload, device, db, **kwargs):
            claim = kwargs["before_commit"]
            def interrupted(plan):
                claim(plan)
                db.flush()
                raise OperationalError("synthetic interruption before commit", {}, Exception())
            return player._materialize_player_heartbeat(payload, device, db,
                **dict(kwargs, before_commit=interrupted))

        self.process(fail_after_flushed_claim)
        row = self.receipt(identity)
        self.assertIsNotNone(row)
        self.assertEqual(self.immutable(row), self.immutable(original))
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(row.attempts, 1)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        self.ready_again(identity)
        self.assertTrue(self.process())
        self.assertEqual(self.count(PlayerHeartbeat), 1)
        self.assertIsNone(self.receipt(identity))

    def test_postcommit_error_does_not_recreate_receipt_or_duplicate_history(self):
        _, identity = self.accept()
        def fail_after_commit(*args, **kwargs):
            player._materialize_player_heartbeat(*args, **kwargs)
            raise OperationalError("synthetic process failure after commit", {}, Exception())
        self.process(fail_after_commit)
        self.assertIsNone(self.receipt(identity))
        self.assertIsNotNone(self.history(identity))
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerHeartbeat), 1)

    def test_repeated_processor_failures_are_bounded_and_preserve_raw_evidence(self):
        _, identity = self.accept()
        original = self.receipt(identity)
        def unavailable(*args, **kwargs):
            raise OperationalError("synthetic unavailable processor", {}, Exception())
        for _ in range(5):
            self.process(unavailable)
            self.ready_again(identity)
        row = self.receipt(identity)
        self.assertEqual(row.status, "FAILED")
        self.assertEqual(row.attempts, 5)
        self.assertEqual(self.immutable(row), self.immutable(original))
        self.assertFalse(self.process(unavailable))
        self.assertEqual(self.count(PlayerHeartbeat), 0)

    def test_receipt_has_no_business_foreign_key_or_orm_delete_cascade(self):
        mapper = inspect(PlayerHeartbeatInbox)
        self.assertEqual(set(mapper.local_table.foreign_keys), set())
        self.assertEqual(tuple(mapper.relationships), ())

    def assert_deleted_scope_target_is_retained(self, model, key):
        _, identity = self.accept()
        original = self.receipt(identity)
        with self.engine.begin() as connection:
            connection.execute(delete(model).where(model.id == self.ids["A"][key]))
        self.assertEqual(self.immutable(self.receipt(identity)), self.immutable(original))
        self.assertTrue(self.process())
        failed = self.receipt(identity)
        self.assertEqual((failed.status, failed.last_error_code), ("FAILED", "TARGET_DELETED"))
        self.assertEqual(self.immutable(failed), self.immutable(original))
        self.assertFalse(self.process())
        self.assertEqual(self.count(PlayerHeartbeat), 0)

    def test_deleted_device_retains_pending_receipt_then_terminal_failure(self):
        self.assert_deleted_scope_target_is_retained(PlayerDevice, "device")

    def test_deleted_channel_retains_pending_receipt_then_terminal_failure(self):
        self.assert_deleted_scope_target_is_retained(Channel, "channel")

    def test_deleted_branch_retains_pending_receipt_then_terminal_failure(self):
        self.assert_deleted_scope_target_is_retained(Branch, "branch")

    def test_deleted_client_retains_pending_receipt_then_terminal_failure(self):
        self.assert_deleted_scope_target_is_retained(Client, "client")

    def test_nonempty_label_then_empty_report_preserves_eventual_label(self):
        _, first = self.accept(self.payload(current_content_id=None, current_campaign_id=None))
        _, second = self.accept(self.payload(current_content_id=None, current_campaign_id=None, current_layout_id=None))
        self.assertTrue(self.process())
        self.assertTrue(self.process())
        with Session(self.engine) as db:
            self.assertEqual(db.get(Channel, self.ids["A"]["channel"]).current_playback,
                             "Layout " + self.rows["A"]["layout"].name)
        self.assertIsNotNone(self.history(first))
        self.assertIsNotNone(self.history(second))

    def test_pending_retry_prevents_same_device_reports_overtaking(self):
        _, first = self.accept(self.payload(current_content_id=None, current_campaign_id=None))
        _, second = self.accept(self.payload())
        def temporarily_unavailable(*args, **kwargs):
            raise OperationalError("synthetic temporarily unavailable", {}, Exception())
        self.process(temporarily_unavailable)
        self.assertFalse(self.process(), "An earlier pending report still governs this device's processing order")
        self.assertIsNone(self.history(second))
        self.ready_again(first)
        self.assertTrue(self.process())
        self.assertTrue(self.process())
        with Session(self.engine) as db:
            self.assertEqual(db.get(Channel, self.ids["A"]["channel"]).current_playback,
                             self.rows["A"]["content"].name)

    def test_requeued_older_failed_report_cannot_overwrite_a_newer_nonempty_label(self):
        _, first = self.accept(self.payload(current_content_id=None, current_campaign_id=None))
        _, second = self.accept(self.payload())
        with Session(self.engine) as db:
            db.get(PlayerHeartbeatInbox, first).status = "FAILED"
            db.commit()
        self.assertTrue(self.process())
        self.assertIsNotNone(self.history(second))
        # An explicit QA operational requeue tests the root materializer's
        # version fence; it neither changes immutable report data nor invents
        # a production retry endpoint.
        with Session(self.engine) as db:
            row = db.get(PlayerHeartbeatInbox, first)
            row.status, row.attempts, row.available_at = "PENDING", 0, datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        self.assertTrue(self.process())
        self.assertIsNotNone(self.history(first))
        with Session(self.engine) as db:
            self.assertEqual(db.get(Channel, self.ids["A"]["channel"]).current_playback,
                             self.rows["A"]["content"].name)

    def test_post_ack_processing_does_not_replay_old_liveness_or_app_version(self):
        _, first = self.accept(self.payload(status="offline", app_version="old-version"))
        _, second = self.accept(self.payload(status="online", app_version="new-version"))
        with Session(self.engine) as db:
            before = (db.get(Channel, self.ids["A"]["channel"]).status,
                      db.get(Channel, self.ids["A"]["channel"]).last_ping_at,
                      db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at,
                      db.get(PlayerDevice, self.ids["A"]["device"]).app_version)
        self.assertTrue(self.process())
        self.assertTrue(self.process())
        with Session(self.engine) as db:
            after = (db.get(Channel, self.ids["A"]["channel"]).status,
                     db.get(Channel, self.ids["A"]["channel"]).last_ping_at,
                     db.get(PlayerDevice, self.ids["A"]["device"]).last_seen_at,
                     db.get(PlayerDevice, self.ids["A"]["device"]).app_version)
        self.assertEqual(after, before)
        self.assertEqual(after[0], ChannelConnectionStatus.ONLINE)
        self.assertEqual(after[3], "new-version")
        self.assertIsNotNone(self.history(first))
        self.assertIsNotNone(self.history(second))

    def test_stopped_worker_backlog_recovers_on_new_application_without_resubmission(self):
        identities = [self.accept(self.payload(app_version="backlog-" + str(i)))[1] for i in range(4)]
        self.assertEqual(self.count(PlayerHeartbeatInbox), 4)
        self.assertEqual(self.count(PlayerHeartbeat), 0)
        async def recover():
            app = FastAPI()
            inbox.ensure_worker(app, self.engine, player._materialize_player_heartbeat)
            try:
                deadline = time.monotonic() + 10
                while self.count(PlayerHeartbeatInbox) and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertEqual(self.count(PlayerHeartbeatInbox), 0)
            finally:
                await inbox.stop_worker(app, self.engine)
        asyncio.run(recover())
        self.assertEqual(self.count(PlayerHeartbeat), 4)
        self.assertTrue(all(self.history(identity) is not None for identity in identities))
        with Session(self.engine) as db:
            self.assertEqual(db.get(PlayerDevice, self.ids["A"]["device"]).app_version, "backlog-3")
        self.assertEqual(self.engine.pool.checkedout(), 0)


class HeartbeatInboxTests(_HeartbeatInboxCases, GateFixture):
    def test_committed_raw_receipt_is_visible_to_an_independent_read_only_process(self):
        _, identity = self.accept()
        path = Path(self.engine.url.database).resolve()
        self.assertEqual(path, (self.root / "fixture.sqlite").resolve())
        env = dict(os.environ, HEARTBEAT_QA_SQLITE_PATH=str(path), HEARTBEAT_QA_RECEIPT_ID=identity)
        script = """import json,os,sqlite3;from pathlib import Path;from urllib.parse import quote
p=Path(os.environ['HEARTBEAT_QA_SQLITE_PATH']).resolve()
c=sqlite3.connect('file:'+quote(p.as_posix(),safe='/:')+'?mode=ro',uri=True)
r=c.execute('SELECT status,payload_json FROM player_heartbeat_inbox WHERE id=?',(os.environ['HEARTBEAT_QA_RECEIPT_ID'],)).fetchone()
assert r is not None and r[0]=='PENDING';assert json.loads(r[1])['status']=='online'
print(json.dumps({'pending_receipt_readable':True,'rows':1}));c.close()
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"pending_receipt_readable": True, "rows": 1})
        self.assertEqual(self.count(PlayerHeartbeat), 0)


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"), "Owned isolated PostgreSQL QA required")
class HeartbeatInboxPostgresTests(_HeartbeatInboxCases, GateFixture):
    def make_engine(self):
        url = make_url(os.environ["SECURITY_R5_TEST_DATABASE_URL"])
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        expected = os.environ.get("SECURITY_R5_PG_SYSTEM_IDENTIFIER")
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
                or url.database != "security_r5_qa" or url.query.get("options") or not marker or not expected):
            raise RuntimeError("Refusing an unverified/nonlocal heartbeat PostgreSQL fixture")
        schema = "heartbeat_inbox_" + uuid4().hex
        engine = create_engine(url, hide_parameters=True, connect_args={"options": "-csearch_path=" + schema})
        with engine.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")), "security_r5_qa")
            self.assertEqual(connection.scalar(text("SELECT current_setting('vwflex.qa_worker', true)")), marker)
            self.assertEqual(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")), expected)
            connection.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            try:
                with engine.begin() as connection:
                    connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            finally:
                engine.dispose()
        self.addCleanup(cleanup)
        return engine
