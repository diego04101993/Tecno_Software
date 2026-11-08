"""R5 exhaustive-history oracle, SQL certificates and real PostgreSQL locks.

Only isolated fixtures are allowed. PostgreSQL is opt-in through its separately
owned security_r5_qa database and marker; this module never imports app.main.
"""
from dataclasses import replace
from datetime import UTC, datetime
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("R5 requires the isolated SQLite-configured test harness")

from fastapi import HTTPException
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.entities import (
    Branch, Campaign, Channel, ContentFolder, ContentItem, Layout, PlayerDevice,
    PlayerHeartbeat, PlayerPlaybackEvent, User,
)
from app.services import media_references as media
from app.services import tenant_mutations as mutations
from app.services.tenant_ownership import RowRef
from test_tenant_gate_performance import GateFixture


HISTORY_SIZE = 1105
OLD_TIME = datetime(2000, 1, 1, tzinfo=UTC)
HISTORY_MODELS = (PlayerHeartbeat, PlayerPlaybackEvent)
FK_FIELDS = {
    PlayerHeartbeat: ("client_id", "branch_id", "channel_id", "player_device_id",
                      "current_content_id", "current_campaign_id", "current_layout_id"),
    PlayerPlaybackEvent: ("client_id", "branch_id", "channel_id", "player_device_id",
                          "content_id", "campaign_id", "layout_id"),
}
TARGET_KEYS = {"client_id": "client", "branch_id": "branch", "channel_id": "channel",
               "player_device_id": "device", "content_id": "content", "campaign_id": "campaign",
               "layout_id": "layout", "current_content_id": "content",
               "current_campaign_id": "campaign", "current_layout_id": "layout"}


