"""Durable receipt scope, deletion safety and atomic consumption in owned QA."""
from datetime import UTC, datetime
from hashlib import sha256
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from app.models.entities import Branch, Channel, Client, PlayerDevice, PlayerPlaybackEvent, PlayerPlaybackInbox
from app.services import tenant_mutations as mutations
from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import APPROVED_RELATION_FINGERPRINTS, MODEL_SPECS, model_spec, relations_for, validate_registry
from test_tenant_gate_performance import GateFixture


class PlaybackInboxGuardTests(GateFixture):
    def receipt_values(self, **changes):
        a = self.ids["A"]
        now = datetime.now(UTC)
        values = dict(player_device_id=a["device"], channel_id=a["channel"], client_id=a["client"],
                      branch_id=a["branch"], payload_json={"status": "completed"}, payload_bytes=22,
                      accepted_at=now, status="PENDING", attempts=0, available_at=now, last_error_code=None)
        values.update(changes)
        return values

    def inbox_intents(self, identity, values):
        return (mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", self.ids["A"]["device"]), ()),
                mutations.MutationIntent("CREATE", RowRef("PlayerPlaybackInbox", identity), tuple(values.items())))

    def enqueue(self, **changes):
        identity = str(uuid4())
        values = self.receipt_values(**changes)
        with mutations.player_write_guard(self.db, channel=self.rows["A"]["channel"],
                player_device=self.rows["A"]["device"], operation="create_player_playback_event",
                intents=self.inbox_intents(identity, values)):
            self.db.add(PlayerPlaybackInbox(id=identity, **values))
            self.db.commit()
        return identity

    def event_intents(self, identity):
        a = self.ids["A"]
        values = dict(player_device_id=a["device"], channel_id=a["channel"], client_id=a["client"],
                      branch_id=a["branch"], started_at=datetime.now(UTC), status="completed", metadata_json={})
        return values, (mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", a["device"]), ()),
                        mutations.MutationIntent("CREATE", RowRef("PlayerPlaybackEvent", identity), tuple(values.items())))

    def consume(self, identity, *, fail_before_commit=False):
        values, intents = self.event_intents(identity)
        with mutations.player_write_guard(self.db, channel=self.rows["A"]["channel"],
                player_device=self.rows["A"]["device"], operation="create_player_playback_event", intents=intents) as plan:
            with mutations.locked_player_playback_receipt(self.db, plan=plan, receipt_id=identity,
                    channel=self.rows["A"]["channel"], player_device=self.rows["A"]["device"]) as receipt:
                self.db.delete(receipt)
            self.db.add(PlayerPlaybackEvent(id=identity, **values))
            if fail_before_commit:
                self.db.flush()
                raise RuntimeError("synthetic interruption")
            self.db.commit()

    def test_catalog_registers_private_historical_identifiers_without_live_relationships(self):
        self.assertTrue(validate_registry())
        spec = model_spec("PlayerPlaybackInbox")
        self.assertEqual(spec.opaque_fields, ())
        self.assertEqual((spec.owner_field, spec.owner_target, spec.owner_kind, spec.branch_policy),
                         ("client_id", "Client", "direct-owner", "FIELD"))
        self.assertEqual(spec.orm_relationships, ())
        self.assertEqual(relations_for("PlayerPlaybackInbox"), ())
        mapper = inspect(PlayerPlaybackInbox)
        self.assertEqual(set(mapper.local_table.foreign_keys), set())
        self.assertEqual(tuple(mapper.relationships), ())
        for field in ("player_device_id", "channel_id", "client_id", "branch_id"):
            self.assertFalse(mapper.columns[field].nullable)
            self.assertEqual(mapper.columns[field].type.length, 36)
        self.assertEqual({constraint.name for constraint in mapper.local_table.constraints if constraint.name},
                         {"ck_player_playback_inbox_status", "ck_player_playback_inbox_payload_bytes",
                          "ck_player_playback_inbox_attempts"})

    def test_additive_catalog_preserves_every_legacy_model_and_fingerprint(self):
        # Frozen from approved e6c3f4 before the additive inbox: no weakening or
        # replacement of any of the original 40 models / 105 edge policies.
        # Both private operational inboxes are additions outside that baseline.
        legacy_models = sorted(spec.name for spec in MODEL_SPECS
                               if spec.name not in {"PlayerPlaybackInbox", "PlayerHeartbeatInbox"})
        legacy_edges = {key: value for key, value in APPROVED_RELATION_FINGERPRINTS.items()
                        if not key.startswith("PlayerPlaybackInbox.")}
        digest = lambda value: sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(digest(legacy_models), "d39469037bbbcbbb61656b9cfa428940a458e2aa67f1bf3d7ae11b637e594581")
        self.assertEqual(digest(legacy_edges), "b0b6382ecb71e02eb1840855395636a82eec91e5fb3344bc5429687c7b29ff12")
        self.assertEqual(len(legacy_edges), 105)
        self.assertEqual(set(APPROVED_RELATION_FINGERPRINTS), set(legacy_edges))

    def test_raw_optional_ids_and_media_in_payload_never_establish_authority(self):
        b = self.ids["B"]
        payload = {"content_id": b["content"], "campaign_id": b["campaign"],
                   "metadata_json": {"src": "/media/" + b["client"] + "/owned.png"}}
        identity = self.enqueue(payload_json=payload)
        row = self.db.get(PlayerPlaybackInbox, identity)
        self.assertEqual(row.payload_json, payload)
        self.assertEqual((row.client_id, row.branch_id), (self.ids["A"]["client"], self.ids["A"]["branch"]))
        self.assertFalse(self.owned(path=self.rows["B"]["path"]))

    def test_each_foreign_scope_field_is_rejected_before_append(self):
        for field, key in (("player_device_id", "device"), ("channel_id", "channel"),
                           ("client_id", "client"), ("branch_id", "branch")):
            with self.subTest(field=field):
                with self.assertRaises(HTTPException) as caught:
                    self.enqueue(**{field: self.ids["B"][key]})
                self.assertEqual(caught.exception.status_code, 409)
                self.db.rollback()
                self.assertEqual(list(self.db.scalars(select(PlayerPlaybackInbox.id))), [])

    def test_enqueue_accepts_one_history_kind_and_no_structural_device_patch(self):
        identity = str(uuid4())
        values = self.receipt_values()
        event_values, event_intents = self.event_intents(str(uuid4()))
        batches = (
            self.inbox_intents(identity, values) + (event_intents[1],),
            (mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", self.ids["A"]["device"]),
                                      (("app_version", "unexpected"),)),
             self.inbox_intents(identity, values)[1]),
        )
        for intents in batches:
            with self.subTest(intents=len(intents)):
                with self.assertRaises(HTTPException) as caught:
                    with mutations.player_write_guard(self.db, channel=self.rows["A"]["channel"],
                            player_device=self.rows["A"]["device"], operation="create_player_playback_event",
                            intents=intents):
                        self.fail("An unrelated/mixed mutation received the activity capability")
                self.assertEqual(caught.exception.status_code, 409)
                self.db.rollback()

    def test_invalid_receipt_scalar_shape_is_rejected(self):
        for changes in ({"payload_bytes": True}, {"payload_bytes": 0}, {"attempts": True},
                        {"attempts": 1}, {"status": "FAILED"}, {"payload_json": []}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(HTTPException):
                    self.enqueue(**changes)
                self.db.rollback()

    def test_generic_actor_cannot_create_a_receipt_without_player_scope(self):
        identity = str(uuid4())
        values = self.receipt_values()
        with self.assertRaises(HTTPException) as caught:
            with mutations.write_guard(self.db, actor=self.rows["A"]["user"],
                    client_id=self.ids["A"]["client"], operation="create_player_playback_event",
                    intents=self.inbox_intents(identity, values)):
                pass
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback()
        self.assertIsNone(self.db.get(PlayerPlaybackInbox, identity))

    def test_generic_actor_cannot_mutate_an_acknowledged_payload(self):
        identity = self.enqueue()
        intent = mutations.MutationIntent("UPDATE", RowRef("PlayerPlaybackInbox", identity),
                                          (("payload_json", {"status": "changed"}),))
        with self.assertRaises(HTTPException) as caught:
            with mutations.write_guard(self.db, actor=self.rows["A"]["user"],
                    client_id=self.ids["A"]["client"], operation="create_player_playback_event",
                    intents=(intent,)):
                pass
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback()
        self.assertEqual(self.db.get(PlayerPlaybackInbox, identity).payload_json, {"status": "completed"})

    def test_generic_actor_cannot_delete_an_acknowledged_receipt(self):
        identity = self.enqueue()
        receipt = self.db.get(PlayerPlaybackInbox, identity)
        with self.assertRaises(HTTPException) as caught:
            with mutations.delete_guard(self.db, (receipt,), actor=self.rows["A"]["user"],
                    operation="delete_channel"):
                self.fail("An administrative operation obtained private receipt deletion")
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback()
        self.assertIsNotNone(self.db.get(PlayerPlaybackInbox, identity))

    def test_parent_delete_plans_do_not_consume_pending_or_failed_receipts(self):
        identity = self.enqueue()
        for status in ("PENDING", "FAILED"):
            row = self.db.get(PlayerPlaybackInbox, identity)
            row.status = status
            self.db.commit()
            before = dict(self.db.execute(select(PlayerPlaybackInbox.__table__).where(
                PlayerPlaybackInbox.id == identity)).mappings().one())
            self.db.rollback()
            for key, operation in (("client", "delete_client"), ("branch", "delete_branch"),
                                   ("channel", "delete_channel"), ("device", "delete_channel")):
                with self.subTest(status=status, parent=key):
                    parent = self.db.get(type(self.rows["A"][key]), self.ids["A"][key])
                    actor = self.db.get(type(self.rows["A"]["user"]), self.ids["A"]["user"])
                    with mutations.delete_guard(self.db, (parent,), actor=actor, operation=operation):
                        pass
                    self.db.rollback()
                    after = dict(self.db.execute(select(PlayerPlaybackInbox.__table__).where(
                        PlayerPlaybackInbox.id == identity)).mappings().one())
                    self.assertEqual(after, before)
                    self.assertIsNotNone(self.db.get(type(parent), self.ids["A"][key]))
                    self.db.rollback()

    def test_committed_raw_parent_delete_preserves_pending_and_failed_receipt_snapshots(self):
        # Fresh synthetic scope per subcase; COMMIT each real parent deletion.
        # The other tenant and the fixture's administrative actor stay intact.
        for status in ("PENDING", "FAILED"):
            for model, key in ((Client, "client_id"), (Branch, "branch_id"),
                               (Channel, "channel_id"), (PlayerDevice, "player_device_id")):
                with self.subTest(status=status, parent=model.__name__):
                    with Session(self.engine, expire_on_commit=False, autoflush=False) as db:
                        client = Client(name="Receipt delete QA", slug="receipt-delete-" + uuid4().hex)
                        db.add(client)
                        db.flush()
                        branch = Branch(client_id=client.id, name="Receipt delete QA", code="DELETE")
                        db.add(branch)
                        db.flush()
                        channel = Channel(client_id=client.id, branch_id=branch.id, name="Receipt delete QA")
                        db.add(channel)
                        db.flush()
                        device = PlayerDevice(client_id=client.id, branch_id=branch.id, channel_id=channel.id,
                                              hardware_id="receipt-" + uuid4().hex, device_name="Receipt delete QA",
                                              player_token_hash=uuid4().hex * 2, is_active=True)
                        db.add(device)
                        db.commit()
                        identity = str(uuid4())
                        values = self.receipt_values(player_device_id=device.id, channel_id=channel.id,
                                                     client_id=client.id, branch_id=branch.id)
                        intents = (mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", device.id), ()),
                                   mutations.MutationIntent("CREATE", RowRef("PlayerPlaybackInbox", identity),
                                                            tuple(values.items())))
                        with mutations.player_write_guard(db, channel=channel, player_device=device,
                                operation="create_player_playback_event", intents=intents):
                            db.add(PlayerPlaybackInbox(id=identity, **values))
                            db.commit()
                        receipt = db.get(PlayerPlaybackInbox, identity)
                        receipt.status = status
                        db.commit()
                        before = dict(db.execute(select(PlayerPlaybackInbox.__table__).where(
                            PlayerPlaybackInbox.id == identity)).mappings().one())
                        db.rollback()
                        with self.engine.begin() as connection:
                            deleted = connection.execute(delete(model).where(model.id == values[key]))
                            self.assertEqual(deleted.rowcount, 1)
                        after = dict(db.execute(select(PlayerPlaybackInbox.__table__).where(
                            PlayerPlaybackInbox.id == identity)).mappings().one())
                        self.assertEqual(after, before)
                        self.assertIsNone(db.scalar(select(model.id).where(model.id == values[key])))

    def test_historical_receipt_does_not_rewrite_after_coherent_parent_reparenting(self):
        a = self.ids["A"]
        destination = Branch(client_id=a["client"], name="Same tenant target", code="TARGET", is_active=True)
        self.db.add(destination)
        self.db.commit()
        intents = (
            mutations.MutationIntent("UPDATE", RowRef("Channel", a["channel"]),
                                     (("branch_id", destination.id),)),
            mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", a["device"]),
                                     (("branch_id", destination.id),)),
        )
        actor = self.rows["A"]["user"]
        # The coherent same-tenant plan is otherwise legitimate. No application
        # mutation occurs in this control, so the exact plan can be compared.
        with mutations.write_guard(self.db, actor=actor, client_id=a["client"],
                operation="activate_player", intents=intents):
            pass
        self.db.rollback()
        identity = self.enqueue()
        before = dict(self.db.execute(select(PlayerPlaybackInbox.__table__).where(
            PlayerPlaybackInbox.id == identity)).mappings().one())
        self.db.rollback()
        with mutations.write_guard(self.db, actor=actor, client_id=a["client"],
                operation="activate_player", intents=intents):
            self.db.get(Channel, a["channel"]).branch_id = destination.id
            self.db.get(PlayerDevice, a["device"]).branch_id = destination.id
            self.db.commit()
        self.assertEqual(self.db.get(Channel, a["channel"]).branch_id, destination.id)
        self.assertEqual(self.db.get(PlayerDevice, a["device"]).branch_id, destination.id)
        after = dict(self.db.execute(select(PlayerPlaybackInbox.__table__).where(
            PlayerPlaybackInbox.id == identity)).mappings().one())
        self.assertEqual(after, before)
        self.assertEqual(self.db.get(PlayerPlaybackInbox, identity).branch_id, a["branch"])

    def test_consumption_commits_future_event_and_receipt_delete_together(self):
        identity = self.enqueue()
        self.consume(identity)
        self.assertIsNone(self.db.get(PlayerPlaybackInbox, identity))
        self.assertIsNotNone(self.db.get(PlayerPlaybackEvent, identity))

    def test_interruption_rolls_back_both_event_and_receipt_delete(self):
        identity = self.enqueue()
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            self.consume(identity, fail_before_commit=True)
        self.db.rollback()
        self.assertIsNotNone(self.db.get(PlayerPlaybackInbox, identity))
        self.assertIsNone(self.db.get(PlayerPlaybackEvent, identity))

    def test_consumption_requires_the_matching_live_event_plan(self):
        identity = self.enqueue()
        with self.assertRaises(HTTPException):
            with mutations.locked_player_playback_receipt(self.db, plan=object(), receipt_id=identity,
                    channel=self.rows["A"]["channel"], player_device=self.rows["A"]["device"]):
                self.fail("A caller forged receipt consumption without a live event plan")
        self.db.rollback()
        values, intents = self.event_intents(str(uuid4()))
        with self.assertRaises(HTTPException):
            with mutations.player_write_guard(self.db, channel=self.rows["A"]["channel"],
                    player_device=self.rows["A"]["device"], operation="create_player_playback_event", intents=intents) as plan:
                with mutations.locked_player_playback_receipt(self.db, plan=plan, receipt_id=identity,
                        channel=self.rows["A"]["channel"], player_device=self.rows["A"]["device"]):
                    self.fail("A receipt was consumed as another future event")
        self.db.rollback()

    def test_receipt_payload_mutation_before_consumption_is_rejected(self):
        identity = self.enqueue()
        values, intents = self.event_intents(identity)
        with self.assertRaises(HTTPException):
            with mutations.player_write_guard(self.db, channel=self.rows["A"]["channel"],
                    player_device=self.rows["A"]["device"], operation="create_player_playback_event", intents=intents) as plan:
                with mutations.locked_player_playback_receipt(self.db, plan=plan, receipt_id=identity,
                        channel=self.rows["A"]["channel"], player_device=self.rows["A"]["device"]) as row:
                    row.payload_json["status"] = "changed"
                    self.db.delete(row)
        self.db.rollback()
        self.assertEqual(self.db.get(PlayerPlaybackInbox, identity).payload_json, {"status": "completed"})

    def test_parent_delete_after_consumption_preserves_legacy_event_cascade(self):
        identity = self.enqueue()
        self.consume(identity)
        device = self.db.get(PlayerDevice, self.ids["A"]["device"])
        actor = self.db.get(type(self.rows["A"]["user"]), self.ids["A"]["user"])
        with mutations.delete_guard(self.db, (device,), actor=actor, operation="delete_channel"):
            self.db.delete(device)
            self.db.commit()
        self.assertIsNone(self.db.get(PlayerDevice, self.ids["A"]["device"]))
        self.assertIsNone(self.db.get(PlayerPlaybackEvent, identity))
