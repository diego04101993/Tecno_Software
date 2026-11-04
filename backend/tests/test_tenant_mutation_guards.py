"""A6 behavior and in-memory mutations. Fixtures contain no real credentials."""
import io
import json
import os
import uuid
import unittest
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.engine import make_url

from tenant_graph_cases import GraphFixture, operation
from app.models.entities import Branch, Channel, Campaign, CampaignSequenceItem, ContentFolder, Dataset, DatasetImport, DatasetRow, DeviceOutput, LayoutRevision, Schedule, User
from app.services import tenant_relations as registry, tenant_queries as queries, tenant_responses as responses
from app.services import tenant_ownership as ownership, tenant_mutations as mutations
from app.services.tenant_ownership import OwnerState, OwnerCandidates, RowRef, context_for_actor
from app.services.tenant_responses import OMIT, ProjectionSpec


class TenantA8WriteEntryTests(GraphFixture):
    def test_client_user_historical_foreign_branch_cannot_be_silently_cleared(self):
        from app.api.routes.clients import update_client_user, update_client_user_status
        from app.schemas.domain import UserUpdate, UserStatusUpdate
        a, b = self.rows["A"], self.rows["B"]
        self.corrupt(a["user"], branch_id=b["branch"].id)
        for handler, payload in ((update_client_user, UserUpdate(full_name="Rename")),
                                 (update_client_user_status, UserStatusUpdate(status="suspended"))):
            with self.subTest(handler=handler.__name__), self.no_effects():
                with self.assertRaises(HTTPException) as caught:
                    handler(a["client"].id, a["user"].id, payload, self.db, self.admin)
                self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                 (409, "TENANT_RELATION_CONFLICT"))
            self.db.rollback()

    def test_client_user_proposed_foreign_branch_precedes_email_assignment(self):
        from app.api.routes.clients import update_client_user
        from app.schemas.domain import UserUpdate
        a, b = self.rows["A"], self.rows["B"]
        with self.no_effects():
            with self.assertRaises(HTTPException):
                update_client_user(a["client"].id, a["operator"].id,
                    UserUpdate(email="new-a8@example.com", branch_id=b["branch"].id), self.db, self.admin)

    def test_user_alias_create_and_client_create_keep_valid_identity_and_scope(self):
        from app.api.routes.users import create_user
        from app.api.routes.clients import create_client_user, update_client_user, update_client_user_status
        from app.schemas.domain import UserCreate, UserUpdate, UserStatusUpdate
        a = self.rows["A"]
        for index, handler in enumerate((create_user, create_client_user)):
            payload = UserCreate(email=f"a8-create-{index}@example.com", full_name="QA",
                password="A8-Synthetic-Only!42", role="operator", client_id=a["client"].id,
                branch_id=a["branch"].id)
            args = (payload,) if handler is create_user else (a["client"].id, payload)
            result = handler(*args, self.db, self.admin)
            self.assertEqual((result.client_id, result.branch_id), (a["client"].id, a["branch"].id))
            updated = update_client_user(a["client"].id, result.id, UserUpdate(full_name="Updated"), self.db, self.admin)
            self.assertEqual(updated.full_name, "Updated")
            suspended = update_client_user_status(a["client"].id, result.id,
                UserStatusUpdate(status="suspended"), self.db, self.admin)
            self.assertFalse(suspended.is_active)

    def test_demotion_and_deactivation_use_the_same_locked_invariant_as_delete(self):
        from app.services import user_admin
        from app.models.entities import UserRole
        self.add(User, role=UserRole.SUPER_ADMIN, email="second-a8@example.com",
                 full_name="Second", password_hash="synthetic")
        self.db.commit()
        for change in ({"next_role": UserRole.STAFF_ADMIN}, {"next_status": "suspended"}):
            with self.subTest(change=change), self.no_effects():
                with patch.object(user_admin, "lock_super_admin_delete_set",
                                  wraps=user_admin.lock_super_admin_delete_set) as locked:
                    user_admin.ensure_super_admin_survives(db=self.db, target_user=self.admin, **change)
                    locked.assert_called_once_with(self.db)

    def test_schedule_name_patch_rejects_historical_foreign_relation_before_assignment(self):
        from app.api.routes.schedules import update_schedule
        from app.schemas.domain import ScheduleUpdate
        a, b = self.rows["A"], self.rows["B"]
        schedule = self.add(Schedule, client_id=a["client"].id, branch_id=a["branch"].id,
                            channel_id=a["channel"].id, campaign_id=b["campaign"].id,
                            title="Historical corruption")
        self.db.commit()
        for actor in (a["user"], self.admin):
            with self.subTest(actor=actor.role), self.no_effects():
                with self.assertRaises(HTTPException) as caught:
                    update_schedule(schedule.id, ScheduleUpdate(title="Rename"), self.db, actor)
                self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                 (409, "TENANT_RELATION_CONFLICT"))

    def test_schedule_proposed_foreign_relation_cannot_follow_an_earlier_setter(self):
        from app.api.routes.schedules import update_schedule
        from app.schemas.domain import ScheduleUpdate
        a, b = self.rows["A"], self.rows["B"]
        schedule = self.add(Schedule, client_id=a["client"].id, branch_id=a["branch"].id,
                            channel_id=a["channel"].id, campaign_id=a["campaign"].id,
                            title="Original")
        self.db.commit()
        with self.no_effects():
            with self.assertRaises(HTTPException):
                update_schedule(schedule.id, ScheduleUpdate(campaign_id=a["campaign"].id,
                                                           layout_id=b["layout"].id), self.db, a["user"])


