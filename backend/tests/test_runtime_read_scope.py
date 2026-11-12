"""Runtime eligibility must stay exact and confined to one read snapshot."""
from collections import Counter
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.models.entities import Branch, CampaignSequenceItem, CampaignSequenceItemType, ContentItem, ContentType
from app.services import media_references, player_runtime, runtime_read, tenant_queries
from app.services.runtime_read import runtime_read_scope, runtime_reads
from test_tenant_isolation import TenantFixture


class RuntimeReadScopeTests(TenantFixture):
    def setUp(self):
        super().setUp()
        self.a = self.res["A"]
        self.b = self.res["B"]
        self.a["campaign"].layout_id = None
        self.db.commit()

    def runtime(self):
        return player_runtime.build_player_runtime_config(
            self.a["channel"], self.db, "http://testserver", snapshot_coherent=True)

    def sequence_item(self, content, position, duration):
        return self.add(CampaignSequenceItem, campaign_id=self.a["campaign"].id,
                        content_id=content.id, item_type=CampaignSequenceItemType.CONTENT,
                        sort_order=position, duration_seconds=duration)

    def test_one_global_owner_build_and_one_validation_per_content_across_runtime(self):
        first = self.a["content"]
        second = self.add(ContentItem, client_id=first.client_id, name="HTML",
                          type=ContentType.HTML, html_content="<p>Runtime fragment</p>")
        self.sequence_item(first, 2, 21)
        self.sequence_item(second, 3, 8)
        self.db.commit()

        with patch.object(media_references, "media_reference_owners",
                          wraps=media_references.media_reference_owners) as owners, patch.object(
            media_references, "_readable_media_row", wraps=media_references._readable_media_row
        ) as validations:
            payload = self.runtime()
        self.assertEqual(owners.call_count, 1)
        self.assertEqual(Counter((call.args[2].client_id, call.args[2].id)
                                 for call in validations.call_args_list),
                         Counter({(first.client_id, first.id): 1, (second.client_id, second.id): 1}))
        self.assertEqual([row["content_id"] for row in payload["sequence_items"]],
                         [first.id, first.id, second.id])
        self.assertEqual([row["duration_seconds"] for row in payload["sequence_items"]][1:], [21, 8])
        self.assertEqual(payload["campaigns"][0]["playlist_items_count"], 3)
        self.assertEqual([row["id"] for row in payload["contents"]], [first.id, second.id])
        self.assertTrue(payload["media_retention"]["complete"])
        self.assertEqual([asset["content_id"] for asset in payload["media_retention"]["assets"]], [first.id])
        self.assertIsNone(runtime_reads(self.db))

    def test_global_owners_outside_the_batch_still_veto_across_requests(self):
        content = self.a["content"]
        self.assertTrue(self.runtime()["playlist"])
        for foreign, field in ((self.b["audio"], "file_path"),
                               (self.b["import_record"], "storage_path")):
            with self.subTest(field=field):
                original = getattr(foreign, field)
                setattr(foreign, field, content.file_path)
                self.db.commit()
                with patch.object(media_references, "media_reference_owners",
                                  wraps=media_references.media_reference_owners) as owners:
                    result = self.runtime()
                self.assertEqual(owners.call_count, 1)
                self.assertEqual(result["playlist"], [])
                self.assertEqual(result["campaigns"][0]["playlist_items_count"], 0)
                self.assertFalse(result["media_retention"]["complete"])
                self.assertEqual(content.file_path, getattr(foreign, field))
                self.assertIsNone(runtime_reads(self.db))
                setattr(foreign, field, original)
                self.db.commit()
                self.assertTrue(self.runtime()["playlist"])

    def test_missing_foreign_and_ambiguous_content_cannot_reuse_a_positive_decision(self):
        first, foreign = self.a["content"], self.b["content"]
        missing = str(uuid4())
        first.html_content = '<img src="/m%2565dia/ambiguous.png">'
        self.db.commit()
        with runtime_read_scope(self.db):
            with patch.object(media_references, "_readable_media_row",
                              wraps=media_references._readable_media_row) as validations:
                for _ in range(2):
                    self.assertEqual(player_runtime._runtime_contents(
                        self.db, first.client_id, {first.id, foreign.id, missing}),
                        {first.id: None, foreign.id: None, missing: None})
                self.assertIs(player_runtime._runtime_contents(
                    self.db, foreign.client_id, {foreign.id})[foreign.id], foreign)
                self.assertIsNone(player_runtime._runtime_contents(
                    self.db, first.client_id, {foreign.id})[foreign.id])
            self.assertCountEqual([call.args[2].id for call in validations.call_args_list],
                                  [first.id, foreign.id])
        self.assertIsNone(runtime_reads(self.db))

    def test_runtime_reloads_media_and_actor_on_each_http_request(self):
        a, b = self.a, self.b
        first_url = f"/api/channels/{a['channel'].id}/runtime-config"
        second_url = f"/api/channels/{b['channel'].id}/runtime-config"
        with patch.object(media_references, "media_reference_owners",
                          wraps=media_references.media_reference_owners) as owners:
            self.assertEqual(self.request("GET", first_url).status_code, 200)
            self.assertEqual(self.request("GET", second_url, who="A_client_admin").status_code, 403)
            self.assertEqual(self.request("GET", second_url, who="B_client_admin").status_code, 200)
            self.assertEqual(self.request("GET", first_url).status_code, 200)
        self.assertEqual(owners.call_count, 3)
        self.assertTrue(all(runtime_reads(call.args[0]) is None for call in owners.call_args_list))

    def test_failure_discards_scope_and_next_runtime_builds_a_fresh_map(self):
        with patch.object(media_references, "media_reference_owners", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                self.runtime()
        self.assertIsNone(runtime_reads(self.db))
        with patch.object(media_references, "media_reference_owners",
                          wraps=media_references.media_reference_owners) as owners:
            self.assertTrue(self.runtime()["playlist"])
        self.assertEqual(owners.call_count, 1)

    def test_exact_batches_keep_one_owner_map_and_reuse_media_results(self):
        first, second = self.a["content"], self.a["audio"]
        foreign = self.b["content"]
        ids = {first.id, second.id, foreign.id}
        keys = {(first.client_id, identity) for identity in ids}
        with runtime_read_scope(self.db), patch.object(runtime_read, "_CONTENT_BATCH_SIZE", 1), patch.object(
                media_references, "media_reference_owners", wraps=media_references.media_reference_owners
        ) as owners, patch.object(
                media_references, "_readable_media_row", wraps=media_references._readable_media_row
        ) as validations, patch.object(
                player_runtime, "readable_query", wraps=player_runtime.readable_query
        ) as selections:
            self.assertCountEqual(media_references.readable_media_ids(
                self.db, "ContentItem", content_keys=keys), [first.id, second.id])
            self.assertEqual(player_runtime._runtime_contents(self.db, first.client_id, ids),
                             {first.id: first, second.id: second, foreign.id: None})
            self.assertEqual(owners.call_count, 1)
            self.assertCountEqual([call.args[2].id for call in validations.call_args_list],
                                  [first.id, second.id])
            self.assertCountEqual([call.kwargs["content_keys"] for call in selections.call_args_list],
                                  [{key} for key in keys])
        self.assertIsNone(runtime_reads(self.db))

    def test_structural_predicate_cache_declines_aliases_options_and_correlation(self):
        first, foreign = self.a["branch"], self.b["branch"]
        branch_alias = aliased(Branch, name="runtime_branch_scope")
        statements = (
            select(Branch.id).join(branch_alias, branch_alias.id == Branch.id),
            select(Branch.id).execution_options(stream_results=True),
            select(Branch.id).correlate(None),
        )
        with runtime_read_scope(self.db), patch.object(
                tenant_queries, "read_integrity_predicate",
                wraps=tenant_queries.read_integrity_predicate) as build:
            for _ in range(2):
                for branch in (first, foreign):
                    query = tenant_queries.readable_query(
                        select(Branch.id), Branch, db=self.db, profile="RUNTIME", client_id=branch.client_id)
                    self.assertEqual(list(self.db.scalars(query)), [branch.id])
            self.assertEqual(build.call_count, 2)
            for statement in statements:
                query = tenant_queries.readable_query(
                    statement, Branch, db=self.db, profile="RUNTIME", client_id=first.client_id)
                self.assertEqual(list(self.db.scalars(query)), [first.id])
            self.assertEqual(build.call_count, 5)
            self.assertEqual(len(runtime_reads(self.db).predicates), 2)
        self.assertIsNone(runtime_reads(self.db))