class _TelemetryCases:
    def history_rows(self, model, count=HISTORY_SIZE, *, side="A", offset=0):
        ids = self.ids[side]
        rows = []
        for index in range(count):
            row = dict(id=str(UUID(int=offset + index + 1)), client_id=ids["client"],
                       branch_id=ids["branch"], channel_id=ids["channel"],
                       player_device_id=ids["device"], created_at=OLD_TIME, updated_at=OLD_TIME)
            if model is PlayerHeartbeat:
                row.update(current_content_id=ids["content"], current_campaign_id=ids["campaign"],
                           current_layout_id=ids["layout"], received_at=OLD_TIME)
            else:
                row.update(content_id=ids["content"], campaign_id=ids["campaign"],
                           layout_id=ids["layout"], started_at=OLD_TIME)
            rows.append(row)
        self.db.execute(insert(model), rows)
        self.db.commit()
        return [row["id"] for row in rows]

    def clear_history(self):
        for model in HISTORY_MODELS:
            self.db.execute(delete(model))
        self.db.commit()

    def seed_both(self, count=HISTORY_SIZE):
        return {model: self.history_rows(model, count) for model in HISTORY_MODELS}

    def allow_channel(self):
        try:
            with self.channel_guard(self.db) as plan:
                return plan
        finally:
            self.db.rollback()

    def deny_channel(self):
        def attempt():
            with self.channel_guard(self.db):
                self.fail("Incompatible historical relation reached the writer")
        try:
            self.assert_conflict(attempt)
        finally:
            self.db.rollback()

    def test_deep_old_first_middle_last_violation_in_each_history_model(self):
        history = self.seed_both()
        for model in HISTORY_MODELS:
            for position in (0, HISTORY_SIZE // 2, HISTORY_SIZE - 1):
                with self.subTest(model=model.__name__, position=position):
                    identity = history[model][position]
                    self.db.execute(update(model).where(model.id == identity)
                                    .values(client_id=self.ids["B"]["client"]))
                    self.db.commit()
                    self.deny_channel()
                    self.db.execute(update(model).where(model.id == identity)
                                    .values(client_id=self.ids["A"]["client"]))
                    self.db.commit()
        self.allow_channel()

    def test_every_registered_telemetry_fk_keeps_its_full_owner_context_check(self):
        for model in HISTORY_MODELS:
            for field in FK_FIELDS[model]:
                with self.subTest(model=model.__name__, field=field):
                    self.clear_history()
                    identity = self.history_rows(model, 1)[0]
                    foreign = self.ids["B"][TARGET_KEYS[field]]
                    self.db.execute(update(model).where(model.id == identity).values({field: foreign}))
                    self.db.commit()
                    self.deny_channel()

    def test_same_client_different_branch_is_not_a_valid_historical_context(self):
        branch = Branch(client_id=self.ids["A"]["client"], name="A second", code="A2")
        self.db.add(branch); self.db.commit()
        for model in HISTORY_MODELS:
            with self.subTest(model=model.__name__):
                self.clear_history()
                identity = self.history_rows(model, 1)[0]
                self.db.execute(update(model).where(model.id == identity).values(branch_id=branch.id))
                self.db.commit()
                self.deny_channel()

    def test_null_optional_references_remain_legal_for_both_history_models(self):
        for model in HISTORY_MODELS:
            identities = self.history_rows(model, 3)
            values = {field: None for field in FK_FIELDS[model][4:]}
            self.db.execute(update(model).where(model.id.in_(identities)).values(values))
        self.db.commit()
        self.allow_channel()

    def test_parent_dependency_closure_is_not_replaced_by_direct_owner_comparison(self):
        self.seed_both(1)
        folder = ContentFolder(client_id=self.ids["B"]["client"], name="B foreign folder")
        self.db.add(folder); self.db.commit()
        self.db.execute(update(ContentItem).where(ContentItem.id == self.ids["A"]["content"])
                        .values(folder_id=folder.id))
        self.db.commit()
        self.deny_channel()

    def test_parent_opaque_media_validation_still_executes_from_history_context(self):
        self.seed_both(1)
        self.db.execute(update(Layout).where(Layout.id == self.ids["A"]["layout"])
                        .values(zones=[{"type": "image", "content_id": self.ids["B"]["content"]}]))
        self.db.commit()
        self.deny_channel()

    def test_referenced_context_is_not_promoted_to_an_incoming_mutation_root(self):
        self.seed_both(2)
        # This invalid B history does not reference any A mutation anchor. It
        # references A content only as outgoing context; the original traversal
        # validates A content, but does not start its independent incoming scan.
        for model in HISTORY_MODELS:
            identity = self.history_rows(model, 1, side="B", offset=100000)[0]
            field = "current_content_id" if model is PlayerHeartbeat else "content_id"
            self.db.execute(update(model).where(model.id == identity)
                            .values({field: self.ids["A"]["content"]}))
        self.db.commit()
        self.allow_channel()

    def proposed_branch_guard(self, branch_id):
        ids = self.ids["A"]
        return mutations.write_guard(self.db, actor=self.db.get(User, ids["user"]),
            client_id=ids["client"], operation="activate_player", intents=(
                mutations.MutationIntent("UPDATE", RowRef("Channel", ids["channel"]),
                                         (("branch_id", branch_id),)),
                mutations.MutationIntent("UPDATE", RowRef("PlayerDevice", ids["device"]),
                                         (("branch_id", branch_id),)),
            ))

    def test_current_and_proposed_channel_device_branch_are_both_validated(self):
        branch = Branch(client_id=self.ids["A"]["client"], name="A move target", code="A-MOVE")
        self.db.add(branch); self.db.commit()
        # A coherent same-client proposed parent move is legitimate with no old
        # dependent telemetry. This positive control prevents a vacuous denial.
        with self.proposed_branch_guard(branch.id):
            pass
        self.db.rollback()
        self.seed_both(2)
        try:
            with self.assertRaises(HTTPException) as caught:
                with self.proposed_branch_guard(branch.id):
                    self.fail("Proposed parent branch mismatch was ignored")
            self.assertEqual((caught.exception.status_code, caught.exception.detail),
                             (409, "TENANT_RELATION_CONFLICT"))
        finally:
            self.db.rollback()

    def test_delete_approval_allows_matching_rows_and_rejects_foreign_coverage(self):
        matching = self.seed_both(2)
        foreign = {model: self.history_rows(model, 1, side="B", offset=100000)[0]
                   for model in HISTORY_MODELS}
        user = self.db.get(User, self.ids["A"]["user"])
        channel = self.db.get(Channel, self.ids["A"]["channel"])
        try:
            with mutations.delete_guard(self.db, (channel,), actor=user, operation="delete_channel"):
                for model in HISTORY_MODELS:
                    mutations.require_delete_coverage(self.db, (RowRef(model.__name__, matching[model][0]),))
                    with self.assertRaises(HTTPException) as caught:
                        mutations.require_delete_coverage(self.db, (RowRef(model.__name__, foreign[model]),))
                    self.assertEqual(caught.exception.status_code, 409)
        finally:
            self.db.rollback()

    def test_delete_bulk_empty_is_safe_and_foreign_or_unscoped_targets_are_rejected(self):
        self.seed_both(2)
        foreign = {model: self.history_rows(model, 1, side="B", offset=100000)[0]
                   for model in HISTORY_MODELS}
        user = self.db.get(User, self.ids["A"]["user"])
        channel = self.db.get(Channel, self.ids["A"]["channel"])
        try:
            with mutations.delete_guard(self.db, (channel,), actor=user, operation="delete_channel"):
                for model in HISTORY_MODELS:
                    self.assertEqual(self.db.execute(delete(model).where(False)).rowcount, 0)
                    for statement in (delete(model).where(model.id == foreign[model]),
                                      update(model).where(model.id == foreign[model]).values(status="changed"),
                                      delete(model)):
                        with self.subTest(model=model.__name__, statement=type(statement).__name__):
                            with self.assertRaises(HTTPException) as caught:
                                self.db.execute(statement)
                            self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                             (409, "TENANT_RELATION_CONFLICT"))
                    self.assertIsNotNone(self.db.get(model, foreign[model]))
        finally:
            self.db.rollback()

    def test_empty_persisted_history_identity_is_rejected_without_fk_corruption(self):
        for model in HISTORY_MODELS:
            with self.subTest(model=model.__name__):
                self.clear_history()
                identity = self.history_rows(model, 1)[0]
                self.db.execute(update(model).where(model.id == identity).values(id=""))
                self.db.commit()
                # Only the history identity is invalid; all seven foreign keys
                # still point at the original coherent A fixtures.
                actual = self.db.execute(select(*(getattr(model, field) for field in FK_FIELDS[model]))
                                         .where(model.id == "")).one()
                self.assertEqual(tuple(actual), tuple(self.ids["A"][TARGET_KEYS[field]]
                                                      for field in FK_FIELDS[model]))
                self.deny_channel()

    def test_nullable_scope_mixed_bulk_targets_reject_before_any_partial_write(self):
        for model in HISTORY_MODELS:
            for action in ("DELETE", "UPDATE"):
                with self.subTest(model=model.__name__, action=action):
                    self.clear_history()
                    own_id = self.history_rows(model, 1)[0]
                    foreign_id = self.history_rows(model, 1, side="B", offset=100000)[0]
                    field = "current_campaign_id" if model is PlayerHeartbeat else "campaign_id"
                    self.db.execute(update(model).where(model.id == foreign_id).values({field: None}))
                    self.db.commit()
                    wanted = (own_id, foreign_id)
                    state = select(model.id, model.status).where(model.id.in_(wanted)).order_by(model.id)
                    before = tuple(self.db.execute(state).all())
                    self.assertEqual(len(before), 2)
                    statement = (delete(model).where(model.id.in_(wanted)) if action == "DELETE" else
                                 update(model).where(model.id.in_(wanted)).values(status="partial-write"))
                    user = self.db.get(User, self.ids["A"]["user"])
                    campaign = self.db.get(Campaign, self.ids["A"]["campaign"])
                    try:
                        with mutations.delete_guard(self.db, (campaign,), actor=user,
                                                    operation="delete_campaign"):
                            mutations.require_delete_coverage(self.db,
                                (RowRef(model.__name__, own_id),))
                            rejection = None
                            try:
                                self.db.execute(statement)
                            except HTTPException as error:
                                rejection = error
                            # Check before rollback: silent narrowing must not
                            # partially delete/update A while leaving B alone.
                            self.assertEqual(tuple(self.db.execute(state).all()), before,
                                             "Mixed nullable-scope bulk statement partially mutated A")
                            self.assertIsNotNone(rejection, "Mixed A+B statement did not return 409")
                            self.assertEqual((rejection.status_code, rejection.detail),
                                             (409, "TENANT_RELATION_CONFLICT"))
                    finally:
                        self.db.rollback()