class TenantCoreBehaviorTests(GraphFixture):
    def test_direct_structural_and_foreign_ownership(self):
        for side in ("A", "B"):
            ctx = self.contexts[side]
            other = "B" if side == "A" else "A"
            with self.no_effects():
                for key in ("channel", "layout", "campaign", "item", "output", "row"):
                    self.assertEqual(self.graph.resolve_owner(self.rows[side][key], ctx.client_id).state, OwnerState.OWNED)
                    self.assertEqual(self.graph.resolve_owner(self.rows[other][key], ctx.client_id).state, OwnerState.FOREIGN)

    def test_unknown_and_ambiguous_never_authorize(self):
        with self.no_effects():
            for value in (None, {}, object(), RowRef("Channel", "absent")):
                self.assertEqual(self.graph.resolve_owner(value).state, OwnerState.UNKNOWN)
            candidates = OwnerCandidates(tuple(self.ref(self.rows[s]["channel"]) for s in ("A", "B")))
            self.assertEqual(self.graph.resolve_owner(candidates).state, OwnerState.AMBIGUOUS)
            with self.assertRaises(HTTPException):
                self.graph.authorize_row(RowRef("Channel", "absent"), self.contexts["A"])
            with self.assertRaises(HTTPException):
                self.graph.authorize_row(candidates, self.contexts["A"])

    def test_detached_rejected_expired_reloaded_without_flush(self):
        row = self.rows["A"]["channel"]
        ref = self.ref(row)
        self.db.expire(row)
        with self.no_effects():
            self.assertEqual(self.graph.resolve_owner(row).state, OwnerState.OWNED)
        self.db.expunge(row)
        with self.no_effects():
            self.assertEqual(self.graph.resolve_owner(row).state, OwnerState.UNKNOWN)
            self.assertEqual(self.graph.resolve_owner(ref).state, OwnerState.OWNED)

    def test_secondary_foreign_fk_never_transfers_authoritative_owner(self):
        a, b = self.rows["A"], self.rows["B"]
        self.corrupt(a["channel"], branch_id=b["branch"].id)
        with self.no_effects():
            owner = self.graph.resolve_owner(a["channel"])
            self.assertEqual(owner.client_id, self.contexts["A"].client_id)
            for ctx in (self.contexts["A"], self.global_context):
                with self.assertRaises(HTTPException) as caught:
                    self.graph.assert_integrity(self.ref(a["channel"]), ctx)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_global_principal_distinct_from_unknown_and_inconsistent(self):
        with self.no_effects():
            self.assertEqual(self.graph.resolve_owner(self.admin).state, OwnerState.GLOBAL_PRINCIPAL)
        self.corrupt(self.admin, client_id=self.rows["A"]["client"].id)
        with self.no_effects():
            self.assertEqual(self.graph.resolve_owner(self.admin).state, OwnerState.INCONSISTENT)
            with self.assertRaises(HTTPException):
                context_for_actor(self.db, self.admin)

    def test_missing_parent_and_cycle_fail_closed(self):
        row = self.rows["A"]["item"]
        original = self.graph.snapshot
        parent = self.ref(self.rows["A"]["campaign"])
        with patch.object(self.graph, "snapshot", side_effect=lambda r: None if self.graph.reference(r) == parent else original(r)):
            with self.no_effects():
                self.assertEqual(self.graph.resolve_owner(row).state, OwnerState.UNKNOWN)
        ref = self.ref(row)
        with self.no_effects():
            self.assertEqual(self.graph.resolve_owner(ref, _visited=frozenset((ref,))).state, OwnerState.INCONSISTENT)

    def test_global_without_parent_and_explicit_parent_scope(self):
        with self.no_effects():
            statement = queries.tenant_safe_query(select(Channel), Channel, self.global_context)
            self.assertEqual(len(self.db.scalars(statement).all()), 2)
            ctx = context_for_actor(self.db, self.admin, parent=self.rows["A"]["client"])
            statement = queries.tenant_safe_query(select(Channel), Channel, ctx)
            self.assertEqual([r.id for r in self.db.scalars(statement)], [self.rows["A"]["channel"].id])

    def test_branch_context_restricts_rows_and_cannot_be_forged(self):
        a = self.rows["A"]
        other = self.add(Branch, client_id=a["client"].id, name="other", code="other")
        channel = self.add(Channel, client_id=a["client"].id, branch_id=other.id, name="other")
        self.db.commit()
        ctx = context_for_actor(self.db, a["operator"])
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(Channel), Channel, ctx)
            self.assertEqual([r.id for r in self.db.scalars(stmt)], [a["channel"].id])
            with self.assertRaises(HTTPException):
                context_for_actor(self.db, a["operator"], branch_id=other.id)

    def test_eligibility_precedes_count_search_and_pagination(self):
        a, b = self.rows["A"], self.rows["B"]
        bad = self.add(Channel, client_id=a["client"].id, branch_id=b["branch"].id, name="000_bad")
        self.db.commit()
        with self.no_effects():
            eligible = queries.tenant_safe_query(select(Channel), Channel, self.contexts["A"])
            count = self.db.scalar(select(func.count()).select_from(eligible.subquery()))
            page = self.db.scalars(eligible.where(Channel.name.contains("A")).order_by(Channel.name).limit(1)).all()
            self.assertEqual(count, 1)
            self.assertEqual([r.id for r in page], [a["channel"].id])
            for unsafe in (select(Channel).limit(1), select(Channel).offset(1), select(Channel).order_by(Channel.name)):
                with self.assertRaises(HTTPException):
                    queries.tenant_safe_query(unsafe, Channel, self.contexts["A"])

    def test_structural_sql_context_and_wrong_dataset_import(self):
        a, b = self.rows["A"], self.rows["B"]
        extra = self.add(Dataset, client_id=a["client"].id, data_source_id=a["source"].id, name="other", slug="other")
        imp = self.add(DatasetImport, dataset_id=extra.id, source_filename="x.csv", storage_path="")
        self.db.commit()
        self.corrupt(a["row"], import_id=imp.id)
        with self.no_effects():
            statement = queries.tenant_safe_query(select(DatasetRow), DatasetRow, self.contexts["A"])
            self.assertEqual(self.db.scalars(statement).all(), [])
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(a["row"]), self.contexts["A"])

    def test_safe_optional_projection_is_copy_and_essential_is_omitted(self):
        a, b = self.rows["A"], self.rows["B"]
        self.corrupt(a["campaign"], layout_id=b["layout"].id)
        self.corrupt(a["channel"], branch_id=b["branch"].id)
        with self.no_effects():
            result = responses.tenant_safe_response(self.graph, a["campaign"], self.contexts["A"],
                                                    ProjectionSpec("Campaign", ("id", "name", "layout_id")))
            self.assertEqual(result, {"id": a["campaign"].id, "name": "A", "layout_id": None})
            self.assertEqual(self.graph.snapshot(a["campaign"])["layout_id"], b["layout"].id)
            self.assertIs(responses.tenant_safe_response(self.graph, a["channel"], self.contexts["A"],
                          ProjectionSpec("Channel", ("id", "name", "branch_id"))), OMIT)

    def test_opaque_fields_need_semantic_adapter_and_secrets_forbidden(self):
        a = self.rows["A"]
        with self.no_effects():
            self.assertIs(responses.tenant_safe_response(self.graph, a["content"], self.contexts["A"],
                          ProjectionSpec("ContentItem", ("id", "file_path", "metadata_json"))), OMIT)
            with self.assertRaises(HTTPException):
                ProjectionSpec("User", ("id", "password_hash"))

    def test_attribution_global_allowed_foreign_neutralized(self):
        a, b = self.rows["A"], self.rows["B"]
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="r", created_by_user_id=self.admin.id)
        self.db.commit()
        projection = ProjectionSpec("LayoutRevision", ("id", "created_by_user_id"))
        with self.no_effects():
            result = responses.tenant_safe_response(self.graph, revision, self.contexts["A"], projection)
            self.assertEqual(result["created_by_user_id"], self.admin.id)
        self.corrupt(revision, created_by_user_id=b["user"].id)
        with self.no_effects():
            result = responses.tenant_safe_response(self.graph, revision, self.contexts["A"], projection)
            self.assertIsNone(result["created_by_user_id"])

    def test_plan_is_immutable_bound_and_not_executed(self):
        a = self.rows["A"]
        ctx = self.contexts["A"]
        ref = self.ref(a["channel"])
        intent = mutations.MutationIntent("UPDATE", ref, (("name", "changed"),))
        with self.no_effects():
            with mutations.MutationGate(self.graph, ctx) as gate:
                plan = mutations.preflight_relationship_graph(gate, (intent,), operation("update_channel"))
                self.assertTrue(plan.assert_usable(gate, ctx))
                self.assertEqual(self.graph.snapshot(ref)["name"], "A")
                self.assertEqual(plan.proposed[0].values["name"], "changed")
                with self.assertRaises(TypeError):
                    plan.proposed[0].values["name"] = "illegal"
                with self.assertRaises(FrozenInstanceError):
                    plan.intents = ()
            with self.assertRaises(HTTPException):
                plan.assert_usable(gate, ctx)

    def test_foreign_incoming_cascade_and_setnull_block_before_any_write(self):
        for source, foreign_key, target in (("channel", "branch_id", "branch"),
                                            ("campaign", "layout_id", "layout")):
            for side in ("A", "B"):
                other = "B" if side == "A" else "A"
                self.corrupt(self.rows[other][source], **{foreign_key: self.rows[side][target].id})
                ctx = self.contexts[side]
                with self.no_effects():
                    with mutations.MutationGate(self.graph, ctx) as gate:
                        with self.assertRaises(HTTPException):
                            mutations.preflight_relationship_graph(
                                gate, (mutations.MutationIntent("DELETE", self.ref(self.rows[side][target])),),
                                operation("delete_client"))
                self.db.rollback()
                self.corrupt(self.rows[other][source], **{foreign_key: self.rows[other][target].id})

    def test_generic_error_never_reveals_foreign_input(self):
        a, b = self.rows["A"], self.rows["B"]
        self.corrupt(a["channel"], branch_id=b["branch"].id)
        with self.no_effects():
            with self.assertRaises(HTTPException) as caught:
                self.graph.assert_integrity(self.ref(a["channel"]), self.global_context)
            serialized = json.dumps({"detail": caught.exception.detail})
            self.assertEqual(serialized, '{"detail": "TENANT_RELATION_CONFLICT"}')
            self.assertNotIn(b["branch"].id, serialized)

    def test_update_validates_old_and_new_omitted_null_and_foreign(self):
        a, b = self.rows["A"], self.rows["B"]
        op = operation("update_campaign_playback_mode")
        with self.no_effects():
            proposal = mutations.validate_proposed_state(self.graph, a["campaign"], {"name": "new"}, self.contexts["A"], op)
            self.assertEqual(proposal.values["layout_id"], a["layout"].id)
            proposal = mutations.validate_proposed_state(self.graph, a["campaign"], {"layout_id": None}, self.contexts["A"], op)
            self.assertIsNone(proposal.values["layout_id"])
            with self.assertRaises(HTTPException):
                mutations.validate_proposed_state(self.graph, a["campaign"], {"layout_id": b["layout"].id}, self.contexts["A"], op)
        self.corrupt(a["campaign"], layout_id=b["layout"].id)
        with self.no_effects():
            with self.assertRaises(HTTPException):
                mutations.validate_proposed_state(self.graph, a["campaign"], {"layout_id": None}, self.contexts["A"], op)

    def test_dirty_session_is_rejected_without_flush(self):
        row = self.rows["A"]["channel"]
        row.name = "not yet authorized"
        with self.assertRaises(HTTPException):
            mutations.validate_proposed_state(self.graph, row, {}, self.contexts["A"], operation("update_channel"))
        self.assertIn(row, self.db.dirty)
        self.db.rollback()

    def test_create_proposal_loads_parent_evidence_without_creating_orm(self):
        a, b = self.rows["A"], self.rows["B"]
        ref = RowRef("Channel", "new-synthetic-channel")
        values = {"client_id": a["client"].id, "branch_id": a["branch"].id, "name": "new"}
        with self.no_effects():
            with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
                plan = mutations.preflight_relationship_graph(gate, (
                    mutations.MutationIntent("CREATE", ref, tuple(values.items())),
                ), operation("create_channel"))
                self.assertTrue(plan.assert_usable(gate, self.contexts["A"]))
                self.assertIsNone(plan.proposed[0].previous)
                self.assertIsNone(self.graph.snapshot(ref))
                for bad in (b["branch"].id, ["invalid"], 123, True, None):
                    with self.assertRaises(HTTPException):
                        mutations.validate_proposed_state(self.graph, ref, dict(values, branch_id=bad),
                                                         self.contexts["A"], operation("create_channel"), create=True)

    def test_proposed_parent_cannot_invalidate_existing_dependent(self):
        a = self.rows["A"]
        branch = self.add(Branch, client_id=a["client"].id, name="other", code="other")
        self.add(Schedule, client_id=a["client"].id, branch_id=a["branch"].id,
                 channel_id=a["channel"].id, campaign_id=a["campaign"].id, title="local")
        self.db.commit()
        with self.no_effects():
            with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
                with self.assertRaises(HTTPException):
                    mutations.preflight_relationship_graph(gate, (
                        mutations.MutationIntent("UPDATE", self.ref(a["channel"]), (("branch_id", branch.id),)),
                    ), operation("update_channel"))

    def test_corrupt_structural_parent_is_excluded_in_sql_and_response(self):
        a, b = self.rows["A"], self.rows["B"]
        self.corrupt(a["channel"], branch_id=b["branch"].id)
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(DeviceOutput), DeviceOutput, self.contexts["A"])
            self.assertEqual(self.db.scalars(stmt).all(), [])
            self.assertIs(responses.tenant_safe_response(
                self.graph, a["output"], self.contexts["A"],
                ProjectionSpec("DeviceOutput", ("id", "name", "channel_id"))), OMIT)

    def test_branch_context_applies_to_output_target_too(self):
        a = self.rows["A"]
        branch = self.add(Branch, client_id=a["client"].id, name="other", code="other")
        channel = self.add(Channel, client_id=a["client"].id, branch_id=branch.id, name="other")
        self.db.commit()
        self.corrupt(a["output"], channel_id=channel.id)
        context = context_for_actor(self.db, a["operator"])
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(DeviceOutput), DeviceOutput, context)
            self.assertEqual(self.db.scalars(stmt).all(), [])
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(a["output"]), context)

    def test_global_directory_requires_explicit_typed_principals(self):
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(User), User, self.global_context,
                                             include_global_principals=True)
            self.assertIn(self.admin.id, [u.id for u in self.db.scalars(stmt)])
            projection = ProjectionSpec("User", ("id", "full_name", "client_id"), allow_global_principal=True)
            result = responses.tenant_safe_response(self.graph, self.admin, self.global_context, projection)
            self.assertIsNone(result["client_id"])
            self.assertIs(responses.tenant_safe_response(self.graph, self.admin, self.contexts["A"], projection), OMIT)
            with self.assertRaises(HTTPException):
                queries.tenant_safe_query(select(User), User, self.contexts["A"], include_global_principals=True)

    def test_unknown_opaque_mutation_is_blocked_and_nested_proposal_is_frozen(self):
        a = self.rows["A"]
        self.corrupt(a["channel"], expanded_outputs=[{"unproved_reference": "synthetic"}])
        with self.no_effects():
            with self.assertRaises(HTTPException):
                mutations.validate_proposed_state(self.graph, a["channel"], {"name": "new"},
                                                 self.contexts["A"], operation("update_channel"))
        intent = mutations.MutationIntent("UPDATE", self.ref(a["channel"]),
                                         (("expanded_outputs", [{"x": ["a"]}]),))
        with self.assertRaises(TypeError):
            intent.patch[0][1][0]["x"] = []

    def test_runtime_requires_active_owner_but_admin_read_keeps_inactive(self):
        a = self.rows["A"]
        self.corrupt(a["client"], status="suspended")
        with self.no_effects():
            read = queries.tenant_safe_query(select(DeviceOutput), DeviceOutput, self.contexts["A"])
            runtime = queries.tenant_safe_query(select(DeviceOutput), DeviceOutput, self.contexts["A"], profile="RUNTIME")
            self.assertEqual(len(self.db.scalars(read).all()), 1)
            self.assertEqual(self.db.scalars(runtime).all(), [])
            spec = ProjectionSpec("DeviceOutput", ("id", "name"))
            self.assertIs(responses.tenant_safe_response(self.graph, a["output"], self.contexts["A"], spec,
                                                        profile="RUNTIME"), OMIT)

    def test_two_session_plan_cannot_cross_transaction_or_context(self):
        a = self.rows["A"]
        with self.no_effects():
            with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
                plan = mutations.preflight_relationship_graph(gate, (
                    mutations.MutationIntent("UPDATE", self.ref(a["channel"]), (("name", "new"),)),
                ), operation("update_channel"))
                with self.assertRaises(HTTPException):
                    plan.assert_usable(gate, self.contexts["B"])
                another_gate = mutations.MutationGate(self.graph, self.contexts["A"])
                with self.assertRaises(HTTPException):
                    plan.assert_usable(another_gate, self.contexts["A"])
        self.db.rollback()
        with self.assertRaises(HTTPException):
            plan.assert_usable(gate, self.contexts["A"])

    def test_explicit_handler_writes_only_after_plan_approval(self):
        a, b = self.rows["A"], self.rows["B"]
        with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
            with self.no_effects():
                plan = mutations.preflight_relationship_graph(gate, (
                    mutations.MutationIntent("UPDATE", self.ref(a["channel"]), (("name", "approved"),)),
                ), operation("update_channel"))
                plan.assert_usable(gate, self.contexts["A"])
            a["channel"].name = "approved"
            self.db.commit()
        self.assertEqual(self.graph.snapshot(a["channel"])["name"], "approved")
        self.assertEqual(self.graph.snapshot(b["channel"])["name"], "B")

    def test_folder_cycle_is_neutral_in_read_and_blocked_in_write(self):
        a = self.rows["A"]
        self.corrupt(a["folder"], parent_id=a["folder"].id)
        with self.no_effects():
            result = responses.tenant_safe_response(self.graph, a["folder"], self.contexts["A"],
                                                    ProjectionSpec("ContentFolder", ("id", "name", "parent_id")))
            self.assertIsNone(result["parent_id"])
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(a["folder"]), self.contexts["A"])

    def test_opt_in_adapters_use_persisted_identity_without_autoflush(self):
        from app.api.deps import get_tenant_operation_context
        from app.services.tenancy import tenant_operation_context
        a, b = self.rows["A"], self.rows["B"]
        with self.no_effects():
            self.assertEqual(get_tenant_operation_context(a["user"], self.db), self.contexts["A"])
        a["user"].client_id = b["client"].id
        effects = []
        from sqlalchemy import event
        def on_flush(*args): effects.append("FLUSH")
        event.listen(self.db, "before_flush", on_flush)
        try:
            ctx = tenant_operation_context(self.db, a["user"])
            self.assertEqual(ctx.client_id, self.contexts["A"].client_id)
            self.assertEqual(effects, [])
            with self.assertRaises(HTTPException):
                with mutations.MutationGate(self.graph, ctx):
                    self.fail("Dirty identity was accepted")
        finally:
            event.remove(self.db, "before_flush", on_flush)
            self.db.rollback()

    def test_parent_resource_scope_cannot_mix_same_tenant_siblings(self):
        a = self.rows["A"]
        other = self.add(Campaign, client_id=a["client"].id, name="other")
        other_item = self.add(CampaignSequenceItem, campaign_id=other.id, item_type="content",
                              content_id=a["content"].id)
        self.db.commit()
        ctx = context_for_actor(self.db, self.admin, parent=a["campaign"],
                                parent_relation="CampaignSequenceItem.campaign_id")
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(CampaignSequenceItem), CampaignSequenceItem, ctx)
            self.assertEqual([r.id for r in self.db.scalars(stmt)], [a["item"].id])
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(other_item), ctx)
            with self.assertRaises(HTTPException):
                context_for_actor(self.db, self.admin, parent=a["campaign"])
        child = self.add(ContentFolder, client_id=a["client"].id, name="child", parent_id=a["folder"].id)
        self.db.commit()
        ctx = context_for_actor(self.db, self.admin, parent=a["folder"],
                                parent_relation="ContentFolder.parent_id")
        with self.no_effects():
            stmt = queries.tenant_safe_query(select(ContentFolder), ContentFolder, ctx)
            self.assertEqual([r.id for r in self.db.scalars(stmt)], [child.id])


    def r1_schedule(self):
        a = self.rows["A"]
        row = self.add(Schedule, client_id=a["client"].id, campaign_id=a["campaign"].id,
                       channel_id=a["channel"].id, branch_id=a["branch"].id, title="R1")
        self.db.commit()
        return row

    def test_r1_valid_plan_records_dependency_closure(self):
        row = self.r1_schedule()
        with self.no_effects():
            with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
                plan = mutations.preflight_relationship_graph(gate,
                    (mutations.MutationIntent("UPDATE", self.ref(row), (("title", "valid"),)),),
                    operation("update_schedule"))
                self.assertTrue({self.ref(row), self.ref(self.rows["A"]["campaign"]),
                                 self.ref(self.rows["A"]["layout"])} <= set(plan.checked))
        self.db.rollback()

    def test_r1_transitive_schedule_preflight(self):
        row = self.r1_schedule()
        self.corrupt(self.rows["A"]["campaign"], layout_id=self.rows["B"]["layout"].id)
        with self.no_effects(), patch("os.unlink", side_effect=AssertionError("unlink")):
            with mutations.MutationGate(self.graph, self.contexts["A"]) as gate:
                with self.assertRaises(HTTPException) as caught:
                    mutations.preflight_relationship_graph(gate,
                        (mutations.MutationIntent("UPDATE", self.ref(row), (("title", "new"),)),),
                        operation("update_schedule"))
                self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                 (409, "TENANT_RELATION_CONFLICT"))

    def test_r1_transitive_schedule_query(self):
        row = self.r1_schedule()
        for profile in ("READ", "RUNTIME"):
            eligible = queries.tenant_safe_query(select(Schedule), Schedule, self.contexts["A"], profile=profile)
            self.assertEqual(self.db.scalar(select(func.count()).select_from(eligible.subquery())), 1)
        self.corrupt(self.rows["A"]["campaign"], layout_id=self.rows["B"]["layout"].id)
        with self.no_effects():
            for profile in ("READ", "RUNTIME"):
                eligible = queries.tenant_safe_query(select(Schedule), Schedule, self.contexts["A"], profile=profile)
                self.assertEqual(self.db.scalar(select(func.count()).select_from(eligible.subquery())), 0)
                self.assertEqual(list(self.db.scalars(eligible.order_by(Schedule.id).limit(1).offset(0))), [])
        self.corrupt(self.rows["A"]["campaign"], layout_id=self.rows["A"]["layout"].id)
        self.corrupt(row, campaign_id=self.rows["B"]["campaign"].id)
        with self.no_effects():
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(row), self.contexts["A"])
            for profile in ("READ", "RUNTIME"):
                eligible = queries.tenant_safe_query(select(Schedule), Schedule, self.contexts["A"], profile=profile)
                self.assertEqual(list(self.db.scalars(eligible)), [])

    def test_r1_owner_and_context_are_separate_for_all_actors(self):
        from app.models.entities import UserRole
        row = self.r1_schedule()
        self.corrupt(self.rows["A"]["campaign"], layout_id=self.rows["B"]["layout"].id)
        actors = [self.rows["A"]["user"]]
        for role in ("staff_operator", "staff_admin", "super_admin"):
            actors.append(self.add(User, role=UserRole(role), email=role+"@example.com",
                                   full_name="Synthetic", password_hash="synthetic"))
        self.db.commit()
        for actor in actors:
            ctx = context_for_actor(self.db, actor, client_id=self.rows["A"]["client"].id)
            with self.no_effects():
                self.assertEqual(self.graph.resolve_owner(row).state, OwnerState.OWNED)
                self.assertEqual(self.graph.resolve_owner(row).client_id, self.rows["A"]["client"].id)
                proof = self.graph.context_integrity(self.ref(row), ctx)
                self.assertEqual(proof.state.value, "INCONSISTENT")
                self.assertTrue({self.ref(row), self.ref(self.rows["A"]["campaign"]),
                                 self.ref(self.rows["B"]["layout"])} <= set(proof.checked))
                with self.assertRaises(HTTPException):
                    self.graph.assert_integrity(self.ref(row), ctx)

    def test_r1_dataset_audio_touch_chains(self):
        from app.models.entities import (LayoutDataBinding, AudioPlaylist, AudioPlaylistItem,
            AudioAssignment, TouchExperience, TouchExperienceAssignment, LayoutBindingPreset, AudioPlaylistKind)
        a, b = self.rows["A"], self.rows["B"]
        binding = self.add(LayoutDataBinding, layout_id=a["layout"].id, dataset_id=a["dataset"].id,
                           name="binding", preset_key=LayoutBindingPreset.MENU)
        playlist = self.add(AudioPlaylist, client_id=a["client"].id, name="music", kind=AudioPlaylistKind.MUSIC)
        item = self.add(AudioPlaylistItem, playlist_id=playlist.id, content_id=a["content"].id)
        audio = self.add(AudioAssignment, client_id=a["client"].id, music_playlist_id=playlist.id)
        exp = self.add(TouchExperience, client_id=a["client"].id, name="touch", slug="touch")
        touch = self.add(TouchExperienceAssignment, client_id=a["client"].id,
                         experience_id=exp.id, channel_id=a["channel"].id)
        self.db.commit()
        for row, corrupt_row, field, foreign in (
            (binding, a["dataset"], "data_source_id", b["source"].id),
            (audio, item, "content_id", b["content"].id),
            (touch, a["channel"], "branch_id", b["branch"].id),
        ):
            for profile in ("READ", "WRITE", "DELETE", "RUNTIME"):
                self.assertTrue(self.graph.assert_integrity(self.ref(row), self.contexts["A"], profile=profile))
            original = getattr(corrupt_row, field)
            self.corrupt(corrupt_row, **{field: foreign})
            with self.no_effects():
                for profile in ("READ", "WRITE", "DELETE", "RUNTIME"):
                    with self.assertRaises(HTTPException):
                        self.graph.assert_integrity(self.ref(row), self.contexts["A"], profile=profile)
                for profile in ("READ", "RUNTIME"):
                    stmt = queries.tenant_safe_query(select(type(row)), type(row), self.contexts["A"], profile=profile)
                    self.assertNotIn(row.id, [x.id for x in self.db.scalars(stmt)])
                if row is audio:
                    self.assertIs(responses.tenant_safe_response(self.graph, playlist, self.contexts["A"],
                        ProjectionSpec("AudioPlaylist", ("id", "name"))), OMIT)
            self.corrupt(corrupt_row, **{field: original})

    def test_r1_missing_parent_cycles_and_valid_backlinks(self):
        a = self.rows["A"]
        missing = RowRef("Schedule", "missing-synthetic")
        with self.assertRaises(HTTPException):
            self.graph.assert_integrity(missing, self.contexts["A"])
        with self.no_effects(), self.assertRaises(HTTPException):
            mutations.validate_proposed_state(self.graph, RowRef("Schedule", "synthetic-create"),
                {"client_id": a["client"].id, "campaign_id": "missing-campaign", "title": "missing"},
                self.contexts["A"], operation("create_schedule"), create=True)
        self.corrupt(a["dataset"], current_import_id=a["imp"].id)
        # Dataset -> current import -> same dataset is a provable, valid backlink.
        self.assertTrue(self.graph.assert_integrity(self.ref(a["dataset"]), self.contexts["A"]))
        self.corrupt(a["folder"], parent_id=a["folder"].id)
        with self.no_effects():
            with self.assertRaises(HTTPException):
                self.graph.assert_integrity(self.ref(a["folder"]), self.contexts["A"])
            # Folder RUNTIME is explicitly excluded in the approved registry.
            # READ keeps the optional cyclic parent neutral, never exposes it.
            output = responses.tenant_safe_response(self.graph, self.ref(a["folder"]), self.contexts["A"],
                                                     ProjectionSpec("ContentFolder", ("id", "parent_id")))
            self.assertIsNone(output["parent_id"])


@unittest.skipUnless(os.environ.get("A6_TEST_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL"),
                     "Isolated local PostgreSQL URL required")
class TenantCorePostgresTests(TenantCoreBehaviorTests):
    def make_engine(self):
        url = make_url(os.environ.get("A6_TEST_DATABASE_URL") or os.environ["TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or not (url.database or "").startswith("tenant_a6_")):
            raise RuntimeError("A6 PostgreSQL tests require a dedicated local tenant_a6_ database")
        schema = "a6_" + uuid.uuid4().hex
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
        with engine.begin() as connection:
            if connection.scalar(text("select current_database()")) != url.database:
                raise RuntimeError("Unexpected QA database")
            connection.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            engine.dispose()
        self.addCleanup(cleanup)
        return engine

    def test_gate_blocks_concurrent_cooperating_writer(self):
        from sqlalchemy.orm import Session
        from sqlalchemy.exc import OperationalError
        from concurrent.futures import ThreadPoolExecutor
        context = self.contexts["A"]
        with mutations.MutationGate(self.graph, context):
            def contender():
                with Session(self.engine) as db:
                    db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    graph = ownership.TenantGraph(db)
                    with self.assertRaises(OperationalError) as caught:
                        with mutations.MutationGate(graph, context):
                            self.fail("Second writer entered a held tenant gate")
                    self.assertEqual(caught.exception.orig.sqlstate, "55P03")
                    db.rollback()
                    return True
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(contender).result(timeout=5))
        self.db.rollback()


def run_a6_mutations(*, only=None):
    """Patch Python objects in memory only; a detected mutant must fail assertions."""
    from test_tenant_relation_registry import TenantRelationRegistryTests
    cases = []
    cases.append(("removed_relation", registry, "validate_registry", lambda *a, **k: True,
                  TenantRelationRegistryTests, "test_removed_and_ghost_relation_rejected_by_validator"))
    cases.append(("wrong_owner", ownership.TenantGraph, "resolve_owner",
                  lambda self, row, *a, **k: ownership.OwnerResolution(OwnerState.OWNED, "wrong"),
                  TenantCoreBehaviorTests, "test_direct_structural_and_foreign_ownership"))
    cases.append(("global_bypass", ownership.TenantGraph, "assert_integrity", lambda *a, **k: True,
                  TenantCoreBehaviorTests, "test_secondary_foreign_fk_never_transfers_authoritative_owner"))
    cases.append(("unknown_accepted", ownership.TenantGraph, "authorize_row", lambda *a, **k: True,
                  TenantCoreBehaviorTests, "test_unknown_and_ambiguous_never_authorize"))
    original_resolver = ownership.TenantGraph.resolve_owner
    def ambiguous_allowed(self, value, *args, **kwargs):
        if isinstance(value, OwnerCandidates):
            return ownership.OwnerResolution(OwnerState.OWNED, "unproved")
        return original_resolver(self, value, *args, **kwargs)
    cases.append(("ambiguous_accepted", ownership.TenantGraph, "resolve_owner", ambiguous_allowed,
                  TenantCoreBehaviorTests, "test_unknown_and_ambiguous_never_authorize"))
    cases.append(("pagination_before_filter", queries, "tenant_safe_query", lambda stmt, *a, **k: stmt,
                  TenantCoreBehaviorTests, "test_eligibility_precedes_count_search_and_pagination"))
    cases.append(("foreign_projection", responses, "tenant_safe_response",
                  lambda graph, ref, ctx, spec, **kw: {k: graph.snapshot(ref)[k] for k in spec.fields},
                  TenantCoreBehaviorTests, "test_safe_optional_projection_is_copy_and_essential_is_omitted"))
    original_preflight = mutations.preflight_relationship_graph
    def premature_assignment(gate, intents, operation):
        cls = registry.model_class(intents[0].reference.model)
        obj = gate.graph.session.get(cls, intents[0].reference.identity)
        obj.name = "premature"
        try:
            return original_preflight(gate, intents, operation)
        except HTTPException:
            raise AssertionError("write before preflight")
    cases.append(("write_before_preflight", mutations, "preflight_relationship_graph", premature_assignment,
                  TenantCoreBehaviorTests, "test_plan_is_immutable_bound_and_not_executed"))
    cases.append(("error_leaks_id", ownership, "conflict", lambda: HTTPException(409, "foreign-id-leak"),
                  TenantCoreBehaviorTests, "test_generic_error_never_reveals_foreign_input"))
    cases.append(("ignored_introspection_mismatch", registry, "validate_registry", lambda *a, **k: True,
                  TenantRelationRegistryTests, "test_new_fk_or_phantom_registration_is_rejected"))
    original_edges = ownership.TenantGraph.context_edges
    no_context = lambda self, ref, profile: tuple(e for e in original_edges(self, ref, profile)
                                                  if e.owner_kind != "context")
    cases.append(("context_edge_not_traversed", ownership.TenantGraph, "context_edges", no_context,
                  TenantCoreBehaviorTests, "test_r1_transitive_schedule_preflight"))
    cases.append(("depth_gt_one_ignored", ownership.TenantGraph, "context_edges",
                  lambda self, ref, profile: () if ref.model == "Campaign" else original_edges(self, ref, profile),
                  TenantCoreBehaviorTests, "test_r1_transitive_schedule_preflight"))
    original_context = ownership.TenantGraph.context_integrity
    def global_context_bypass(self, ref, ctx, **kwargs):
        if ctx.actor.role in registry.GLOBAL_ROLES:
            return ownership.ContextIntegrity(ownership.ContextIntegrityState.CONSISTENT, (ref,))
        return original_context(self, ref, ctx, **kwargs)
    cases.append(("global_context_bypass", ownership.TenantGraph, "context_integrity", global_context_bypass,
                  TenantCoreBehaviorTests, "test_r1_owner_and_context_are_separate_for_all_actors"))
    original_edge_sql = queries._edge_predicate
    def query_context_bypass(entity, edge, *args, **kwargs):
        from sqlalchemy import true
        return true() if edge.owner_kind == "context" else original_edge_sql(entity, edge, *args, **kwargs)
    cases.append(("query_context_ignored", queries, "_edge_predicate", query_context_bypass,
                  TenantCoreBehaviorTests, "test_r1_transitive_schedule_query"))
    def preflight_context_bypass(*args, **kwargs):
        with patch.object(ownership.TenantGraph, "context_edges", no_context):
            return original_preflight(*args, **kwargs)
    cases.append(("preflight_context_ignored", mutations, "preflight_relationship_graph", preflight_context_bypass,
                  TenantCoreBehaviorTests, "test_r1_transitive_schedule_preflight"))
    original_fingerprint = registry.relation_fingerprint
    approved_edge = registry.relation_spec("Channel.branch_id")
    for mutation_name, attributes in (
        ("context_structural_drift", ("owner_kind",)),
        ("owner_path_removed", ("authoritative_owner",)),
        ("owner_path_changed", ("authoritative_owner",)),
        ("runtime_applicability_removed", ("operations", "exclusions")),
        ("conflict_strategy_changed", ("conflict_strategy",)),
    ):
        def ignored_policy(edge, attributes=attributes, mutation_name=mutation_name):
            if edge.key == approved_edge.key:
                change = True
                if mutation_name == "owner_path_removed":
                    change = not edge.authoritative_owner
                elif mutation_name == "owner_path_changed":
                    change = bool(edge.authoritative_owner)
                elif mutation_name == "runtime_applicability_removed":
                    change = "RUNTIME" not in edge.operations
                if change:
                    edge = replace(edge, **{k: getattr(approved_edge, k) for k in attributes})
            return original_fingerprint(edge)
        cases.append((mutation_name, registry, "relation_fingerprint", ignored_policy,
                      TenantRelationRegistryTests, "test_semantic_policy_drift_rejected_by_validator"))
    from test_tenant_relation_matrix import _run_delete_mutation, _legacy_defense_sites
    return [_run_delete_mutation(name, target, attribute, replacement, cls, method,
                                 _legacy_defense_sites("A6", name), after_setup=True)
            for name, target, attribute, replacement, cls, method in cases if only is None or name == only]


class TenantA6MutationTests(unittest.TestCase):
    def test_all_in_memory_mutations_detected(self):
        results = run_a6_mutations()
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r["detected"] and r["errors"] == 0 for r in results), results)