class TelemetryScaleSQLiteTests(_TelemetryCases, GateFixture):
    """Original exhaustive row-wise implementation is the SQLite oracle."""


@unittest.skipUnless(os.environ.get("SECURITY_R5_TEST_DATABASE_URL"),
                     "isolated SECURITY_R5 PostgreSQL fixture not configured")
class TelemetryScalePostgresTests(_TelemetryCases, GateFixture):
    def make_engine(self):
        url = make_url(os.environ["SECURITY_R5_TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or url.database != "security_r5_qa" or url.query.get("options")):
            raise RuntimeError("R5 requires its isolated localhost security_r5_qa database")
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        expected = os.environ.get("SECURITY_R5_PG_SYSTEM_IDENTIFIER")
        if not marker and not expected:
            raise RuntimeError("R5 requires the owned PostgreSQL marker or system identifier")
        schema = "r5_" + uuid4().hex
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True)
        with engine.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")), "security_r5_qa")
            if marker:
                self.assertEqual(connection.scalar(text("SELECT current_setting('vwflex.qa_worker', true)")), marker)
            if expected:
                self.assertEqual(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")), expected)
            connection.execute(text("CREATE SCHEMA " + schema))

        def cleanup():
            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            engine.dispose()
        self.addCleanup(cleanup)
        return engine

    def blocked_external(self, action):
        def contender():
            with Session(self.engine, autoflush=False) as db:
                db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    action(db)
                    db.commit()
                except OperationalError as error:
                    self.assertEqual(getattr(error.orig, "sqlstate", None), "55P03")
                    db.rollback()
                    return "BLOCKED"
                self.fail("A concurrent writer bypassed the real PostgreSQL locks")
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(contender).result(timeout=5), "BLOCKED")

    def test_sql_proof_never_materializes_persisted_history_snapshots(self):
        self.seed_both()
        snapshot = mutations._WriteGraph.snapshot
        observed = []
        phases = []
        preflight = mutations.preflight_relationship_graph

        def capture_snapshot(graph, row):
            ref = graph.reference(row)
            if ref.model in {model.__name__ for model in HISTORY_MODELS}:
                observed.append(ref)
            return snapshot(graph, row)

        def capture_plan(gate, *args, **kwargs):
            plan = preflight(gate, *args, **kwargs)
            phases.append((gate.graph.media_validation_locked, plan))
            return plan

        with patch.object(mutations._WriteGraph, "snapshot", capture_snapshot), \
                patch.object(mutations, "preflight_relationship_graph", capture_plan):
            self.allow_channel()
        self.assertEqual(observed, [], "History identities/payloads were materialized in Python")
        self.assertEqual([flag for flag, _ in phases], [False, True])
        self.assertEqual(phases[0][1].telemetry, phases[1][1].telemetry)
        for _, plan in phases:
            for model in HISTORY_MODELS:
                certificates = [proof for proof in plan.telemetry if proof.model == model.__name__]
                self.assertTrue(certificates)
                self.assertTrue(all(proof.count == HISTORY_SIZE for proof in certificates))
                self.assertTrue(all(ref.model not in {item.__name__ for item in HISTORY_MODELS}
                                    for proof in certificates for ref in proof.references))
            self.assertFalse(any(ref.model in {item.__name__ for item in HISTORY_MODELS}
                                 for ref in plan.checked))

    def test_existing_history_owner_and_branch_updates_wait_for_locked_proof(self):
        history = self.seed_both()
        with self.channel_guard(self.db):
            for model in HISTORY_MODELS:
                for field in ("client_id", "branch_id"):
                    with self.subTest(model=model.__name__, field=field):
                        self.blocked_external(lambda db, model=model, field=field: db.execute(
                            update(model).where(model.id == history[model][-1])
                            .values({field: self.ids["B"][TARGET_KEYS[field]]})))
        self.db.rollback()

    def new_history(self, model, *, foreign=False):
        ids = self.ids["A"]
        row = dict(id=str(uuid4()), player_device_id=ids["device"], channel_id=ids["channel"],
                   client_id=self.ids["B"]["client"] if foreign else ids["client"], branch_id=ids["branch"])
        if model is PlayerPlaybackEvent:
            row["started_at"] = OLD_TIME
        return row

    def test_concurrent_heartbeat_and_playback_event_inserts_wait_for_anchors(self):
        self.seed_both(2)
        with self.channel_guard(self.db):
            for model in HISTORY_MODELS:
                for foreign in (False, True):
                    with self.subTest(model=model.__name__, foreign=foreign):
                        values = self.new_history(model, foreign=foreign)
                        self.blocked_external(lambda db, model=model, values=values:
                                              db.execute(insert(model), values))
        self.db.rollback()

    def test_concurrent_cross_tenant_incoming_fk_update_waits_for_anchor(self):
        self.seed_both(1)
        foreign = {model: self.history_rows(model, 1, side="B", offset=100000)[0]
                   for model in HISTORY_MODELS}
        with self.channel_guard(self.db):
            for model in HISTORY_MODELS:
                self.blocked_external(lambda db, model=model: db.execute(update(model)
                    .where(model.id == foreign[model]).values(channel_id=self.ids["A"]["channel"])))
        self.db.rollback()

    def test_parent_update_and_delete_cannot_race_the_confirmed_history_proof(self):
        self.seed_both(2)
        with self.channel_guard(self.db):
            for model, identity in ((Channel, self.ids["A"]["channel"]),
                                    (PlayerDevice, self.ids["A"]["device"]),
                                    (Branch, self.ids["A"]["branch"])):
                with self.subTest(model=model.__name__):
                    field = "device_name" if model is PlayerDevice else "name"
                    self.blocked_external(lambda db, model=model, identity=identity, field=field:
                        db.execute(update(model).where(model.id == identity).values({field: "racing"})))
                    self.blocked_external(lambda db, model=model, identity=identity:
                        db.execute(delete(model).where(model.id == identity)))
        self.db.rollback()

    def test_mutation_attempt_between_passes_waits_and_confirmation_is_fresh(self):
        history = self.seed_both(2)
        phases = []
        preflight = mutations.preflight_relationship_graph
        lock_paths = media.lock_media_paths
        attempted = []

        def record(gate, *args, **kwargs):
            plan = preflight(gate, *args, **kwargs)
            phases.append(gate.graph.media_validation_locked)
            return plan

        def between(db, paths):
            if not attempted:
                attempted.append(True)
                for model in HISTORY_MODELS:
                    self.blocked_external(lambda other, model=model: other.execute(update(model)
                        .where(model.id == history[model][-1]).values(client_id=self.ids["B"]["client"])))
            return lock_paths(db, paths)

        with patch.object(mutations, "preflight_relationship_graph", record), \
                patch.object(media, "lock_media_paths", between):
            self.allow_channel()
        self.assertEqual(phases, [False, True])
        self.assertEqual(attempted, [True])

    def test_invalid_new_history_committed_before_anchor_lock_is_observed(self):
        snapshot = mutations._WriteGraph.snapshot
        inserted = []

        def before_anchor(graph, row):
            ref = graph.reference(row)
            if graph.locking and ref == RowRef("Channel", self.ids["A"]["channel"]) and not inserted:
                inserted.append(True)
                with Session(self.engine, autoflush=False) as other:
                    other.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    for model in HISTORY_MODELS:
                        other.execute(insert(model), self.new_history(model, foreign=True))
                    other.commit()
            return snapshot(graph, row)

        with patch.object(mutations._WriteGraph, "snapshot", before_anchor):
            self.deny_channel()
        self.assertEqual(inserted, [True])

    def test_certificate_cannot_survive_transaction_or_gate_binding_drift(self):
        self.seed_both(1)
        user = self.db.get(User, self.ids["A"]["user"])
        channel = self.db.get(Channel, self.ids["A"]["channel"])
        with mutations.delete_guard(self.db, (channel,), actor=user, operation="delete_channel") as approval:
            self.assertTrue(approval.telemetry)
            gate = approval.plans[0]._gate
            original = gate.token
            gate.token = object()
            try:
                with self.assertRaises(HTTPException) as caught:
                    mutations.require_delete_coverage(self.db,
                        (RowRef("PlayerHeartbeat", str(UUID(int=1))),))
                self.assertEqual(caught.exception.status_code, 409)
            finally:
                gate.token = original
        self.db.rollback()

    def test_parent_owner_committed_before_its_lock_is_compared_fresh(self):
        self.seed_both(2)
        snapshot = mutations._WriteGraph.snapshot
        changed = []

        def before_parent_lock(graph, row):
            ref = graph.reference(row)
            if graph.locking and ref == RowRef("ContentItem", self.ids["A"]["content"]) and not changed:
                changed.append(True)
                with Session(self.engine, autoflush=False) as other:
                    other.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    other.execute(update(ContentItem).where(ContentItem.id == ref.identity)
                                  .values(client_id=self.ids["B"]["client"]))
                    other.commit()
            return snapshot(graph, row)

        with patch.object(mutations._WriteGraph, "snapshot", before_parent_lock):
            self.deny_channel()
        self.assertEqual(changed, [True])
        self.assertEqual(self.db.get(ContentItem, self.ids["A"]["content"]).client_id,
                         self.ids["B"]["client"])

    def test_delete_certificate_is_not_usable_after_its_transaction_scope(self):
        self.seed_both(1)
        user = self.db.get(User, self.ids["A"]["user"])
        channel = self.db.get(Channel, self.ids["A"]["channel"])
        reference = RowRef("PlayerHeartbeat", str(UUID(int=1)))
        with mutations.delete_guard(self.db, (channel,), actor=user, operation="delete_channel") as approval:
            self.assertTrue(approval.telemetry)
            mutations.require_delete_coverage(self.db, (reference,))
        self.db.rollback()
        with self.assertRaises(HTTPException) as caught:
            mutations.require_delete_coverage(self.db, (reference,))
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (409, "TENANT_RELATION_CONFLICT"))

    def test_two_pass_certificate_mismatch_rejects_admin_player_and_delete(self):
        self.seed_both(2)
        real_preflight = mutations.preflight_relationship_graph
        ids = self.ids["A"]
        for kind in ("admin", "player", "delete"):
            with self.subTest(guard=kind):
                plans = []

                def changed_second_pass(gate, *args, **kwargs):
                    plan = real_preflight(gate, *args, **kwargs)
                    plans.append(plan)
                    self.assertTrue(plan.telemetry)
                    if len(plans) == 2:
                        first = plan.telemetry[0]
                        plan = replace(plan, telemetry=(replace(first, count=first.count + 1),)
                                       + plan.telemetry[1:])
                    return plan

                if kind == "admin":
                    scope = self.channel_guard(self.db)
                elif kind == "player":
                    scope = mutations.player_write_guard(self.db,
                        channel=self.db.get(Channel, ids["channel"]),
                        player_device=self.db.get(PlayerDevice, ids["device"]),
                        operation="activate_player", intents=(mutations.MutationIntent(
                            "UPDATE", RowRef("PlayerDevice", ids["device"]), ()),))
                else:
                    scope = mutations.delete_guard(self.db,
                        (self.db.get(Channel, ids["channel"]),),
                        actor=self.db.get(User, ids["user"]), operation="delete_channel")
                try:
                    with patch.object(mutations, "preflight_relationship_graph", changed_second_pass):
                        with self.assertRaises(HTTPException) as caught:
                            with scope:
                                self.fail("A mismatched telemetry certificate reached the writer")
                    self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                     (409, "TENANT_RELATION_CONFLICT"))
                    self.assertEqual(len(plans), 2)
                finally:
                    self.db.rollback()