class TenantA8MutationEntryTests(GraphFixture):
    def test_direct_upsert_cannot_transfer_foreign_owner(self):
        from app.api.routes import audio
        from app.models.entities import AudioAssignment, AudioPlaylist
        from app.schemas.domain import AudioAssignmentCreate
        a, b = self.rows["A"], self.rows["B"]
        own = self.add(AudioPlaylist, client_id=a["client"].id, name="QA own", kind="music")
        foreign = self.add(AudioPlaylist, client_id=b["client"].id, name="QA foreign", kind="music")
        self.add(AudioAssignment, client_id=b["client"].id, channel_id=a["channel"].id,
                 branch_id=b["branch"].id, music_playlist_id=foreign.id)
        self.db.commit()
        payload = AudioAssignmentCreate(channel_id=a["channel"].id, music_playlist_id=own.id)
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            audio.upsert_audio_assignment(payload, self.db, a["user"])
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (409, "TENANT_RELATION_CONFLICT"))

    def test_legacy_materializers_require_approved_parent_batch(self):
        from app.services import campaign_sequence
        from app.models.entities import CampaignPlaylistItem
        a = self.rows["A"]
        self.add(CampaignPlaylistItem, campaign_id=a["campaign"].id, content_id=a["content"].id, sort_order=1)
        self.db.commit()
        for function in (campaign_sequence.sync_sequence_from_legacy, campaign_sequence.sync_legacy_playlist_from_sequence):
            with self.subTest(function=function.__name__), self.no_effects(), self.assertRaises(HTTPException) as caught:
                function(a["campaign"].id, self.db)
            self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")
