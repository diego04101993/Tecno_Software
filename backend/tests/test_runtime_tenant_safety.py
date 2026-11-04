"""Corrupt graph and inactive-resource defenses through real authenticated HTTP."""
import json
import uuid
from datetime import datetime, UTC
from sqlalchemy import select, func
from test_tenant_isolation import TenantFixture, HistoricalRelationFixture, LocalPostgresFixture, R4_ROLES
import os
from unittest import skipUnless
from app.models.entities import *


class RuntimeTenantTests(TenantFixture):
    def runtime(self):
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_no_foreign(self, data):
        rendered = json.dumps(data)
        for key in ("layout", "campaign", "content", "audio", "playlist", "experience", "screen"):
            resource = self.res["B"][key]
            self.assertNotIn(resource.id, rendered, key)
            self.assertNotIn(resource.name, rendered, key)

    def test_runtime_corrupt_campaign_layout_content_and_legacy_no_repair(self):
        a, b = self.res["A"], self.res["B"]
        a["campaign"].layout_id = b["layout"].id
        a["sequence"].content_id = b["content"].id
        a["legacy"].content_id = b["content"].id
        self.db.commit()
        self.assert_no_foreign(self.runtime())
        response = self.request("GET", f"/api/campaigns/{a['campaign'].id}/sequence-preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_foreign(response.json())
        self.db.expire_all()
        self.assertEqual(a["campaign"].layout_id, b["layout"].id)
        self.assertEqual(a["sequence"].content_id, b["content"].id)
        self.db.delete(a["sequence"])
        self.db.commit()
        self.assert_no_foreign(self.runtime())
        self.assertEqual(self.db.scalar(select(func.count()).select_from(CampaignSequenceItem).where(
            CampaignSequenceItem.campaign_id == a["campaign"].id)), 0)

    def test_foreign_campaign_assignment_and_schedule_not_selected(self):
        a, b = self.res["A"], self.res["B"]
        self.db.query(CampaignChannelAssignment).filter_by(channel_id=a["channel"].id).update({"campaign_id": b["campaign"].id})
        a["schedule"].campaign_id = b["campaign"].id
        self.db.commit()
        payload = self.runtime()
        self.assertEqual(payload["campaigns"], [])
        self.assertIsNone(payload["schedule"])
        self.assert_no_foreign(payload)
        response = self.request("GET", f"/api/channels/{a['channel'].id}/player-config")
        self.assertEqual(response.status_code, 200)
        self.assert_no_foreign(response.json())

    def test_audio_runtime_and_historical_reports_never_resolve_foreign_names(self):
        a, b = self.res["A"], self.res["B"]
        a["audioitem"].content_id = b["audio"].id
        assignment = self.add(AudioAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                              music_playlist_id=a["playlist"].id)
        event = self.add(AudioPlaybackEvent, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                         playlist_id=b["playlist"].id, content_id=b["audio"].id, entry_kind=AudioPlaybackEntryKind.MUSIC,
                         played_at=datetime.now(UTC))
        self.db.commit()
        self.assert_no_foreign(self.runtime())
        response = self.request("GET", "/api/audio/runtime-config", params={"channel_id": a["channel"].id})
        self.assertEqual(response.status_code, 200)
        self.assert_no_foreign(response.json())
        response = self.request("GET", "/api/audio/reports/summary")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b["audio"].name, response.text)
        self.assertNotIn(b["playlist"].name, response.text)
        assignment.music_playlist_id = b["playlist"].id
        self.db.commit()
        self.assert_no_foreign(self.runtime())

    def test_touch_foreign_children_and_targets_omitted(self):
        a, b = self.res["A"], self.res["B"]
        b["screen"].experience_id = a["experience"].id
        a["experience"].home_screen_id = b["screen"].id
        self.add(KioskButton, screen_id=a["screen"].id, label="bad target", x=0, y=0, width=10, height=10,
                 action_type=KioskActionType.NAVIGATE_MENU, target_screen_id=b["screen"].id)
        self.add(TouchExperienceAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id, experience_id=a["experience"].id)
        self.db.commit()
        payload = self.runtime()
        self.assert_no_foreign(payload)
        self.assertEqual(payload["touch"]["screens"][0]["buttons"], [])
        self.db.expire_all()
        self.assertEqual(b["screen"].experience_id, a["experience"].id)

    def test_foreign_binding_and_import_snapshots_not_serialized(self):
        a, b = self.res["A"], self.res["B"]
        self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1, name="old",
                 is_current_published=True, status="published", editor_state_json={},
                 preview_state_json={"foreign": b["layout"].name})
        self.db.commit()
        for corrupt in ("binding", "import", "source"):
            if corrupt == "binding":
                a["binding"].dataset_id = b["dataset"].id
            elif corrupt == "import":
                a["binding"].dataset_id = a["dataset"].id
                a["dataset"].current_import_id = b["import_record"].id
            else:
                a["dataset"].current_import_id = a["import_record"].id
                a["dataset"].data_source_id = b["source"].id
            self.db.commit()
            with self.subTest(corrupt=corrupt):
                self.assert_no_foreign(self.runtime())
                for suffix in ("preview", "editor-state"):
                    response = self.request("GET", f"/api/layouts/{a['layout'].id}/{suffix}", who="A_operator")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assert_no_foreign(response.json())
                    self.assertNotIn(b["dataset"].id, response.text)
                    self.assertNotIn("DATA_B", response.text)

    def test_player_invalid_telemetry_sanitizes_only_bad_fields(self):
        a, b = self.res["A"], self.res["B"]
        for field, resource in (("content", "content"), ("campaign", "campaign"), ("layout", "layout")):
            for invalid in (b[resource].id, str(uuid.uuid4())):
                hb = dict(status="online", current_content_id=a["content"].id,
                          current_campaign_id=a["campaign"].id, current_layout_id=a["layout"].id)
                hb["current_" + field + "_id"] = invalid
                response = self.request("POST", "/api/player/heartbeat", who="player_A", body=hb)
                self.assertEqual(response.status_code, 200, response.text)
                row = self.db.scalar(select(PlayerHeartbeat).order_by(PlayerHeartbeat.received_at.desc()))
                self.assertIsNone(getattr(row, "current_" + field + "_id"))
                for other in {"content", "campaign", "layout"} - {field}:
                    self.assertEqual(getattr(row, "current_" + other + "_id"), a[other].id)
                body = dict(content_id=a["content"].id, campaign_id=a["campaign"].id,
                            layout_id=a["layout"].id, started_at=datetime.now(UTC).isoformat(), status="completed")
                body[field + "_id"] = invalid
                response = self.request("POST", "/api/player/playback-events", who="player_A", body=body)
                self.assertEqual(response.status_code, 201, response.text)
                row = self.db.get(PlayerPlaybackEvent, response.json()["event_id"])
                self.assertIsNone(getattr(row, field + "_id"))
                self.db.expire_all()
                self.assertNotIn("SECRET_" + resource.upper() + "_B", a["channel"].current_playback or "")

    def test_suspension_and_inactive_branch_block_players_without_token_changes(self):
        a = self.res["A"]
        token_before = a["player"].player_token_hash
        for state in ("suspended", "inactive", "branch"):
            self.tenants["A"].status = state if state != "branch" else "active"
            a["branch"].is_active = state != "branch"
            self.db.commit()
            for method, path, body in (
                ("GET", "/api/player/runtime-config", None),
                ("POST", "/api/player/activate", dict(channel_code=a["channel"].channel_code, hardware_id="synthetic-A", device_name="fixture", app_version="test")),
                ("POST", "/api/player/heartbeat", dict(status="online")),
                ("POST", "/api/player/playback-events", dict(started_at=datetime.now(UTC).isoformat(), status="completed")),
            ):
                with self.subTest(state=state, path=path):
                    self.assertEqual(self.request(method, path, who="player_A", body=body).status_code, 403)
            self.db.expire_all()
            self.assertEqual(a["player"].player_token_hash, token_before)
            self.assertTrue(a["player"].is_active)
        self.tenants["A"].status = "active"
        a["branch"].is_active = True
        self.db.commit()
        self.assertEqual(self.runtime()["channel"]["id"], a["channel"].id)

    def test_inactive_campaign_touch_audio_location_map_excluded(self):
        a = self.res["A"]
        a["campaign"].is_active = False
        a["playlist"].is_active = False
        a["experience"].is_active = False
        self.add(AudioAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id, music_playlist_id=a["playlist"].id)
        self.add(TouchExperienceAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id, experience_id=a["experience"].id)
        self.db.commit()
        payload = self.runtime()
        self.assertEqual(payload["campaigns"], [])
        self.assertEqual(payload["playlist"], [])
        self.assertIsNone(payload["touch"])
        self.assertIsNone(payload["audio"])  # An invalid assignment no longer competes in selection.
        a["experience"].is_active = True
        a["location"].is_active = False
        self.add(TouchMap, experience_id=a["experience"].id, name="inactive", is_active=False)
        self.db.commit()
        payload = self.runtime()
        self.assertEqual(payload["touch"]["locations"], [])
        self.assertEqual(payload["touch"]["maps"], [])

class PhaseAR1RuntimeTests(TenantFixture):
    def test_foreign_file_omitted_without_repair(self):
        a,b=self.res["A"],self.res["B"]
        foreign="/media/"+b["import_record"].storage_path
        a["content"].file_path=foreign;self.db.commit()
        for url in ("/api/player/runtime-config",f"/api/campaigns/{a['campaign'].id}/sequence-preview"):
            r=self.request("GET",url,who="player_A" if "player/" in url else "A_client_admin")
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn(foreign,r.text)
            self.assertNotIn(a["content"].id,json.dumps(r.json().get("contents")))
        self.db.expire_all();self.assertEqual(a["content"].file_path,foreign)

    def test_all_revision_reads_sanitize_historical_preview(self):
        a,b=self.res["A"],self.res["B"]
        old={"name":b["layout"].name,"url":b["content"].file_path}
        rev=self.add(LayoutRevision,layout_id=a["layout"].id,revision_number=1,name="historical",status="published",
                     is_current_published=True,editor_state_json={},preview_state_json=old);self.db.commit()
        for suffix in ("revisions","revisions/"+rev.id,"preview","editor-state"):
            r=self.request("GET",f"/api/layouts/{a['layout'].id}/"+suffix,who="A_operator")
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn(b["layout"].name,r.text);self.assertNotIn(b["content"].file_path,r.text)
        self.db.expire_all();self.assertEqual(rev.preview_state_json,old)

    def test_inactive_secondary_branch_output_omitted(self):
        a=self.res["A"]
        branch=self.add(Branch,client_id=self.tenants["A"].id,name="secondary",code="secondary",is_active=False)
        ch=self.add(Channel,client_id=self.tenants["A"].id,branch_id=branch.id,name="secondary")
        out=self.add(DeviceOutput,name="secondary",root_channel_id=a["channel"].id,channel_id=ch.id,display_order=1,enabled=True)
        self.db.commit()
        r=self.request("GET","/api/player/runtime-config",who="player_A")
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn(out.id,json.dumps(r.json()["outputs"]))
        self.db.expire_all();self.assertTrue(out.enabled)

    def test_all_gets_are_read_only_for_all_roles(self):
        from sqlalchemy import event
        a=self.res["A"]
        self.db.delete(a["sequence"]);self.db.commit()
        writes=[]
        def count(conn,cursor,statement,parameters,context,many):
            if statement.lstrip().upper().startswith(("INSERT ","UPDATE ","DELETE ")):writes.append(statement)
        event.listen(self.engine,"before_cursor_execute",count)
        try:
            for role in UserRole:
                for url in (f"/api/campaigns/{a['campaign'].id}/sequence",f"/api/campaigns/{a['campaign'].id}/sequence-preview",
                            f"/api/layouts/{a['layout'].id}/editor-state",f"/api/layouts/{a['layout'].id}/preview"):
                    r=self.request("GET",url,who="A_"+role.value)
                    self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(writes,[])
        finally:event.remove(self.engine,"before_cursor_execute",count)

    def test_revision_embedded_media_and_binding_ids_are_filtered(self):
        a,b=self.res["A"],self.res["B"]
        state={"widgets":[{"id":"foreign-media","widget_type":"image","props_json":{"src":b["content"].file_path}},
                          {"id":"foreign-dataset","widget_type":"dataset_table","binding_ref":{"dataset_id":b["dataset"].id}},
                          {"id":"malformed-id","widget_type":"image","props_json":{"content_id":{"invalid":"type"}}},
                          {"id":"own-media","widget_type":"image","props_json":{"src":a["content"].file_path}}]}
        rev=self.add(LayoutRevision,layout_id=a["layout"].id,revision_number=1,name="stored",status="draft",is_current_draft=True,editor_state_json=state)
        self.db.commit()
        for suffix in ("revisions","revisions/"+rev.id,"preview","editor-state"):
            r=self.request("GET",f"/api/layouts/{a['layout'].id}/"+suffix,who="A_operator")
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn(b["content"].file_path,r.text);self.assertNotIn(b["dataset"].id,r.text)
            self.assertIn("own-media",r.text)
        self.db.expire_all();self.assertEqual(rev.editor_state_json,state)

    def test_foreign_file_retention_and_audio_are_fail_safe(self):
        from app.services.player_runtime import build_player_runtime_config
        a,b=self.res["A"],self.res["B"]
        a["content"].file_path=b["content"].file_path
        a["audio"].file_path=b["content"].file_path
        self.add(AudioAssignment,client_id=self.tenants["A"].id,channel_id=a["channel"].id,music_playlist_id=a["playlist"].id)
        self.db.commit()
        data=build_player_runtime_config(a["channel"],self.db,"http://testserver",snapshot_coherent=True)
        self.assertNotIn(b["content"].file_path,json.dumps(data))
        self.assertFalse(data["media_retention"]["complete"])
        self.assertEqual(data["media_retention"]["assets"],[])
        r=self.request("GET","/api/audio/runtime-config",params={"channel_id":a["channel"].id})
        self.assertEqual(r.status_code,200)
        self.assertNotIn(b["content"].file_path,r.text)

    def test_legacy_sequence_ids_remain_editable_on_explicit_writes(self):
        for action in ("patch","duplicate","reorder","delete","add"):
            f=TenantFixture();f.setUp()
            try:
                a=f.res["A"]
                f.db.delete(a["sequence"]);f.db.commit()
                base=f"/api/campaigns/{a['campaign'].id}/sequence"
                r=f.request("GET",base);self.assertEqual(r.status_code,200)
                item_id=r.json()[0]["id"]
                self.assertEqual(f.db.scalar(select(func.count()).select_from(CampaignSequenceItem).where(CampaignSequenceItem.campaign_id==a["campaign"].id)),0)
                if action=="patch":r=f.request("PATCH",base+"/"+item_id,body={"options_json":{"zone_key":"main"}})
                elif action=="duplicate":r=f.request("POST",base+"/"+item_id+"/duplicate")
                elif action=="reorder":r=f.request("POST",base+"/reorder",body={"ordered_item_ids":[item_id]})
                elif action=="delete":r=f.request("DELETE",base+"/"+item_id)
                else:r=f.request("POST",base,body={"item_type":"content","content_id":a["content"].id,"sort_order":2})
                self.assertIn(r.status_code,(200,201,204),(action,r.text))
                if action in ("duplicate","add"):self.assertEqual(len(f.request("GET",base).json()),2)
            finally:f.doCleanups()


class PhaseAR2WidgetTests(TenantFixture):
    """Each widget instance must earn trust, even when identifiers collide."""

    def _widget(self, marker, *, widget_id="shared", references=None):
        widget = {
            "id": widget_id, "name": "Shared display name", "widget_type": "image",
            "region_id": "main", "layer_role": "content", "z_index": 7,
            "x": 25, "y": 30, "width": 516, "height": 860,
            "opacity": 0.75, "rotation": 90, "visible": True, "locked": False,
            "props_json": {"audit_marker": marker, "fit": "contain", "padding": 3},
        }
        widget["props_json"].update(references or {})
        return widget

    def _safe_widget(self, marker="safe"):
        content = self.res["A"]["content"]
        return self._widget(marker, references={"content_id": content.id, "src": content.file_path})

    def _foreign_widget(self, kind="path"):
        b = self.res["B"]
        refs = {
            "url": {"src": "http://testserver" + b["content"].file_path},
            "path": {"file_path": b["content"].file_path},
            "content": {"content_id": b["content"].id},
            "layout": {"layout_id": b["layout"].id},
            "dataset": {"dataset_id": b["dataset"].id},
            "binding": {},
            "metadata": {"metadata": {"content_id": b["content"].id, "name": b["content"].name}},
        }[kind]
        refs["audit_foreign_metadata"] = "FOREIGN_WIDGET_METADATA"
        widget = self._widget("foreign-" + kind, references=refs)
        if kind == "binding":
            widget["binding_ref"] = {"binding_id": b["binding"].id}
        return widget

    def _stored_state(self):
        models = (Layout, LayoutRevision, LayoutDataBinding, LayoutBindingField,
                  Dataset, DatasetImport, DatasetRow, ContentItem)
        return {
            model.__tablename__: sorted(
                json.dumps(dict(row._mapping), sort_keys=True, default=str)
                for row in self.db.execute(select(*model.__table__.columns))
            )
            for model in models
        }

    def _assert_safe_reads(self, widgets, expected):
        from copy import deepcopy
        from sqlalchemy import event

        a, b = self.res["A"], self.res["B"]
        number = self.db.scalar(select(func.count()).select_from(LayoutRevision)) + 1
        stored_editor = {"widgets": deepcopy(widgets)}
        stored_preview = {"historical": {"url": b["content"].file_path, "id": b["content"].id,
                                         "metadata": "FOREIGN_WIDGET_METADATA"}}
        revision = self.add(
            LayoutRevision, layout_id=a["layout"].id, revision_number=number,
            name="Duplicate ID audit", status="draft", is_current_draft=True,
            editor_state_json=stored_editor, preview_state_json=stored_preview,
        )
        self.db.commit()
        revision_id = revision.id
        before = self._stored_state()
        writes, commits = [], []

        def record_sql(conn, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
                writes.append(statement)

        def record_commit(conn):
            commits.append(True)

        event.listen(self.engine, "before_cursor_execute", record_sql)
        event.listen(self.engine, "commit", record_commit)
        try:
            for suffix in ("revisions", "revisions/" + revision_id, "preview", "editor-state"):
                with self.subTest(endpoint=suffix, input_markers=[w["props_json"]["audit_marker"] for w in widgets]):
                    response = self.request("GET", f"/api/layouts/{a['layout'].id}/" + suffix, who="A_operator")
                    self.assertEqual(response.status_code, 200, response.text)
                    forbidden = [b["content"].file_path, "http://testserver" + b["content"].file_path,
                                 b["content"].id, b["layout"].id, b["dataset"].id, b["binding"].id,
                                 b["content"].name, "FOREIGN_WIDGET_METADATA"]
                    for value in forbidden:
                        self.assertNotIn(value, response.text)

                    payload = response.json()
                    if suffix == "revisions":
                        serialized = next(item for item in payload if item["id"] == revision_id)
                    elif suffix.startswith("revisions/"):
                        serialized = payload
                    elif suffix == "preview":
                        serialized = payload["revision"]
                    else:
                        serialized = payload["active_revision"]
                    states = [serialized["editor_state_json"], serialized["preview_state_json"]]
                    if suffix == "preview":
                        states.append(payload["preview"])
                    for state in states:
                        actual = state["widgets"]
                        self.assertEqual(
                            [widget["props_json"]["audit_marker"] for widget in actual],
                            [widget["props_json"]["audit_marker"] for widget in expected],
                        )
                        for source, result in zip(expected, actual):
                            for key in ("id", "name", "widget_type", "layer_role", "z_index", "x", "y",
                                        "width", "height", "opacity", "rotation", "visible", "locked"):
                                self.assertEqual(result[key], source[key], key)
                            for key, value in source["props_json"].items():
                                self.assertEqual(result["props_json"][key], value)
                            if source.get("binding_ref"):
                                self.assertEqual(result["binding_ref"], source["binding_ref"])
                    self.assertNotIn("binding_preview", json.dumps(serialized["editor_state_json"]))
                    for source in expected:
                        if source["widget_type"] == "dataset_table":
                            preview_widget = next(
                                widget for widget in serialized["preview_state_json"]["widgets"]
                                if widget["props_json"]["audit_marker"] == source["props_json"]["audit_marker"]
                            )
                            self.assertEqual(preview_widget["binding_preview"]["dataset"]["id"], a["dataset"].id)
            self.assertEqual(writes, [])
            self.assertEqual(commits, [])
        finally:
            event.remove(self.engine, "before_cursor_execute", record_sql)
            event.remove(self.engine, "commit", record_commit)
        self.db.expire_all()
        self.assertEqual(revision.editor_state_json, stored_editor)
        self.assertEqual(revision.preview_state_json, stored_preview)
        self.assertEqual(self._stored_state(), before)

    def test_mixed_duplicate_ids_in_both_orders(self):
        own, foreign = self._safe_widget(), self._foreign_widget()
        self._assert_safe_reads([own, foreign], [own])
        self._assert_safe_reads([foreign, own], [own])

    def test_safe_duplicates_preserve_order_visuals_and_bindings(self):
        own = self._safe_widget("media")
        text = self._widget("plain", references={"text": "Own text without media references"})
        text["widget_type"] = "text"
        bound = self._widget("bound")
        bound["widget_type"] = "dataset_table"
        bound["binding_ref"] = {"binding_id": self.res["A"]["binding"].id,
                                "dataset_id": self.res["A"]["dataset"].id}
        widgets = [own, text, bound]
        self._assert_safe_reads(widgets, widgets)

    def test_foreign_only_and_three_shared_ids(self):
        first, second, own = self._foreign_widget("content"), self._foreign_widget("dataset"), self._safe_widget()
        self._assert_safe_reads([first, second], [])
        self._assert_safe_reads([first, own, second], [own])

    def test_each_foreign_reference_kind_is_checked_independently(self):
        for kind in ("url", "path", "content", "layout", "dataset", "binding", "metadata"):
            with self.subTest(kind=kind):
                own = self._safe_widget()
                self._assert_safe_reads([own, self._foreign_widget(kind)], [own])

    def test_distinct_ids_and_foreign_widget_without_id(self):
        own, foreign = self._safe_widget(), self._foreign_widget()
        own["id"], foreign["id"] = "own", "foreign"
        self._assert_safe_reads([own, foreign], [own])
        del foreign["id"]
        self._assert_safe_reads([foreign, own], [own])

    def test_duplicate_ids_in_nested_props_cannot_transfer_trust(self):
        # The editor has a flat widget list; arbitrary nested props are supported.
        # Validate their references recursively without inventing a child-widget API.
        own = self._safe_widget()
        safe_nested = self._widget("safe-nested", references={"children": [
            {"id": "shared", "content_id": self.res["A"]["content"].id},
            {"id": "shared", "dataset_id": self.res["A"]["dataset"].id},
        ]})
        unsafe_nested = self._widget("unsafe-nested", references={"children": [
            {"id": "shared", "content_id": self.res["A"]["content"].id},
            {"id": "shared", "content_id": self.res["B"]["content"].id,
             "metadata": "FOREIGN_WIDGET_METADATA"},
        ]})
        self._assert_safe_reads([own, safe_nested], [own, safe_nested])
        self._assert_safe_reads([unsafe_nested, own], [own])


class PhaseAR5AudioTests(HistoricalRelationFixture):
    def test_corrupt_channel_assignment_falls_back_on_actual_player_endpoint(self):
        a, b = self.res["A"], self.res["B"]
        valid = self.add(AudioAssignment, client_id=self.tenants["A"].id, branch_id=a["branch"].id,
                         music_playlist_id=a["playlist"].id, songs_between_spots=3)
        bad = self.add(AudioAssignment, client_id=self.tenants["A"].id, branch_id=a["branch"].id,
                       channel_id=a["channel"].id, music_playlist_id=b["playlist"].id, songs_between_spots=77)
        self.db.commit()
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET",
                "/api/channels/" + a["channel"].id + "/runtime-config", who=who))
            self.assert_audio(response, a["playlist"].id, "branch", 3)
        # Presence bookkeeping is part of the existing Player route. Verify the
        # audio business graph stays byte-identical while exercising the real token.
        before = self.audio_snapshot()
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assert_audio(response, a["playlist"].id, "branch", 3)
        self.assertEqual(self.audio_snapshot(), before)
        bad.music_playlist_id = a["playlist"].id
        self.db.commit()
        self.assert_audio(self.request("GET", "/api/player/runtime-config", who="player_A"),
                          a["playlist"].id, "channel", 77)
        bad.music_playlist_id = b["playlist"].id
        self.db.delete(valid)
        self.db.commit()
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["audio"])

    def audio_snapshot(self):
        return [sorted(json.dumps(dict(row._mapping), sort_keys=True, default=str)
                       for row in self.db.execute(select(model.__table__)))
                for model in (AudioAssignment, AudioPlaylist, AudioPlaylistItem, ContentItem)]

    def assert_audio(self, response, playlist_id, scope, songs):
        self.assertEqual(response.status_code, 200, response.text)
        audio = response.json()["audio"]
        self.assertIsNotNone(audio)
        self.assertEqual(audio["assignment_scope"], scope)
        self.assertEqual(audio["music_playlist"]["id"], playlist_id)
        self.assertEqual(audio["rules"]["songs_between_spots"], songs)
        self.assertNotIn(self.res["B"]["playlist"].id, response.text)
        self.assertNotIn(self.res["B"]["playlist"].name, response.text)

    def test_corrupt_branch_scope_and_spot_playlist_cannot_compete(self):
        a, b = self.res["A"], self.res["B"]
        other = self.add(Branch, client_id=self.tenants["A"].id, name="other", code="r5-other")
        self.add(AudioAssignment, client_id=self.tenants["A"].id, branch_id=a["branch"].id,
                 music_playlist_id=a["playlist"].id, songs_between_spots=3)
        bad = self.add(AudioAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                       branch_id=a["branch"].id, music_playlist_id=a["playlist"].id, songs_between_spots=77)
        for values in ({"spot_playlist_id": b["playlist"].id}, {"spot_playlist_id": None, "branch_id": b["branch"].id},
                       {"branch_id": other.id}, {"branch_id": a["branch"].id, "client_id": self.tenants["B"].id}):
            for field, value in values.items():
                setattr(bad, field, value)
            self.db.commit()
            response = self.observe_unchanged(lambda: self.request("GET",
                "/api/channels/" + a["channel"].id + "/runtime-config"))
            self.assert_audio(response, a["playlist"].id, "branch", 3)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLPhaseAR5AudioTests(LocalPostgresFixture, PhaseAR5AudioTests):
    pass



class A9ReadSurfaceTests(TenantFixture):
    def test_read_media_adapter_preserves_own_spaces_external_urls_and_text(self):
        from app.services.media_references import read_payload_media_owned, MediaReadContext as C
        owner = self.tenants["A"].id
        path = f"/media/{owner}/synthetic with spaces.png"
        self.add(ContentItem, client_id=owner, name="own spaced file", type=ContentType.IMAGE, file_path=path)
        self.db.commit()
        for value, context in ((path, C.TYPED_URL), (f'<img src="{path}">', C.HTML),
                      (f"url('{path}')", C.CSS),
                      ("http://testserver" + path, C.TYPED_URL),
                      ("https://external.example/media/not-our-storage.png", C.TYPED_URL),
                      ("Texto Unicode válido sin referencia: pantalla ✓", C.OPAQUE)):
            self.assertTrue(read_payload_media_owned(self.db, owner, value, context=context), value)
        self.assertFalse(read_payload_media_owned(self.db, owner, f'<img src=&quot;{path}&quot;>', context=C.HTML),
                         "Encoded quotes are URL characters, not attribute delimiters")
        foreign = self.res["B"]["content"].file_path
        for value, context in ((f'<img src="http://testserver{foreign}">', C.HTML),
                      (f'url("{foreign}")', C.CSS), (f'<img src=&quot;{foreign}&quot;>', C.HTML)):
            self.assertFalse(read_payload_media_owned(self.db, owner, value, context=context), value)

    def test_player_root_channel_opaque_media_is_not_trusted(self):
        a, b = self.res["A"], self.res["B"]
        a["channel"].expanded_outputs = [{"poster": b["content"].file_path}]
        self.db.commit()
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 403, "A9 root channel bypassed media ownership")
        self.assertNotIn(b["content"].file_path, response.text)
        self.db.expire_all()
        self.assertEqual(a["channel"].expanded_outputs, [{"poster": b["content"].file_path}])

    def test_import_summary_cannot_reveal_foreign_media(self):
        a, b = self.res["A"], self.res["B"]
        record = a["import_record"]
        record.summary_json = {"poster": a["content"].file_path}
        self.db.commit()
        path = f"/api/datasets/{a['dataset'].id}/imports"
        response = self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(record.id, response.text)
        record.summary_json = {"poster": b["content"].file_path}
        self.db.commit()
        response = self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(b["content"].file_path, response.text, "A9 import summary leaked foreign media")
        self.assertNotIn(record.id, response.text)
        self.db.expire_all()
        self.assertEqual(record.summary_json, {"poster": b["content"].file_path})

    def test_content_text_and_html_media_references_never_grant_ownership(self):
        a, b = self.res["A"], self.res["B"]
        content = a["content"]
        for field in ("text_content", "html_content"):
            for template in ('{}', '<img src="{}">', 'background-image: url("{}")'):
                setattr(content, field, template.format(a["content"].file_path))
                self.db.commit()
                response = self.request("GET", "/api/contents/")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(content.id, response.text, "A9 own embedded media must remain readable")
                setattr(content, field, template.format(b["content"].file_path))
                self.db.commit()
                response = self.request("GET", "/api/contents/")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(b["content"].file_path, response.text, "A9 embedded content leaked foreign media")
                self.assertNotIn(content.id, response.text)
                response = self.request("GET", "/api/player/runtime-config", who="player_A")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(b["content"].file_path, response.text)
                setattr(content, field, None)
                self.db.commit()

    def test_editor_binding_options_use_same_media_guard_as_preview(self):
        a, b = self.res["A"], self.res["B"]
        self.db.commit()
        paths = [f"/api/layouts/{a['layout'].id}/{suffix}"
                 for suffix in ("editor-state", "data-preview", "runtime-data")]
        for path in paths:
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(a["binding"].id, response.text, "A9 intact binding must remain visible")
        a["binding"].options_json = {"poster": b["content"].file_path}
        self.db.commit()
        from app.services.layout_bindings import build_layout_data_preview
        direct = build_layout_data_preview(a["layout"], [a["binding"]], self.db)
        self.assertEqual(direct["bindings"], [], "A9 direct preview accepted foreign binding options")
        for path in paths:
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(b["content"].file_path, response.text, "A9 alternate editor binding leaked foreign media")
            self.assertNotIn(a["binding"].id, response.text)
        self.db.expire_all()
        self.assertEqual(a["binding"].options_json, {"poster": b["content"].file_path})

    def test_videowall_preview_neutralizes_foreign_primary_for_global_actor(self):
        a, b = self.res["A"], self.res["B"]
        a["wall"].primary_channel_id = b["channel"].id
        self.db.commit()
        for role in R4_ROLES:
            response = self.request("GET", f"/api/videowalls/{a['wall'].id}/preview", who=role)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(response.json()["videowall"]["primary_channel_id"], "A9 foreign wall primary")
            self.assertNotIn(b["channel"].id, response.text)
        self.db.expire_all()
        self.assertEqual(a["wall"].primary_channel_id, b["channel"].id)

    def test_revision_creator_foreign_neutralized_and_global_preserved(self):
        a = self.res["A"]
        foreign = self.users["B_client_admin"]
        global_user = self.users["A_super_admin"]
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, name="qa", revision_number=1,
                            created_by_user_id=foreign.id, editor_state_json={})
        self.db.commit()
        path = f"/api/layouts/{a['layout'].id}/revisions/{revision.id}"
        response = self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["created_by_user_id"], "A9 foreign revision creator")
        revision.created_by_user_id = global_user.id
        self.db.commit()
        response = self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created_by_user_id"], global_user.id)

    def test_audio_foreign_media_event_does_not_increase_report_count(self):
        a, b = self.res["A"], self.res["B"]
        a["audio"].file_path = b["content"].file_path
        self.add(AudioPlaybackEvent, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                 content_id=a["audio"].id, entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
        self.db.commit()
        response = self.request("GET", "/api/audio/reports/summary")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["music"], [], "A9 unsafe event contaminated count")
        self.assertEqual(response.json()["recent_events"], [])

    def test_admin_and_player_audio_skip_inactive_channel_priority(self):
        a = self.res["A"]
        inactive = self.add(AudioPlaylist, client_id=self.tenants["A"].id, name="inactive", kind="music", is_active=False)
        self.add(AudioAssignment, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                 music_playlist_id=inactive.id)
        self.add(AudioAssignment, client_id=self.tenants["A"].id, branch_id=a["branch"].id,
                 music_playlist_id=a["playlist"].id)
        self.db.commit()
        response = self.request("GET", "/api/audio/runtime-config", params={"channel_id": a["channel"].id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["assignment_scope"], "branch", "A9 corrupt priority won admin runtime")
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["audio"]["assignment_scope"], "branch", "A9 corrupt priority won Player runtime")

    def test_device_output_count_uses_eligible_output_set(self):
        a, b = self.res["A"], self.res["B"]
        self.add(DeviceOutput, root_channel_id=a["channel"].id, channel_id=a["channel"].id, name="safe", display_order=0)
        self.add(DeviceOutput, root_channel_id=a["channel"].id, channel_id=b["channel"].id, name="foreign", display_order=1)
        self.db.commit()
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["outputs"]), 1)
        self.assertEqual(response.json()["device"]["expected_outputs"], 1, "A9 count includes foreign output")

    def test_unverified_historical_playback_text_not_returned(self):
        a, b = self.res["A"], self.res["B"]
        a["channel"].current_playback = b["content"].name
        self.db.commit()
        for path, who in (("/api/channels/", "A_client_admin"), ("/api/player/runtime-config", "player_A")):
            response = self.request("GET", path, who=who)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(b["content"].name, response.text, "A9 historical foreign label exposed")

    def test_websocket_rebuilds_room_payload_instead_of_forwarding_foreign_fields(self):
        a, b = self.res["A"], self.res["B"]
        path = f"/api/realtime/ws/channels/{a['channel'].id}?token={self.tokens['A_client_admin']}"
        with self.http.websocket_connect(path) as socket:
            socket.send_json({"type": "heartbeat", "channel_id": b["channel"].id,
                              "content_id": b["content"].id, "current_playback": b["content"].name,
                              "metadata": {"url": b["content"].file_path}})
            payload = socket.receive_json()
            self.assertEqual(payload["channel_id"], a["channel"].id, "A9 foreign websocket room payload")
            self.assertNotIn(b["content"].id, json.dumps(payload))
            self.assertNotIn(b["content"].name, json.dumps(payload))
            self.assertNotIn(b["content"].file_path, json.dumps(payload))

    def test_touch_runtime_omits_foreign_media_screen(self):
        a, b = self.res["A"], self.res["B"]
        a["screen"].background_url = b["content"].file_path
        self.db.commit()
        response = self.request("GET", f"/api/touch/experiences/{a['experience'].id}/runtime-config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["screens"], [], "A9 foreign background URL returned")

    def test_read_snapshot_blocks_flush_and_commit(self):
        from fastapi import HTTPException
        from app.services.tenant_responses import read_snapshot
        @read_snapshot()
        def invalid(db):
            channel = db.get(Channel, self.res["A"]["channel"].id)
            channel.name = "forbidden read repair"
            db.flush()
        with self.assertRaises(HTTPException):
            invalid(db=self.db)
        self.db.expire_all()
        self.assertEqual(self.res["A"]["channel"].name, "Channel A", "A9 read changed persistent ORM")

    def test_dto_copy_does_not_mutate_nested_orm_json(self):
        from app.schemas.domain import ContentRead
        from app.services.tenant_responses import safe_model_read
        content = self.res["A"]["content"]
        content.metadata_json = {"nested": {"safe": "original"}}
        self.db.commit()
        result = safe_model_read(content, ContentRead, self.db)
        result.metadata_json["nested"]["safe"] = "copy edit"
        self.assertEqual(content.metadata_json["nested"]["safe"], "original", "A9 response aliases nested ORM")
        self.assertEqual(list(self.db.dirty), [])

    def test_dataset_binding_lazy_fields_with_foreign_media_are_omitted(self):
        a, b = self.res["A"], self.res["B"]
        field = self.add(LayoutBindingField, binding_id=a["binding"].id, target_field="title", column_key="secret",
                         fallback_value=b["content"].file_path)
        self.db.commit()
        response = self.request("GET", f"/api/layouts/{a['layout'].id}/data-preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(b["content"].file_path, response.text, "A9 lazy field leaked foreign media")
        self.assertNotIn(field.id, response.text)

    def test_postgres_snapshot_failure_never_falls_back_to_incoherent_read(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import HTTPException
        from app.services.player_runtime import build_player_runtime_from_snapshot
        broken = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        broken.engine = broken
        def connect():
            raise RuntimeError("synthetic unavailable snapshot")
        broken.connect = connect
        with patch.object(self.db, "get_bind", return_value=broken):
            with self.assertRaises(HTTPException) as caught:
                build_player_runtime_from_snapshot(self.res["A"]["channel"], self.db, "http://testserver")
        self.assertEqual(caught.exception.status_code, 503, "A9 snapshot failure must fail closed")



    def test_optional_user_projection_never_repairs_persistent_row(self):
        from app.schemas.domain import UserRead
        from app.services.tenant_responses import safe_model_read
        user = self.users["A_branch_manager"]
        user.branch_id = self.res["B"]["branch"].id
        self.db.commit()
        result = safe_model_read(user, UserRead, self.db)
        self.assertIsNone(result.branch_id)
        self.assertEqual(user.branch_id, self.res["B"]["branch"].id, "A9 serializer repaired ORM")
        self.assertFalse(self.db.dirty)

    def test_runtime_foreign_sequence_content_is_not_serialized(self):
        a, b = self.res["A"], self.res["B"]
        a["sequence"].content_id = b["content"].id
        self.db.commit()
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(b["content"].id, response.text, "A9 runtime accepted foreign item")
        self.assertNotIn(b["content"].file_path, response.text)

    def test_sql_filters_corrupt_rows_before_small_page(self):
        from app.services.tenant_queries import readable_query
        from sqlalchemy import func
        a, b = self.res["A"], self.res["B"]
        self.add(Channel, name="a9-page-0-corrupt", client_id=self.tenants["A"].id, branch_id=b["branch"].id)
        good = [self.add(Channel, name="a9-page-" + str(i), client_id=self.tenants["A"].id,
                         branch_id=a["branch"].id) for i in (1, 2)]
        self.db.commit()
        query = readable_query(select(Channel), Channel, db=self.db,
                               client_id=self.tenants["A"].id).where(Channel.name.like("a9-page-%"))
        page = list(self.db.scalars(query.order_by(Channel.name).limit(1)))
        self.assertEqual([r.id for r in page], [good[0].id], "A9 pagination included corrupt row")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(query.subquery())), 2)

    def test_global_parent_context_still_filters_foreign_owner(self):
        from app.services.tenant_ownership import context_for_actor
        from app.services.tenant_queries import readable_query
        actor = self.users["A_super_admin"]
        context = context_for_actor(self.db, actor, client_id=self.tenants["A"].id)
        query = readable_query(select(Channel), Channel, db=self.db, client_id=context.client_id)
        self.assertEqual([row.id for row in self.db.scalars(query)], [self.res["A"]["channel"].id],
                         "A9 global actor bypassed parent context")


    def test_validate_foreign_and_unknown_dataset_are_indistinguishable(self):
        a, b = self.res["A"], self.res["B"]
        results = []
        for identity in (b["dataset"].id, str(uuid.uuid4())):
            response = self.request("POST", f"/api/layouts/{a['layout'].id}/bindings/validate",
                                    body={"dataset_id": identity, "preset_key": a["binding"].preset_key.value})
            results.append((response.status_code, response.json()))
        self.assertEqual(results, [(404, {"detail": "Resource not found"})] * 2,
                         "A9 validate reveals foreign existence or metadata")

    def test_folder_impact_counts_match_same_tenant_names(self):
        a, b = self.res["A"], self.res["B"]
        b["sequence"].content_id = a["content"].id
        b["legacy"].content_id = a["content"].id
        self.db.commit()
        response = self.request("GET", f"/api/content-folders/{a['folder'].id}/delete-impact")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["referenced_campaign_count"], 1, "A9 foreign reference contaminated impact")
        self.assertEqual(response.json()["published_campaign_count"], 1)
        self.assertEqual(response.json()["published_campaign_names"], [a["campaign"].name])
        self.assertNotIn(b["campaign"].name, response.text)


    def test_channel_read_filters_lazy_device_outputs_before_serialization(self):
        a, b = self.res["A"], self.res["B"]
        self.add(DeviceOutput, root_channel_id=a["channel"].id, channel_id=a["channel"].id, name="safe", display_order=0)
        bad = self.add(DeviceOutput, root_channel_id=a["channel"].id, channel_id=b["channel"].id,
                       name="A9_BAD_OUTPUT", display_order=1)
        self.db.commit()
        for who in R4_ROLES:
            response = self.request("GET", "/api/channels/", who=who,
                                    params={"client_id": self.tenants["A"].id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(b["channel"].id, response.text, "A9 lazy output exposed foreign channel")
            self.assertNotIn(bad.id, response.text)
            self.assertEqual(len(response.json()[0]["device_outputs"]), 1)
        self.db.expire_all()
        self.assertEqual(bad.channel_id, b["channel"].id)


    def test_all_disabled_outputs_preserve_declared_legacy_default(self):
        from app.services.device_outputs import serialize_runtime_device
        channel = self.res["A"]["channel"]
        channel.screen_count = 3
        self.add(DeviceOutput, root_channel_id=channel.id, channel_id=channel.id,
                 name="disabled", display_order=0, enabled=False)
        self.db.commit()
        self.db.expire(channel, ["device_outputs"])
        self.assertEqual(serialize_runtime_device(channel, eligible_outputs=[])["expected_outputs"], 3,
                         "A9 changed an intact disabled-output default")

@skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A9")
class A9PostgresReadConcurrencyTests(LocalPostgresFixture, TenantFixture):
    def race_commit(self, model, identity, values):
        from concurrent.futures import ThreadPoolExecutor
        from sqlalchemy import update
        from sqlalchemy.orm import Session
        def change():
            with Session(self.engine) as other:
                other.execute(update(model).where(model.id == identity).values(**values))
                other.commit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(change).result(timeout=15)

    def test_preview_keeps_one_snapshot_when_binding_changes_to_foreign(self):
        from unittest.mock import patch
        from sqlalchemy import text
        from app.api.routes import layouts
        a, b = self.res["A"], self.res["B"]
        original = layouts.build_layout_data_preview
        observations = []
        def interleave(layout, bindings, db, *args, **kwargs):
            observations.append((db.connection().get_isolation_level(), db.connection().scalar(text("SHOW transaction_read_only"))))
            self.race_commit(LayoutDataBinding, a["binding"].id, {"dataset_id": b["dataset"].id})
            return original(layout, bindings, db, *args, **kwargs)
        with patch.object(layouts, "build_layout_data_preview", interleave):
            response = self.request("GET", f"/api/layouts/{a['layout'].id}/data-preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(observations, [("REPEATABLE READ", "on")], "A9 administrative read not one read-only snapshot")
        self.assertIn(a["dataset"].id, response.text)
        self.assertNotIn(b["dataset"].id, response.text, "A9 concurrent preview leaked foreign dataset")
        fresh = self.request("GET", f"/api/layouts/{a['layout'].id}/data-preview")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(fresh.json()["bindings"], [], "A9 snapshot cache persisted across requests")

    def test_player_runtime_keeps_old_sequence_when_other_transaction_changes_fk(self):
        from unittest.mock import patch
        from sqlalchemy import text
        from app.services import player_runtime
        a, b = self.res["A"], self.res["B"]
        original = player_runtime._build_sequence_runtime
        observations = []
        def interleave(campaign, db, base_url):
            observations.append((db.connection().get_isolation_level(), db.connection().scalar(text("SHOW transaction_read_only"))))
            self.race_commit(CampaignSequenceItem, a["sequence"].id, {"content_id": b["content"].id})
            return original(campaign, db, base_url)
        with patch.object(player_runtime, "_build_sequence_runtime", interleave):
            response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(observations, [("REPEATABLE READ", "on")], "A9 Player not one read-only snapshot")
        self.assertIn(a["content"].id, response.text)
        self.assertNotIn(b["content"].id, response.text, "A9 concurrent Player runtime leaked foreign content")
        fresh = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(fresh.json()["sequence_items"], [])


# A9-R1: immutable adversarial fixtures from the independent A9 audit.
A9_R1_MEDIA_OBSERVATIONS = []
A9_R1_READ_EFFECTS = []


def a9_r1_aliases(path):
    return {
        "host_case": "http://TESTSERVER" + path,
        "default_port": "http://testserver:80" + path,
        "protocol_relative": "//testserver" + path,
        "encoded_route": "http://testserver" + path.replace("/media/", "/m%65dia/", 1),
        "encoded_route_first": "http://testserver" + path.replace("/media/", "/%6dedia/", 1),
        "encoded_separator": "http://testserver" + path.replace("/media/", "/media%2f", 1),
        "dot_segment": "http://testserver/unused/.." + path,
        "userinfo": "http://qa@testserver" + path,
    }


def a9_r1_embeddings(url):
    from html import escape
    return {
        "src": '<img src="' + url + '">',
        "href": "<a href='" + url + "'>media</a>",
        "poster": '<video poster="' + url + '"></video>',
        "srcset": '<img srcset="' + url + ' 1x, https://cdn.example/own.png 2x">',
        "unquoted": "<img src=" + url + ">",
        "css": 'background-image: url("' + url + '")',
        "inline_style": '<div style="background-image: url(\'' + url + '\')"></div>',
        "style_block": '<style>.screen { background: url("' + url + '") }</style>',
        "entity": "<img src=&quot;" + url + "&quot;>",
        "nested_entity": escape(escape('<img src="' + url + '">')),
        "entity_path": '<img src="' + url.replace("/", "&#x2f;") + '">',
        "css_escape": 'background-image: url("' + url.replace("/", "\\2f ") + '")',
        "comment": '<!-- media reference "' + url + '" -->',
        "srcdoc": '<iframe srcdoc="' + escape('<img src="' + url + '">', quote=True) + '"></iframe>',
        "meta_refresh": '<meta http-equiv="refresh" content="0; url=' + url + '">',
        "css_import": '<style>@import "' + url + '";</style>',
    }


class A9R1MediaReadTests(TenantFixture):
    def media_read(self, path, who="A_client_admin"):
        from contextlib import nullcontext
        from unittest.mock import patch
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        from app.api.routes import player
        counters = dict(INSERT=0, UPDATE=0, DELETE=0, FLUSH=0, BUSINESS_COMMIT=0)
        is_player = path == "/api/player/runtime-config"
        # The existing Player presence stamp precedes its read-only builder.
        # Count the complete projection, while preserving that pre-existing protocol.
        active = [not is_player]
        def sql(conn, cursor, statement, params, context, many):
            token = statement.lstrip().split(None, 1)[0].upper()
            if active[0] and token in counters:
                counters[token] += 1
        def flush(*_):
            if active[0]:
                counters["FLUSH"] += 1
        def commit(*_):
            if active[0]:
                counters["BUSINESS_COMMIT"] += 1
        original = player.build_player_runtime_from_snapshot
        def snapshot(*args, **kwargs):
            active[0] = True
            try:
                return original(*args, **kwargs)
            finally:
                active[0] = False
        event.listen(self.engine, "before_cursor_execute", sql)
        event.listen(Session, "before_flush", flush)
        event.listen(Session, "before_commit", commit)
        try:
            with (patch.object(player, "build_player_runtime_from_snapshot", snapshot) if is_player else nullcontext()):
                response = self.request("GET", path, who=who)
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
            event.remove(Session, "before_flush", flush)
            event.remove(Session, "before_commit", commit)
        A9_R1_READ_EFFECTS.append(dict(path=path, engine=self.engine.dialect.name, counters=counters,
                                      scope="runtime projection" if is_player else "entire administrative GET"))
        self.assertEqual(sum(counters.values()), 0, "A9-R1 read projection performed a business write")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def no_media_leak(self, response, *, label, url, html_reference=None):
        from html import unescape
        from urllib.parse import unquote
        import re
        b = self.res["B"]["content"]
        # Independent evidence decoder: count B's reference even when HTML
        # entities, CSS hex escapes or URI escapes hide its literal spelling.
        decoded = json.dumps(response.json(), ensure_ascii=False)
        for _ in range(8):
            decoded = unescape(decoded)
        decoded = decoded.replace("\\\\", "\\")
        decoded = re.sub(r"\\([0-9a-fA-F]{1,6})[ \t]?", lambda m: chr(int(m.group(1), 16)), decoded)
        decoded = unquote(decoded)
        counters = dict(ID=response.text.count(self.tenants["B"].id),
                        PATH=response.text.count(b.file_path), URL=response.text.count(url),
                        NAME=response.text.count(b.name), METADATA=response.text.count("QA_FOREIGN_METADATA"),
                        CONTENT=decoded.count(html_reference or b.file_path))
        A9_R1_MEDIA_OBSERVATIONS.append(dict(label=label, engine=self.engine.dialect.name,
                                             counters=counters, response=response.json()))
        self.assertEqual(sum(counters.values()), 0, "A9-R1 foreign media leaked in HTTP payload")

    def test_exact_eight_aliases_on_all_four_content_surfaces(self):
        a, b = self.res["A"], self.res["B"]
        paths = [("/api/contents/", "A_client_admin"),
                 ("/api/campaigns/" + a["campaign"].id + "/sequence-preview", "A_client_admin"),
                 ("/api/channels/" + a["channel"].id + "/runtime-config", "A_client_admin"),
                 ("/api/player/runtime-config", "player_A")]
        for label, url in a9_r1_aliases(b["content"].file_path).items():
            a["content"].source_url = url
            self.db.commit()
            for path, who in paths:
                with self.subTest(alias=label, path=path):
                    response = self.media_read(path, who)
                    self.no_media_leak(response, label=label + ":" + path, url=url)
            self.db.expire_all()
            self.assertEqual(a["content"].source_url, url, "A9-R1 repaired stored source_url")

    def test_owned_queries_fragments_and_external_sources_preserve_original_value(self):
        content = self.res["A"]["content"]
        own = content.file_path
        for url in list(a9_r1_aliases(own).values()) + [own + "?x=1", own + "#part",
                "HTTP://TESTSERVER:80/unused/../m%65dia/" + own.split("/media/", 1)[1],
                "https://external.example/media/remote.png?x=1&y=2"]:
            content.source_url = url
            self.db.commit()
            response = self.media_read("/api/contents/")
            row = next(row for row in response.json() if row["id"] == content.id)
            self.assertEqual(row["source_url"], url, "A9-R1 changed an owned/external original URL")

    def widget_reads(self, revision):
        prefix = "/api/layouts/" + self.res["A"]["layout"].id + "/"
        return [(prefix + suffix, "A_operator") for suffix in (
            "revisions", "revisions/" + revision.id, "preview", "editor-state")] + [
            ("/api/channels/" + self.res["A"]["channel"].id + "/runtime-config", "A_client_admin"),
            ("/api/player/runtime-config", "player_A")]

    def test_html_alias_embedding_surface_matrix_draft_and_published(self):
        a, b = self.res["A"], self.res["B"]
        widgets = []
        # Each valid widget shares its ID with a foreign widget. Neither order
        # nor ID can transfer trust; forbidden HTML must omit the entire widget.
        urls = {"canonical": b["content"].file_path, **a9_r1_aliases(b["content"].file_path)}
        for alias, url in urls.items():
            for form, markup in a9_r1_embeddings(url).items():
                identity = alias + ":" + form
                good = dict(id=identity, widget_type="html",
                            props_json={"htmlContent": a9_r1_embeddings(a["content"].file_path)[form]})
                bad = dict(id=identity, widget_type="html",
                           name="A9R1_FOREIGN_WIDGET_" + identity, props_json={"htmlContent": markup})
                widgets.extend((good, bad) if len(widgets) % 4 == 0 else (bad, good))
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="QA HTML revision", status="draft", is_current_draft=True,
                            editor_state_json={"widgets": widgets})
        for phase in ("draft", "published"):
            revision.status = phase
            revision.is_current_draft = phase == "draft"
            revision.is_current_published = phase == "published"
            self.db.commit()
            for path, who in self.widget_reads(revision):
                with self.subTest(phase=phase, path=path):
                    response = self.media_read(path, who)
                    self.no_media_leak(response, label=phase + ":" + path,
                                       url=b["content"].file_path, html_reference=b["content"].file_path)
                    self.assertNotIn("A9R1_FOREIGN_WIDGET_", response.text, "A9-R1 retained a foreign widget partially")
                    self.assertIn(a["content"].file_path, response.text, "A9-R1 removed the owned HTML control")
            self.db.expire_all()
            self.assertEqual(revision.editor_state_json, {"widgets": widgets}, "A9-R1 repaired stored revision HTML")

    def test_unknown_ambiguous_media_omit_whole_widget(self):
        from app.services.layout_editor import build_layout_preview_state
        a, b = self.res["A"], self.res["B"]
        self.add(ContentItem, client_id=self.tenants["B"].id, name="QA inconsistent evidence",
                 type=ContentType.IMAGE, file_path=a["content"].file_path)
        self.db.commit()
        urls = [a["content"].file_path, "/media/unknown/missing.png",
                "/m%2565dia/" + b["content"].file_path.split("/media/", 1)[1],
                "/m%6Gdia/file.png"]
        for url in urls:
            state = {"widgets": [{"id": "denied", "widget_type": "html",
                       "props_json": {"htmlContent": '<img src="' + url + '">', "text": "must omit entire widget"}}]}
            preview = build_layout_preview_state(a["layout"], state, self.db)
            self.assertEqual(preview["widgets"], [], "A9-R1 unknown/ambiguous media survived")

    def html_defense(self, markup, *, surface="preview", duplicate=False):
        from html import unescape
        a, b = self.res["A"], self.res["B"]
        good = dict(id="same" if duplicate else "good", widget_type="html",
                    props_json={"htmlContent": '<img src="' + a["content"].file_path + '">'})
        bad = dict(id="same" if duplicate else "bad", name="A9R1_FOREIGN_WIDGET_SENTINEL",
                   widget_type="html", props_json={"htmlContent": markup})
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="QA hostile widget", status="draft", is_current_draft=True,
                            editor_state_json={"widgets": [good, bad]})
        self.db.commit()
        if surface == "player":
            response = self.media_read("/api/player/runtime-config", "player_A")
        else:
            response = self.media_read("/api/layouts/" + a["layout"].id + "/" + surface)
        self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")
        self.assertNotIn(b["content"].file_path, unescape(response.text), "A9-R1 encoded HTML defense leaked foreign media")
        self.assertNotIn("A9R1_FOREIGN_WIDGET_SENTINEL", response.text, "A9-R1 partial widget survived")
        self.assertIn(a["content"].file_path, response.text, "A9-R1 positive HTML control missing")

    def test_html_src_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["src"])

    def test_css_url_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["css"])

    def test_html_entities_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["entity_path"])

    def test_editor_html_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["src"], surface="editor-state")

    def test_player_html_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["src"], surface="player")

    def test_duplicate_widget_html_defense(self):
        self.html_defense(a9_r1_embeddings(self.res["B"]["content"].file_path)["src"], duplicate=True)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A9-R1")
class A9R1MediaReadPostgresTests(LocalPostgresFixture, A9R1MediaReadTests):
    pass


class A9R1CanonicalMediaTests(__import__("unittest").TestCase):
    def classify(self, url, base="http://testserver"):
        from unittest.mock import patch
        from types import SimpleNamespace
        from app.services import media_references
        with patch.object(media_references, "get_settings", return_value=SimpleNamespace(PUBLIC_BASE_URL=base)):
            return media_references.canonical_media_reference(url)

    def test_host_case_defense(self):
        self.assertEqual(self.classify("HTTP://TESTSERVER/media/A/MixedCase.PNG"), ("LOCAL", "/media/A/MixedCase.PNG"), "A9-R1 host case bypass")

    def test_default_port_defense(self):
        self.assertEqual(self.classify("http://testserver:80/media/A/a.png"), ("LOCAL", "/media/A/a.png"), "A9-R1 default port bypass")
        self.assertEqual(self.classify("https://testserver:443/media/A/a.png", "https://testserver"), ("LOCAL", "/media/A/a.png"))

    def test_percent_path_defense(self):
        self.assertEqual(self.classify("/m%65dia/A/a.png"), ("LOCAL", "/media/A/a.png"), "A9-R1 encoded route bypass")

    def test_query_fragment_defense(self):
        self.assertEqual(self.classify("/media/A/a.png?x=1#part"), ("LOCAL", "/media/A/a.png"), "A9-R1 query fragment key changed")

    def test_ambiguous_encoding_defense(self):
        self.assertEqual(self.classify("/m%2565dia/A/a.png"), ("AMBIGUOUS", None), "A9-R1 ambiguous encoding accepted")
        for url in ("/m%6Gdia/a", "/media/%", "/media/%FF", "/media/%00", "/media/%5cA/a",
                    "/media/%252fA/a", "///testserver/media/A/a", "http://testserver:bad/media/a",
                    "/media/\ud800", "/media/\x00", "http:/media/A/a"):
            with self.subTest(url=repr(url)):
                self.assertEqual(self.classify(url), ("AMBIGUOUS", None))

    def test_exact_aliases_and_combined_origin(self):
        path = "/media/A/MixedCase.PNG"
        for label, url in a9_r1_aliases(path).items():
            with self.subTest(alias=label):
                self.assertEqual(self.classify(url), ("LOCAL", path))
        for url in ("media/A/MixedCase.PNG", "./media/A/MixedCase.PNG",
                    "/%6d%65%64%69%61/A/MixedCase.PNG",
                    "HTTP://TESTSERVER.:80/unused/../m%65dia/A/MixedCase.PNG?x=1#part"):
            self.assertEqual(self.classify(url), ("LOCAL", path))
        self.assertEqual(self.classify("/MEDIA/A/a"), ("OTHER", None))
        self.assertEqual(self.classify("/media/A/a%3Fb%23c"), ("LOCAL", "/media/A/a?b#c"))

    def test_canonical_hosts_are_syntactic(self):
        from unittest.mock import patch
        import socket
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            self.assertEqual(self.classify("https://xn--bcher-kva.example:443/media/A/a", "https://bücher.example"), ("LOCAL", "/media/A/a"))
            self.assertEqual(self.classify("http://[0:0:0:0:0:0:0:1]:80/media/A/a", "http://[::1]"), ("LOCAL", "/media/A/a"))
        for url in ("http://127.1/media/A/a", "http://2130706433/media/A/a", "http://0x7f000001/media/A/a"):
            self.assertEqual(self.classify(url, "http://127.0.0.1"), ("AMBIGUOUS", None))

    def test_negative_hosts_ports_schemes_and_userinfo(self):
        base = "https://api.tecnocontrolcloud.com"
        for origin in ("https://api.tecnocontrolcloud.com.evil.example",
                       "https://evil-api.tecnocontrolcloud.com", "https://api.tecnocontrolcloud.com@evil.example",
                       "https://api.tecnocontrolcloud.com:444", "http://api.tecnocontrolcloud.com",
                       "https://api.tecnocontrolcloud.com:0"):
            with self.subTest(origin=origin):
                self.assertEqual(self.classify(origin + "/media/A/a", base), ("EXTERNAL", None))
        self.assertEqual(self.classify("https://qa@api.tecnocontrolcloud.com/media/A/a", base), ("LOCAL", "/media/A/a"))

    def test_embedding_extraction_same_key(self):
        from app.services.media_references import _read_local_media_paths, MediaReadContext
        path = "/media/A/MixedCase.PNG"
        for alias, url in {"canonical": path, **a9_r1_aliases(path)}.items():
            for form, html in a9_r1_embeddings(url).items():
                with self.subTest(alias=alias, form=form):
                    if form in {"entity", "nested_entity", "srcdoc"}:
                        # The old parser invented delimiters / reparsed inert
                        # text. Undeclared nested documents now fail closed.
                        from app.services.media_storage import MediaPathError
                        with self.assertRaises(MediaPathError):
                            list(_read_local_media_paths(html, context=MediaReadContext.HTML))
                    else:
                        self.assertEqual(set(_read_local_media_paths(html, context=MediaReadContext.HTML)), {path})
        for html in ('<img data-src="/media/A/MixedCase.PNG">', '<object data="/media/A/MixedCase.PNG"></object>',
                     '<style>.s{background:u\\72l("\\2f media/A/MixedCase.PNG")}</style>',
                     '<img srcset="https://cdn.example/a 1x, /media/A/MixedCase.PNG 2x">'):
            self.assertEqual(set(_read_local_media_paths(html, context=MediaReadContext.HTML)), {path})
        for form in ("entity", "srcdoc"):
            spaced = "/media/A/original with spaces.PNG"
            from app.services.media_storage import MediaPathError
            with self.assertRaises(MediaPathError):
                list(_read_local_media_paths(a9_r1_embeddings(spaced)[form], context=MediaReadContext.HTML))

    def test_nested_malformed_and_limits_fail_closed(self):
        from html import escape
        from app.services.media_references import _read_local_media_paths, MediaReadContext
        from app.services.media_storage import MediaPathError
        nested = '<img src="/media/A/a">'
        for _ in range(12):
            nested = escape(nested)
        for html in (nested, '<img src="&#xD800;media/a">', '<img src="&#wat;/media/a">',
                     "/me\ndia/A/a", '<img src="/me&#10;dia/A/a">',
                     '<img src="/m%2565dia/A/a">', '<style>background:url("/media/A/a"</style>',
                     '<img src="' + "/media/" + "x" * 17_000 + '">', "x" * 1_048_577,
                     '<img src="/media/A/a">' * 2049):
            with self.subTest(prefix=repr(html[:50])):
                with self.assertRaises(MediaPathError):
                    list(_read_local_media_paths(html, context=MediaReadContext.HTML))

    def test_external_html_and_literal_storage_are_unchanged(self):
        from app.services.media_references import _read_local_media_paths, MediaReadContext
        from app.services.media_storage import resolve_stored_media_path
        from pathlib import Path
        import tempfile
        markup = '<img src="https://cdn.example/media/B/file.png?x=1&amp;y=2">'
        self.assertEqual(list(_read_local_media_paths(markup, context=MediaReadContext.HTML)), [])
        with tempfile.TemporaryDirectory(prefix="a9-r1-literal-") as directory:
            root = Path(directory)
            path = resolve_stored_media_path(root, "/media/A/percent%20literal.png", must_exist=False)
            self.assertEqual(path.name, "percent%20literal.png", "A9-R1 decoded authoritative stored path")


A9_R2_P1_OBSERVATIONS = []


class A9R2TypedURLTests(TenantFixture):
    media_read = A9R1MediaReadTests.media_read

    def exact_p1_fixture(self):
        """The unchanged A9-R1 checkpoint P1: B owns the literal ampersand file."""
        from app.core.config import get_settings
        a, b = self.res["A"], self.res["B"]
        b["content"].file_path = "/media/&"
        (get_settings().MEDIA_ROOT / "&").write_bytes(b"QA_FOREIGN_LITERAL_ENTITY_FILE")
        suffix = a["content"].file_path.removeprefix("/media/")
        url = "/media/&#x" + format(ord(suffix[0]), "x") + ";" + suffix[1:]
        a["content"].source_url = url
        self.db.commit()
        delivered = self.http.get(url)
        self.assertEqual(delivered.status_code, 200)
        self.assertEqual(delivered.content, b"QA_FOREIGN_LITERAL_ENTITY_FILE")
        return url

    def typed_url_defense(self, surface):
        from app.services.media_references import canonical_media_reference, _read_local_media_paths, MediaReadContext
        a = self.res["A"]
        url = self.exact_p1_fixture()
        paths = {"contents": ("/api/contents/", "A_client_admin"),
                 "preview": ("/api/campaigns/" + a["campaign"].id + "/sequence-preview", "A_client_admin"),
                 "admin": ("/api/channels/" + a["channel"].id + "/runtime-config", "A_client_admin"),
                 "player": ("/api/player/runtime-config", "player_A")}
        for name in paths if surface == "all" else [surface]:
            path, who = paths[name]
            with self.subTest(surface=name, engine=self.engine.dialect.name):
                response = self.media_read(path, who)
                counters = dict(ID=response.text.count(self.tenants["B"].id),
                                PATH=response.text.count("/media/&"), URL=response.text.count(url),
                                CONTENT=response.text.count("/media/&"))
                A9_R2_P1_OBSERVATIONS.append(dict(surface=name, engine=self.engine.dialect.name,
                    exact_matrix=surface == "all", url=url, effective_key=canonical_media_reference(url)[1],
                    validated_keys=list(_read_local_media_paths(url, context=MediaReadContext.TYPED_URL)),
                    counters=counters, response=response.json()))
                self.assertNotIn(url, response.text, "A9-R2 typed URL exposed foreign effective target")
                self.assertEqual(sum(counters.values()), 0, "A9-R2 foreign reference leaked")
        self.db.expire_all()
        self.assertEqual(a["content"].source_url, url, "A9-R2 repaired stored source_url")
        self.assertEqual(self.res["B"]["content"].file_path, "/media/&")

    def test_exact_p1_all_four_surfaces(self):
        self.typed_url_defense("all")

    def test_contents_typed_context(self):
        self.typed_url_defense("contents")
        from app.api.routes.contents import readable_content_ids
        # The list endpoint has two independent predicates. Assert this caller
        # too, so the other predicate cannot mask an incorrect context mutation.
        self.assertNotIn(self.res["A"]["content"].id, readable_content_ids(self.db),
                         "A9-R2 Content caller accepted an incorrect URL context")

    def test_preview_typed_context(self):
        self.typed_url_defense("preview")

    def test_admin_runtime_typed_context(self):
        self.typed_url_defense("admin")

    def test_player_runtime_typed_context(self):
        self.typed_url_defense("player")

    def test_same_literal_entity_owned_effective_target_is_preserved(self):
        url = self.exact_p1_fixture()
        self.res["B"]["content"].file_path = None
        self.add(ContentItem, client_id=self.tenants["A"].id, name="QA owned ampersand",
                 type=ContentType.VIDEO, file_path="/media/&")
        self.db.commit()
        response = self.media_read("/api/contents/")
        row = next(row for row in response.json() if row["id"] == self.res["A"]["content"].id)
        self.assertEqual(row["source_url"], url, "A9-R2 blanket-blocked an owned typed URL")

    def test_widget_typed_source_and_html_attribute_have_distinct_contexts(self):
        from app.services.layout_editor import build_layout_preview_state
        from copy import deepcopy
        url = self.exact_p1_fixture()
        state = {"widgets": [
            dict(id="typed", widget_type="image", props_json={"sourceUrl": url}),
            dict(id="html", widget_type="html", props_json={"htmlContent": '<img src="' + url + '">'})]}
        original = deepcopy(state)
        preview = build_layout_preview_state(self.res["A"]["layout"], state, self.db)
        self.assertEqual([w["id"] for w in preview["widgets"]], ["html"])
        self.assertEqual(state, original, "A9-R2 changed caller HTML")

    def test_typed_model_url_columns_do_not_decode_html(self):
        from app.services.media_references import readable_payload_ids
        url = self.exact_p1_fixture()
        screen = self.res["A"]["screen"]
        screen.background_url = url
        self.db.commit()
        self.assertNotIn(screen.id, readable_payload_ids(self.db, "KioskScreen", ("background_url",)))
        self.db.expire_all()
        self.assertEqual(screen.background_url, url)

    def test_unknown_json_field_cannot_claim_widget_html_context(self):
        from app.services.media_references import read_payload_media_owned, MediaReadContext
        url = self.exact_p1_fixture()
        for field in ("htmlContent", "sourceUrl", "arbitrary"):
            self.assertFalse(read_payload_media_owned(self.db, self.tenants["A"].id, {field: url},
                             context=MediaReadContext.STRUCTURED), "A9-R2 JSON labels granted context")
        self.assertFalse(read_payload_media_owned(self.db, self.tenants["A"].id, url),
                         "A9-R2 missing context was accepted")


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A9-R2")
class A9R2TypedURLPostgresTests(LocalPostgresFixture, A9R2TypedURLTests):
    pass


class A9R2ContextTests(__import__("unittest").TestCase):
    def keys(self, value, context):
        from app.services.media_references import _read_local_media_paths
        return set(_read_local_media_paths(value, context=context))

    def test_typed_url_never_decodes_html_entities(self):
        from app.services.media_references import MediaReadContext as C
        self.assertEqual(self.keys("/media/&#x41;/file.png", C.TYPED_URL), {"/media/&"},
                         "A9-R2 typed URL was interpreted as HTML")
        from unittest.mock import patch
        with patch("app.services.media_references._html_media_view", side_effect=AssertionError("HTML decode forbidden")):
            self.assertEqual(self.keys("/media/a&amp;b.png", C.TYPED_URL), {"/media/a&amp;b.png"})

    def test_differential_typed_and_html_attribute(self):
        from app.services.media_references import MediaReadContext as C
        for raw, typed, html in (("/media/&#x41;/file.png", "/media/&", "/media/A/file.png"),
                                 ("/media/a&amp;b.png", "/media/a&amp;b.png", "/media/a&b.png"),
                                 ("/media/a&amp;amp;b.png", "/media/a&amp;amp;b.png", "/media/a&amp;b.png")):
            with self.subTest(raw=raw):
                self.assertEqual(self.keys(raw, C.TYPED_URL), {typed})
                self.assertEqual(self.keys('<img src="' + raw + '">', C.HTML), {html},
                                 "A9-R2 HTML attribute context lost entity semantics")

    def test_effective_url_key_matches_uri_path_not_decoded_document(self):
        from app.services.media_references import MediaReadContext as C
        from urllib.parse import urlsplit, unquote
        for url in ("/media/A/a%23b.png?x=1#fragment", "/media/A/a%3Fb.png", "/media/a&amp;b.png", "/media/&#x41;/a"):
            expected = unquote(urlsplit(url).path, encoding="utf-8", errors="strict")
            self.assertEqual(self.keys(url, C.TYPED_URL), {expected}, "A9-R2 validated/effective media key mismatch")

    def test_css_does_not_apply_html_entity_rules(self):
        from app.services.media_references import MediaReadContext as C
        self.assertEqual(self.keys('url("/media/a&amp;b.png")', C.CSS), {"/media/a&amp;b.png"})
        self.assertEqual(self.keys('<div style="background:url(/media/a&amp;b.png)"></div>', C.HTML), {"/media/a&b.png"})
        self.assertEqual(self.keys('<style>.x{background:url("/media/a&amp;b.png")}</style>', C.HTML), {"/media/a&amp;b.png"})
        self.assertEqual(self.keys('@import "/media/a&amp;b.css";', C.CSS), {"/media/a&amp;b.css"})
        self.assertEqual(self.keys('<p>/media/a&amp;amp;b.png</p>', C.HTML), {"/media/a&amp;b.png"})

    def test_unknown_context_fails_closed(self):
        from app.services.media_storage import MediaPathError
        for context in (None, "HTML", "typed_url", object()):
            with self.subTest(context=str(context)):
                with self.assertRaises(MediaPathError, msg="A9-R2 unknown context accepted"):
                    self.keys("/media/A/file.png", context)

    def test_opaque_context_rejects_differing_keys_without_guessing(self):
        from app.services.media_references import MediaReadContext as C
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError):
            self.keys("/media/&#x41;/file.png", C.OPAQUE)
        self.assertEqual(self.keys({"note": "plain text\nsecond line", "value": 12}, C.STRUCTURED), set())
        self.assertEqual(self.keys({"arbitrary": "/media/A/a.png?x=1#part"}, C.STRUCTURED), {"/media/A/a.png"})

    def test_structured_context_preserves_total_reference_budget(self):
        from app.services.media_references import MediaReadContext as C
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError):
            self.keys(["/media/A/" + str(i) + ".png" for i in range(2049)], C.STRUCTURED)


# A9-R3: exact independent-audit fixtures, including ordinary UUID paths.
A9_R3_MEDIA_OBSERVATIONS = []
A9_R3_STORED_INVARIANTS = []
A9_R3_CASES = [{'name': 'srcset_comma', 'owned': '/media/owned.png', 'foreign': '/media/owned.png,foreign.png', 'html': '<img srcset="/media/owned.png,foreign.png 1x">', 'context': 'HTML'}, {'name': 'css_escaped_paren_style', 'owned': '/media/owned.png', 'foreign': '/media/owned.png).png', 'html': '<style>.probe{background:url(/media/owned.png\\).png)}</style><div class="probe" style="width:40px;height:40px"></div>', 'context': 'HTML'}, {'name': 'css_escaped_paren_inline', 'owned': '/media/owned.png', 'foreign': '/media/owned.png).png', 'html': '<div class="probe" style="width:40px;height:40px;background:url(/media/owned.png\\).png)"></div>', 'context': 'HTML'}, {'name': 'css_escaped_quote', 'owned': '/media/owned.png', 'foreign': '/media/foreign".png', 'html': '<style>.probe{background:url("/media/foreign\\22 .png")}</style><div class="probe" style="width:40px;height:40px"></div>', 'context': 'HTML', 'posix_only': True}, {'name': 'css_quoted_nbsp', 'owned': '/media/owned.png', 'foreign': '/media/owned.png\xa0', 'html': '<style>.probe{background:url("/media/owned.png\xa0")}</style><div class="probe" style="width:40px;height:40px"></div>', 'context': 'HTML'}, {'name': 'css_image_set_string', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<style>.probe{background-image:image-set("/media/foreign.png" 1x)}</style><div class="probe" style="width:40px;height:40px"></div>', 'context': 'HTML'}, {'name': 'base_relative_image', 'owned': '/media/owned.html', 'foreign': '/media/foreign.png', 'html': '<base href="/media/owned.html"><img src="foreign.png">', 'context': 'HTML'}, {'name': 'typed_trailing_space', 'owned': '/media/foreign.png ', 'foreign': '/media/foreign.png', 'url': '/media/foreign.png ', 'context': 'TYPED_URL', 'posix_only': True}, {'name': 'image_set_uuid', 'context': 'HTML', 'ordinary_uuid': True}, {'name': 'base_relative_uuid', 'context': 'HTML', 'ordinary_uuid': True}]

class A9R3EffectiveMediaReadTests(TenantFixture):
    media_read = A9R1MediaReadTests.media_read

    def scenario(self, case):
        from app.services.media_references import _read_local_media_paths, MediaReadContext, read_payload_media_owned
        from urllib.parse import quote
        if os.name == 'nt' and case.get('posix_only'):
            self.skipTest('Historical filename requires POSIX; run unchanged on Linux')
        a, b = (self.res['A'], self.res['B'])
        case = dict(case)
        if case.get('ordinary_uuid'):
            case['owned'], case['foreign'] = a['content'].file_path, b['content'].file_path
            if case['name'] == 'image_set_uuid':
                case['html'] = '<style>.probe{background-image:image-set("' + case['foreign'] + '" 1x)}</style><div class="probe"></div>'
            else:
                case['html'] = '<base href="' + case['owned'] + '"><img src="../' + case['foreign'].removeprefix('/media/') + '">'
        a['content'].file_path = case['owned']
        b['content'].file_path = case['foreign']
        for key, value in [(case['owned'], b'QA_OWNED_BYTES'), (case['foreign'], b'QA_FOREIGN_BYTES')]:
            p = self.root / key.removeprefix('/media/')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(value)
        self.add(ContentItem, client_id=self.tenants['A'].id, name='QA positive independent control', type=ContentType.IMAGE, file_path='/media/positive.png')
        (self.root / 'positive.png').write_bytes(b'QA_OWNED_POSITIVE')
        raw = case.get('html', case.get('url', case.get('opaque')))
        ctx = getattr(MediaReadContext, case['context'])
        a['content'].html_content = raw if case['context'] == 'HTML' else None
        a['content'].source_url = raw if case['context'] == 'TYPED_URL' else None
        if case['context'] == 'STRUCTURED':
            a['content'].metadata_json = {'note': raw}
        a['content'].type = ContentType.HTML if case['context'] == 'HTML' else ContentType.IMAGE
        self.db.commit()
        try:
            validated = list(_read_local_media_paths(raw, context=ctx))
            parse_error = None
        except Exception as error:
            validated = []
            parse_error = str(error)
        ownership_pass = read_payload_media_owned(self.db, self.tenants['A'].id, raw, context=ctx)
        delivery = self.http.get(quote(case['foreign'], safe='/'))
        self.assertEqual(delivery.status_code, 200, delivery.text)
        self.assertEqual(delivery.content, b'QA_FOREIGN_BYTES')
        from copy import deepcopy
        source_before = deepcopy((a['content'].source_url, a['content'].html_content, a['content'].metadata_json))

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for x in value.values():
                    yield from strings(x)
            elif isinstance(value, list):
                for x in value:
                    yield from strings(x)

        def observe(surface, path, who):
            response = self.media_read(path, who)
            leaked = raw in list(strings(response.json()))
            A9_R3_MEDIA_OBSERVATIONS.append(dict(case=case['name'], platform=os.name, engine=self.engine.dialect.name, surface=surface, path=path, status=response.status_code, context=case['context'], raw=raw, validated_keys=validated, parse_error=parse_error, ownership_pass=ownership_pass, effective_key=case['foreign'], owned_key=case['owned'], foreign_bytes_delivered=True, reference_exposed=leaked, counters=dict(ID=response.text.count(self.tenants['B'].id), PATH=response.text.count(case['foreign']), URL=int(leaked), NAME=response.text.count(b['content'].name), METADATA=0, CONTENT=int(leaked)), response=response.json()))
            with self.subTest(case=case['name'], surface=surface, engine=self.engine.dialect.name):
                self.assertFalse(leaked, 'Independent effective foreign media URL survived read projection')
                if surface.startswith(('draft:', 'published:')):
                    self.assertIn('/media/positive.png', response.text,
                                  'Owned widget sharing the same ID was lost')
        for name, path, who in [('contents', '/api/contents/', 'A_client_admin'), ('campaign_preview', '/api/campaigns/' + a['campaign'].id + '/sequence-preview', 'A_client_admin'), ('admin_runtime', '/api/channels/' + a['channel'].id + '/runtime-config', 'A_client_admin'), ('player_runtime', '/api/player/runtime-config', 'player_A')]:
            observe(name, path, who)
        self.db.expire_all()
        self.assertEqual((a['content'].source_url, a['content'].html_content, a['content'].metadata_json), source_before)
        good = dict(id='same', widget_type='image', props_json={'sourceUrl': '/media/positive.png'})
        bad = dict(id='same', widget_type='html' if case['context'] == 'HTML' else 'image', name='QA_BAD_CONTEXT_WIDGET', props_json={'htmlContent' if case['context'] == 'HTML' else 'sourceUrl': raw})
        if case['context'] == 'STRUCTURED':
            bad['props_json'] = {'sourceUrl': '/media/positive.png', 'note': raw}
        state = {'widgets': [good, bad]}
        revision = self.add(LayoutRevision, layout_id=a['layout'].id, revision_number=1, name='QA independent context revision', status='draft', is_current_draft=True, editor_state_json=state)
        for phase in ('draft', 'published'):
            revision.status = phase
            revision.is_current_draft = phase == 'draft'
            revision.is_current_published = phase == 'published'
            self.db.commit()
            for suffix in ('revisions', 'revisions/' + revision.id, 'preview', 'editor-state'):
                path = '/api/layouts/' + a['layout'].id + '/' + suffix
                observe(phase + ':' + suffix.split('/')[0], path, 'A_operator')
            observe(phase + ':admin_runtime', '/api/channels/' + a['channel'].id + '/runtime-config', 'A_client_admin')
            observe(phase + ':player_runtime', '/api/player/runtime-config', 'player_A')
            self.db.expire_all()
            self.assertEqual(revision.editor_state_json, state)
        A9_R3_STORED_INVARIANTS.append(dict(case=case['name'], engine=self.engine.dialect.name, platform=os.name, stored_source_html_unchanged=True, stored_revision_unchanged=True))

@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A9-R3")
class A9R3EffectiveMediaPostgresTests(LocalPostgresFixture, A9R3EffectiveMediaReadTests):
    pass


for _case in A9_R3_CASES:
    def _check_effective_case(self, case=_case):
        self.scenario(case)
    for _cls in (A9R3EffectiveMediaReadTests, A9R3EffectiveMediaPostgresTests):
        setattr(_cls, "test_" + _case["name"], _check_effective_case)


class A9R3ContextContractTests(__import__("unittest").TestCase):
    def keys(self, value, context="HTML"):
        from app.services.media_references import _read_local_media_paths, MediaReadContext
        return set(_read_local_media_paths(value, context=getattr(MediaReadContext, context)))

    def test_srcset_comma_is_url_data(self):
        self.assertEqual(self.keys('<img srcset="/media/owned.png,foreign.png 1x">'),
                         {"/media/owned.png,foreign.png"}, "A9-R3 srcset effective target")

    def test_all_srcset_candidates_checked(self):
        self.assertEqual(self.keys('<img srcset="/media/own.png 1x, /media/foreign.png 2x">'),
                         {"/media/own.png", "/media/foreign.png"}, "A9-R3 all srcset candidates")

    def test_srcset_supported_grammar(self):
        for source in ('/media/a.png', '/media/a.png 1x, /media/b.png 2x',
                       '/media/a.png 400w, /media/b.png 800w', '/media/a.png, /media/b.png',
                       '/m%65dia/a.png 1x', '/media/a&amp;b.png 1x'):
            with self.subTest(source=source):
                self.assertTrue(self.keys('<img srcset="' + source + '">'))
        from app.services.media_storage import MediaPathError
        for source in ('', ', /media/a.png 1x', '/media/a.png 1x 2x', '/media/a.png 0x',
                       '/media/a.png 1x, /media/b.png 400w', '/media/a.png 1x, , /media/b.png 2x'):
            with self.subTest(source=source), self.assertRaises(MediaPathError):
                self.keys(source, "HTML_SRCSET")

    def test_css_escapes_are_decoded_inside_tokens(self):
        self.assertEqual(self.keys(r'url(/media/owned.png\).png)', "CSS"),
                         {"/media/owned.png).png"}, "A9-R3 CSS escaped delimiter")
        self.assertEqual(self.keys(r'url("/media/foreign\22 .png")', "CSS"), {'/media/foreign".png'})

    def test_css_nbsp_is_url_data(self):
        self.assertEqual(self.keys('url("/media/owned.png\u00a0")'.replace('\\u00a0', '\u00a0'), "CSS"),
                         {"/media/owned.png\u00a0"}, "A9-R3 CSS NBSP effective target")

    def test_image_set_string_urls_checked(self):
        self.assertEqual(self.keys('image-set("/media/foreign.png" 1x)', "CSS_IMAGE_SET"),
                         {"/media/foreign.png"}, "A9-R3 image-set string")

    def test_all_image_set_candidates_checked(self):
        self.assertEqual(self.keys('image-set(url("/media/own.png") 1x, "/media/foreign.png" 2x)', "CSS"),
                         {"/media/own.png", "/media/foreign.png"}, "A9-R3 all image-set candidates")

    def test_image_set_supported_and_unsupported_grammar(self):
        for value in ('image-set("/media/a" 2dppx type("image/png"))',
                      '-webkit-image-set(url(/media/a) 1x, url(/media/b) 2x)',
                      'image-set("/media/a" 96dpi, "/media/b" 37.8dpcm)'):
            self.assertTrue(self.keys(value, "CSS"))
        from app.services.media_storage import MediaPathError
        for value in ('image-set()', 'image-set("/media/a" 1x,)', 'image-set("/media/a" nonsense)',
                      'image-set("/media/a" 1x 2x)', 'image-set(var(--source) 1x)'):
            with self.subTest(value=value), self.assertRaises(MediaPathError):
                self.keys(value, "CSS")

    def test_base_is_resolved_before_relative_reference(self):
        self.assertEqual(self.keys('<base href="/media/owned.html"><img src="foreign.png">'),
                         {"/media/owned.html", "/media/foreign.png"}, "A9-R3 effective base target")

    def test_relative_parent_resolution(self):
        self.assertEqual(self.keys('<base href="/media/A/owned.html"><img src="../B/foreign.png">'),
                         {"/media/A/owned.html", "/media/B/foreign.png"}, "A9-R3 relative resolution order")

    def test_base_matrix(self):
        self.assertEqual(self.keys('<img src="/media/a">'), {"/media/a"})
        self.assertEqual(self.keys('<base href="https://cdn.example/base/"><img src="relative.png"><img src="/media/foreign">'), set())
        self.assertEqual(self.keys('<base href="https://cdn.example/base/"><img src="http://testserver/media/a">'), {"/media/a"})
        from app.services.media_storage import MediaPathError
        for value in ('<base>', '<base href="">', '<base href="/media/a"><base href="/media/b">',
                      '<base href="javascript:evil"><img src="a">', '<img src="unknown.png">',
                      '<base href="../media/a"><img src="b">',
                      '<base href="/media/a" href="/media/b"><img src="a">'):
            with self.subTest(value=value), self.assertRaises(MediaPathError):
                self.keys(value)

    def test_unquoted_non_html_whitespace_is_ambiguous(self):
        from app.services.media_storage import MediaPathError
        # HTML5 does not treat NBSP as an unquoted attribute delimiter.
        self.assertEqual(self.keys('<img src=/media/owned.png\u00a0>'), {"/media/owned.png\u00a0"})
        self.assertEqual(self.keys('<img src="/media/owned.png\u00a0">'), {"/media/owned.png\u00a0"})

    def test_opaque_mime_text_is_not_a_relative_resource_attribute(self):
        for mime in ("text/html", "application/xhtml+xml", "image/svg+xml", "video/mp4"):
            self.assertEqual(self.keys({"content_type": mime}, "STRUCTURED"), set())
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError):
            self.keys('<img src="video/mp4">')

    def test_meta_refresh_explicit_url_context(self):
        from app.services.media_storage import MediaPathError
        self.assertEqual(self.keys('<meta http-equiv="refresh" content="0; url=/media/owned.png,foreign.png">'),
                         {'/media/owned.png,foreign.png'}, "A9-R3 meta refresh full target")
        self.assertEqual(self.keys('<meta http-equiv="refresh" content="0; url=/media/owned.png\u00a0foreign.png">'),
                         {'/media/owned.png\u00a0foreign.png'})
        self.assertEqual(self.keys("<meta http-equiv=\"refresh\" content=\"0; url='/media/owned.png,foreign.png'\">"),
                         {'/media/owned.png,foreign.png'})
        self.assertEqual(self.keys('<meta http-equiv="refresh" content="0">'), set())
        for markup in ('<meta http-equiv="refresh" content="0; url=">',
                       '<meta http-equiv="refresh" content="0; url=relative.png">',
                       '<base href="/media/owned.html"><meta http-equiv="refresh" content="0; url=/media/owned.png">'):
            with self.assertRaises(MediaPathError):
                self.keys(markup)

    def test_opaque_mixed_text_requires_whole_url(self):
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError, msg="A9-R3 opaque partial URL approved"):
            self.keys({'note': 'prefix /media/owned.png,foreign.png'}, "STRUCTURED")
        self.assertEqual(self.keys({'note': 'plain text without a media URL'}, "STRUCTURED"), set())
        self.assertEqual(self.keys({'url': '/media/owned.png'}, "STRUCTURED"), {'/media/owned.png'})
        for template in ('<img src="{}">', 'background-image: url("{}")'):
            for key in ('/media/owned.png', '/media/owned.png,foreign.png'):
                self.assertEqual(self.keys({'note': template.format(key)}, "STRUCTURED"), {key})
        for suffix in (',foreign.png', '\u00a0foreign.png'):
            with self.assertRaises(MediaPathError, msg="A9-R3 mixed HTML text cannot approve a substring"):
                self.keys('prefix /media/owned.png' + suffix, "HTML")
        for suffix in (')foreign.png', ';foreign.png', ',foreign.png'):
            with self.assertRaises(MediaPathError):
                self.keys({'note': 'prefix /media/owned.png' + suffix}, "STRUCTURED")

    def test_typed_boundary_whitespace_is_effective(self):
        self.assertEqual(self.keys('/media/foreign.png ', "TYPED_URL"),
                         {"/media/foreign.png"}, "A9-R3 typed boundary whitespace")
        self.assertEqual(self.keys('\t /media/a\r\n', "TYPED_URL"), {"/media/a"})
        self.assertEqual(self.keys('/media/a%20', "TYPED_URL"), {"/media/a "})
        self.assertEqual(self.keys('/media/a\u00a0', "TYPED_URL"), {"/media/a\u00a0"})

    def test_typed_entity_never_decoded(self):
        self.assertEqual(self.keys('/media/&#x41;/file.png', "TYPED_URL"),
                         {"/media/&"}, "A9-R3 typed HTML entity separation")

    def test_ambiguous_css_is_omitted(self):
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError, msg="A9-R3 ambiguous CSS accepted"):
            self.keys('background:var(--foreign)', "CSS")
        for value in ('url("/media/a"', 'url(/media/a', 'image-set("/media/a" 1x',
                      'url("/media/a\n")', '/* comment', 'url(/media/a) }'):
            with self.subTest(value=value), self.assertRaises(MediaPathError):
                self.keys(value, "CSS")

    def test_effective_key_is_not_renormalized_as_markup(self):
        self.assertEqual(self.keys('/media/a%3Fb%23c?query=1#fragment', "TYPED_URL"),
                         {"/media/a?b#c"}, "A9-R3 effective key equality")


class A9R3TerminalDecisionTests(TenantFixture):
    def test_explicit_terminal_decisions(self):
        from app.services.media_references import resolve_effective_media_reference, MediaReadContext as C, MediaReadDecision as D
        a, b = self.res["A"], self.res["B"]
        for value, expected in ((a["content"].file_path, D.SAFE_LOCAL_OWNED),
                                (b["content"].file_path, D.FOREIGN),
                                ('/media/missing-unowned.png', D.UNKNOWN),
                                ('https://cdn.example/image.png', D.SAFE_EXTERNAL),
                                ('/m%2565dia/unknown', D.AMBIGUOUS), ('/media/\x00', D.INVALID)):
            with self.subTest(value=repr(value)):
                result = resolve_effective_media_reference(self.db, self.tenants["A"].id, value, context=C.TYPED_URL)
                self.assertEqual(result.decision, expected)

    def test_foreign_candidate_rejects_whole_widget(self):
        from app.services.media_references import read_payload_media_decision, MediaReadContext as C, MediaReadDecision as D
        a, b = self.res["A"], self.res["B"]
        for html in ('<img srcset="' + a['content'].file_path + ' 1x, ' + b['content'].file_path + ' 2x">',
                     '<style>.x{background:image-set("' + a['content'].file_path + '" 1x,"' + b['content'].file_path + '" 2x)}</style>'):
            self.assertEqual(read_payload_media_decision(self.db, self.tenants['A'].id, html, context=C.HTML), D.FOREIGN)


class A9R3OpaqueMediaReadTests(TenantFixture):
    media_read = A9R1MediaReadTests.media_read
    scenario = A9R3EffectiveMediaReadTests.scenario

    def test_mixed_text_does_not_leak_foreign_path(self):
        self.scenario(dict(name="opaque_mixed_text", context="STRUCTURED",
                           owned="/media/owned.png", foreign="/media/owned.png,foreign.png",
                           opaque="prefix /media/owned.png,foreign.png"))

    def test_meta_refresh_comma_preserves_full_target(self):
        self.scenario(dict(name="meta_refresh_comma", context="HTML",
                           owned="/media/owned.png", foreign="/media/owned.png,foreign.png",
                           html='<meta http-equiv="refresh" content="0; url=/media/owned.png,foreign.png">'))

    def test_meta_refresh_nbsp_preserves_full_target(self):
        self.scenario(dict(name="meta_refresh_nbsp", context="HTML",
                           owned="/media/owned.png", foreign="/media/owned.png\u00a0foreign.png",
                           html='<meta http-equiv="refresh" content="0; url=/media/owned.png\u00a0foreign.png">'))

    def test_html_text_nbsp_does_not_grant_partial_ownership(self):
        self.scenario(dict(name="html_mixed_nbsp", context="HTML",
                           owned="/media/owned.png", foreign="/media/owned.png\u00a0foreign.png",
                           html="prefix /media/owned.png\u00a0foreign.png"))

    def test_html_text_keeps_complete_foreign_key(self):
        self.scenario(dict(name="html_mixed_text", context="HTML",
                           owned="/media/owned.png", foreign="/media/owned.png,foreign.png",
                           html="prefix /media/owned.png,foreign.png"))


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A9-R3")
class A9R3OpaqueMediaPostgresTests(LocalPostgresFixture, A9R3OpaqueMediaReadTests):
    pass


# Exact independent A9-R3 audit corpus, not substitute payloads.
A9_R4_CASES = [{'name': 'entity_ampx_double', 'context': 'HTML', 'html': '<img src="/media/owned&ampx.png">', 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_ampx_single', 'context': 'HTML', 'html': "<img src='/media/owned&ampx.png'>", 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_ampx_bare', 'context': 'HTML', 'html': '<img src=/media/owned&ampx.png>', 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_ampx_srcset', 'context': 'HTML', 'html': '<img srcset="/media/owned&ampx.png 1x">', 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_ampx_style', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:url("/media/owned&ampx.png")\'></div>', 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_notit_double', 'context': 'HTML', 'html': '<img src="/media/owned&notit;.png">', 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_notit_single', 'context': 'HTML', 'html': "<img src='/media/owned&notit;.png'>", 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_notit_bare', 'context': 'HTML', 'html': '<img src=/media/owned&notit;.png>', 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_notit_srcset', 'context': 'HTML', 'html': '<img srcset="/media/owned&notit;.png 1x">', 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_notit_style', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:url("/media/owned&notit;.png")\'></div>', 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_copyx_double', 'context': 'HTML', 'html': '<img src="/media/owned&copyx.png">', 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_copyx_single', 'context': 'HTML', 'html': "<img src='/media/owned&copyx.png'>", 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_copyx_bare', 'context': 'HTML', 'html': '<img src=/media/owned&copyx.png>', 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_copyx_srcset', 'context': 'HTML', 'html': '<img srcset="/media/owned&copyx.png 1x">', 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_copyx_style', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:url("/media/owned&copyx.png")\'></div>', 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_amp_one_double', 'context': 'HTML', 'html': '<img src="/media/owned&amp=one.png">', 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'entity_amp_one_single', 'context': 'HTML', 'html': "<img src='/media/owned&amp=one.png'>", 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'entity_amp_one_bare', 'context': 'HTML', 'html': '<img src=/media/owned&amp=one.png>', 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'entity_amp_one_srcset', 'context': 'HTML', 'html': '<img srcset="/media/owned&amp=one.png 1x">', 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'entity_amp_one_style', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:url("/media/owned&amp=one.png")\'></div>', 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'entity_ampx_image_set', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:image-set("/media/owned&ampx.png" 1x)\'></div>', 'owned': '/media/owned&x.png', 'foreign': '/media/owned&ampx.png'}, {'name': 'entity_notit_image_set', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:image-set("/media/owned&notit;.png" 1x)\'></div>', 'owned': '/media/owned¬it;.png', 'foreign': '/media/owned&notit;.png'}, {'name': 'entity_copyx_image_set', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:image-set("/media/owned&copyx.png" 1x)\'></div>', 'owned': '/media/owned©x.png', 'foreign': '/media/owned&copyx.png'}, {'name': 'entity_amp_one_image_set', 'context': 'HTML', 'html': '<div class=\'probe\' style=\'width:40px;height:40px;background:image-set("/media/owned&amp=one.png" 1x)\'></div>', 'owned': '/media/owned&=one.png', 'foreign': '/media/owned&amp=one.png'}, {'name': 'inert_base_template', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<template><base href="https://outside.example/"></template><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_textarea', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<textarea><base href="https://outside.example/"></textarea><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_title', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<title><base href="https://outside.example/"></title><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_noscript', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<noscript><base href="https://outside.example/"></noscript><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_iframe', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<iframe><base href="https://outside.example/"></iframe><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_xmp', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<xmp><base href="https://outside.example/"></xmp><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_noembed', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<noembed><base href="https://outside.example/"></noembed><img src="/media/foreign.png">', 'browser_local_foreign': True}, {'name': 'inert_base_noframes', 'context': 'HTML', 'owned': '/media/owned.png', 'foreign': '/media/foreign.png', 'html': '<noframes><base href="https://outside.example/"></noframes><img src="/media/foreign.png">', 'browser_local_foreign': True}]


class A9R4HTMLSemanticsTests(__import__("unittest").TestCase):
    def keys(self, markup):
        from app.services.media_references import _read_local_media_paths, MediaReadContext
        return set(_read_local_media_paths(markup, context=MediaReadContext.HTML))

    def view(self, markup, mode="document"):
        from app.services.media_references import _html_media_view
        return _html_media_view(markup, mode=mode)

    def test_inert_base_tree_semantics(self):
        for tag in ("template", "textarea", "title", "noscript", "iframe", "xmp", "noembed", "noframes"):
            markup = '<' + tag + '><base href="https://outside.example/"></' + tag + '><img src="/media/foreign.png">'
            for mode in ("document", "fragment"):
                with self.subTest(tag=tag, mode=mode):
                    view = self.view(markup, mode)
                    self.assertIsNone(view.base, "A9-R4 inactive base altered document origin")
                    self.assertEqual([c.effective_url for c in view.candidates], ["/media/foreign.png"],
                                     "A9-R4 inactive base changed validated URL")

    def test_template_projection_is_fail_closed(self):
        from app.services.media_storage import MediaPathError
        for markup in ('<template><base href="https://outside.example/"></template><img src="/media/foreign.png">',
                       '<template><table><base href="https://outside.example/"></table></template><img src="/media/foreign.png">'):
            with self.assertRaises(MediaPathError, msg="A9-R4 unsupported template mode accepted"):
                self.keys(markup)

    def test_first_eligible_base_after_inactive_base(self):
        for tag in ("textarea", "title", "noscript", "iframe", "xmp", "noembed", "noframes"):
            value = '<' + tag + '><base href="https://outside.example/"></' + tag + '><base href="/media/owned.html"><img src="foreign.png">'
            self.assertEqual({c.effective_url for c in self.view(value).candidates}, {"http://testserver/media/owned.html", "http://testserver/media/foreign.png"})

    def test_ambiguous_entity_effective_attributes(self):
        for case in A9_R4_CASES:
            if not case['name'].startswith('entity_'):
                continue
            with self.subTest(case=case['name']):
                self.assertEqual({c.effective_url for c in self.view(case['html']).candidates}, {case['foreign']},
                                 "A9-R4 ambiguous entity resolved to a different key")

    def test_ambiguous_refs_projection_omitted(self):
        from app.services.media_storage import MediaPathError
        for case in A9_R4_CASES:
            if case['name'].startswith('entity_'):
                with self.subTest(case=case['name']), self.assertRaises(MediaPathError, msg="A9-R4 cross-consumer entity ambiguity admitted"):
                    self.keys(case['html'])

    def test_attribute_not_reparsed(self):
        self.assertEqual(self.keys('<img src="/media/a&amp;amp;b.png">'), {"/media/a&amp;b.png"},
                         "A9-R4 second HTML attribute decode")
        self.assertEqual({c.effective_url for c in self.view('<img src="/media/owned&ampx.png">').candidates}, {"/media/owned&ampx.png"},
                         "A9-R4 ambiguous ampersand decoded")

    def test_srcset_not_redecoded(self):
        self.assertEqual(self.keys('<img srcset="/media/a&amp;amp;b.png 1x">'), {"/media/a&amp;b.png"},
                         "A9-R4 srcset second HTML decode")

    def test_style_not_redecoded(self):
        self.assertEqual(self.keys('<div style="background:url(/media/a&amp;amp;b.png)"></div>'),
                         {"/media/a&amp;b.png"}, "A9-R4 style second HTML decode")
        self.assertEqual(self.keys('<style>.a{background:url(/media/a&amp;amp;b.png)}</style>'),
                         {"/media/a&amp;amp;b.png"}, "A9-R4 style raw-text HTML decode")

    def test_css_escape_not_redecoded(self):
        # CSS emits the literal HTML-looking entity; it is not parsed as HTML.
        self.assertEqual(self.keys(r'<style>.a{background:url(/media/a\26 ampx.png)}</style>'),
                         {"/media/a&ampx.png"}, "A9-R4 CSS to HTML re-decode")

    def test_rawtext_is_not_reparsed(self):
        self.assertEqual({c.effective_url for c in self.view('<textarea><img src="/media/foreign.png"></textarea><img src="/media/owned.png">').candidates},
                         {"/media/owned.png"}, "A9-R4 rawtext reparsed as markup")
        self.assertEqual({c.effective_url for c in self.view('<noscript><img src="/media/foreign.png"></noscript><img src="/media/owned.png">').candidates},
                         {"/media/owned.png"}, "A9-R4 scripting mode mismatch")

    def test_inert_markup_never_discloses_a_foreign_reference(self):
        from app.services.media_storage import MediaPathError
        for tag in ("template", "textarea", "title", "noscript", "iframe", "xmp", "noembed", "noframes"):
            value = '<' + tag + '><img src="/media/foreign.png"></' + tag + '><img src="/media/owned.png">'
            with self.subTest(tag=tag), self.assertRaises(MediaPathError, msg="A9-R4 inert foreign URL disclosed"):
                self.keys(value)

    def test_provenance_survives_html_css_and_base(self):
        view = self.view('''<base href="/media/a.html"><img src="b.png" srcset="c.png 1x"><div style='background:image-set("d.png" 1x),url(e.png)'></div>''')
        contexts = {candidate.context for candidate in view.candidates}
        self.assertEqual(contexts, {"HTML_BASE", "HTML_ATTRIBUTE", "HTML_SRCSET", "CSS_IMAGE_SET", "CSS_URL"})
        self.assertTrue(all("BASE_RESOLVED_RELATIVE" in candidate.provenance for candidate in view.candidates if candidate.context != "HTML_BASE"))

    def test_unknown_context_and_dynamic_markup_omit(self):
        from app.services.media_storage import MediaPathError
        for value in ('<img src="/media/a" onerror="fetch(1)">', '<iframe srcdoc="&lt;img src=/media/a&gt;"></iframe>',
                      '&lt;img src="/media/a"&gt;', '<svg><image href="/media/a"/></svg>',
                      '<img src="/media/a" src="/media/b">', '<base href="/media/a"><base href="/media/b">'):
            with self.subTest(value=value), self.assertRaises(MediaPathError):
                self.keys(value)


class A9R4EffectiveMediaReadTests(TenantFixture):
    media_read = A9R1MediaReadTests.media_read
    scenario = A9R3EffectiveMediaReadTests.scenario

    def test_fragment_entity_reverse_ownership_style(self):
        self.scenario(dict(name="fragment_reverse_notit_style", context="HTML",
                           owned="/media/owned&notit;.png", foreign="/media/owned¬it;.png",
                           html='<div style=\'background:url("/media/owned&notit;.png")\'></div>'))

    def test_fragment_entity_reverse_ownership_image_set(self):
        self.scenario(dict(name="fragment_reverse_notit_image_set", context="HTML",
                           owned="/media/owned&notit;.png", foreign="/media/owned¬it;.png",
                           html='<div style=\'background:image-set("/media/owned&notit;.png" 1x)\'></div>'))


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A9-R4")
class A9R4EffectiveMediaPostgresTests(LocalPostgresFixture, A9R4EffectiveMediaReadTests):
    pass


A9_R4_INERT_DISCLOSURE_CASES = [dict(name="inert_disclosure_" + tag, context="HTML",
    owned="/media/owned.png", foreign="/media/foreign.png",
    html='<' + tag + '><img src="/media/foreign.png"></' + tag + '><img src="/media/owned.png">')
    for tag in ("template", "textarea", "title", "noscript", "iframe", "xmp", "noembed", "noframes")]

for _r4_case in A9_R4_CASES + A9_R4_INERT_DISCLOSURE_CASES:
    def _r4_check(self, case=_r4_case):
        self.scenario(case)
    for _r4_class in (A9R4EffectiveMediaReadTests, A9R4EffectiveMediaPostgresTests):
        setattr(_r4_class, "test_" + _r4_case["name"], _r4_check)
del _r4_class, _r4_case, _r4_check


class A10R1SerializationContextTests(__import__("unittest").TestCase):
    def keys(self, value, context="STRUCTURED"):
        from app.services import media_references as refs
        return set(refs._read_local_media_paths(value, context=getattr(refs.MediaReadContext, context)))

    def assert_base_rejected(self, attribute):
        from app.services.media_storage import MediaPathError
        markup = '<base href="/media/owned.png" ' + attribute + '><img src="/media/owned.png">'
        with self.assertRaises(MediaPathError, msg="A10-R1 unsupported base attribute survived"):
            self.keys(markup, "HTML")

    def test_base_other_attribute(self):
        self.assert_base_rejected('title="/media/foreign.png"')

    def test_base_style_attribute(self):
        self.assert_base_rejected('style="background:url(/media/foreign.png)"')

    def test_base_data_attribute(self):
        self.assert_base_rejected('data-leak="/media/foreign.png"')

    def test_base_event_attribute(self):
        for name in ("onclick", "onload", "onerror", "oncustom"):
            with self.subTest(name=name):
                self.assert_base_rejected(name + '="fetch(\'/media/foreign.png\')"')

    def test_base_unsupported_attributes_are_not_partially_repaired(self):
        for attribute in ('style="color:red"', 'data-label="ordinary"', 'class="ordinary"', 'hidden'):
            with self.subTest(attribute=attribute):
                self.assert_base_rejected(attribute)

    def test_base_href_target_keep_existing_resolution(self):
        for target in ("", "_blank", "_BLANK", "_self", "_parent", "_top", "screen-1", "pantalla_ñ"):
            with self.subTest(target=target):
                raw = '<base href="/media/owned.html" target="' + target + '"><img src="owned.png">'
                self.assertEqual(self.keys(raw, "HTML"), {"/media/owned.html", "/media/owned.png"})

    def test_base_target_is_a_name_not_an_unchecked_url(self):
        for target in ("/media/foreign.png", "https://testserver/media/foreign.png", "<opaque>"):
            with self.subTest(target=target):
                self.assert_base_rejected('target="' + target + '"')

    def test_json_top_level_key(self):
        self.assertEqual(self.keys({"/media/foreign.png": "ordinary"}), {"/media/foreign.png"},
                         "A10-R1 top-level JSON key skipped")

    def test_json_nested_keys(self):
        for value in ({"outer": {"/media/foreign.png": "ordinary"}},
                      {"outer": [{"/media/foreign.png": "ordinary"}]},
                      [{"outer": {"deeper": {"/media/foreign.png": "ordinary"}}}]):
            with self.subTest(value=value):
                self.assertEqual(self.keys(value), {"/media/foreign.png"}, "A10-R1 nested JSON key skipped")

    def test_json_widget_keys_and_values(self):
        value = {"id": "ordinary", "widget_type": "image", "/media/top.png": "label",
                 "props_json": {"sourceUrl": "/media/owned.png", "/media/prop.png": "label",
                                "config": [{"/media/nested.png": "/media/value.png"}]}}
        self.assertEqual(self.keys(value, "WIDGET"),
                         {"/media/top.png", "/media/owned.png", "/media/prop.png", "/media/nested.png", "/media/value.png"},
                         "A10-R1 JSON keys and values must both be inspected")

    def test_json_aliases_use_existing_canonicalizer(self):
        from unittest.mock import patch
        from app.core.config import get_settings
        with patch.object(get_settings(), "PUBLIC_BASE_URL", "https://testserver"):
            for key in ("/media/foreign.png", "https://TESTSERVER/media/foreign.png",
                        "https://testserver:443/media/foreign.png", "//TESTSERVER:443/media/foreign.png",
                        "/m%65dia/foreign%2Epng", "%2fmedia/foreign.png", "media/foreign.png",
                        "/media/foreign.png?q=1#part", "\t /media/foreign.png \r\n"):
                with self.subTest(key=key):
                    self.assertEqual(self.keys({key: "ordinary"}), {"/media/foreign.png"})
            for key in ('label "m%65dia/foreign.png"', "label 'm%65dia/foreign.png'",
                        'label "%2fmedia/foreign.png"', 'label "med%69a/foreign.png"',
                        'label "https://TESTSERVER:443/m%65dia/foreign.png?q=1#part"'):
                with self.subTest(key=key):
                    self.assertEqual(self.keys({key: "ordinary"}), {"/media/foreign.png"})

    def test_json_key_context_never_parses_html_or_css(self):
        from unittest.mock import patch
        from app.services import media_references as refs
        with patch.object(refs, "_html_media_view", wraps=refs._html_media_view) as html_view, \
                patch.object(refs, "_css_media_urls", wraps=refs._css_media_urls) as css_urls:
            self.assertEqual(self.keys('<img src="/media/owned&amp;.png">', "JSON_KEY"), {"/media/owned&amp;.png"})
            self.assertEqual(self.keys('background:url("/media/owned&amp;.png")', "JSON_KEY"), {"/media/owned&amp;.png"})
            self.assertEqual(html_view.call_count, 0, "A10-R1 JSON key inferred HTML semantics")
            self.assertEqual(css_urls.call_count, 0, "A10-R1 JSON key inferred CSS semantics")

    def test_json_key_typed_entities_and_delimiters_remain_literal(self):
        self.assertEqual(self.keys("/media/&#x41;/file.png", "JSON_KEY"), {"/media/&"})
        self.assertEqual(self.keys("/media/a%3Fb%23c?q=1#part", "JSON_KEY"), {"/media/a?b#c"})
        self.assertEqual(self.keys("/media/a%20", "JSON_KEY"), {"/media/a "})

    def test_json_normal_keys_and_external_url_keys(self):
        for key in ("width", "label", "https://external.example/x", "100% width", "color:label", "<b>literal</b>",
                    'label "100% natural"', "label '100%'", 'ratio "100% / 20%"',
                    'label "discount%2520"', 'label "path/100%"'):
            with self.subTest(key=key):
                self.assertEqual(self.keys({key: "ordinary"}), set())

    def test_json_depth_is_bounded_without_losing_nested_references(self):
        from app.services.media_storage import MediaPathError
        value = {"/media/foreign.png": "ordinary"}
        for _ in range(32):
            value = {"layer": [value]}
        # 32 dict/list pairs reach the declared boundary.
        self.assertEqual(self.keys(value), {"/media/foreign.png"})
        for _ in range(2):
            value = {"layer": value}
        with self.assertRaises(MediaPathError):
            self.keys(value)

    def test_json_ambiguous_or_unbounded_local_key_fails_closed(self):
        from app.services.media_storage import MediaPathError
        for key in ("/m%2565dia/foreign.png", "/media/foreign%ZZ.png", "prefix /media/foreign.png",
                    "/media/foreign.png\\suffix", "/media/\x00", "\ud800", "prefix m%65dia/foreign.png",
                    'label "m%2565dia/foreign.png"', 'label "m%65dia/foreign%ZZ.png"',
                    'label "med%69a/foreign%ZZ.png"', 'label "%252fmedia%252fforeign.png"'):
            with self.subTest(key=repr(key)), self.assertRaises(MediaPathError):
                self.keys({key: "ordinary"})


def a10_r1_returned_strings(value, *, keys_only=False):
    """Independent QA walk of the actual serialized representation, not a sanitizer."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from a10_r1_returned_strings(child, keys_only=keys_only)
    elif isinstance(value, list):
        for child in value:
            yield from a10_r1_returned_strings(child, keys_only=keys_only)
    elif isinstance(value, str) and not keys_only:
        yield value


class A10R1SerializationProjectionTests(TenantFixture):
    def test_foreign_json_key_is_not_external_safe(self):
        from app.services import media_references as refs
        from copy import deepcopy
        foreign = self.res["B"]["content"].file_path
        for payload in ({foreign: "ordinary"}, {"outer": [{foreign: "ordinary"}]},
                        {"props_json": {"config": [{foreign: "ordinary"}]}}):
            before = deepcopy(payload)
            for context in (refs.MediaReadContext.STRUCTURED, refs.MediaReadContext.WIDGET):
                self.assertEqual(refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                                 payload, context=context), refs.MediaReadDecision.FOREIGN,
                                 "A10-R1 foreign JSON key treated as external-safe")
            self.assertEqual(payload, before)

    def revision_payload(self, *, unsafe_sibling=False):
        from copy import deepcopy
        from app.services import layout_editor
        a, b = self.res["A"], self.res["B"]
        foreign = b["content"].file_path
        state = {"widgets": [dict(id="safe-widget", widget_type="image", props_json={"sourceUrl": a["content"].file_path}),
                              dict(id="unsafe-widget", widget_type="image", props_json={
                                  foreign: "ordinary", "sourceUrl": foreign if unsafe_sibling else a["content"].file_path})]}
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="A10-R1 QA", status="draft", editor_state_json=deepcopy(state))
        self.db.commit()
        payload = layout_editor.safe_revision_payload(revision, a["layout"], self.db)
        self.db.expire_all()
        self.assertEqual(revision.editor_state_json, state, "A10-R1 projection changed stored JSON")
        self.assertIn("safe-widget", list(a10_r1_returned_strings(payload)))
        return foreign, payload

    def test_partial_key_scrub_cannot_keep_unsafe_sibling(self):
        foreign, payload = self.revision_payload(unsafe_sibling=True)
        self.assertNotIn(foreign, list(a10_r1_returned_strings(payload, keys_only=True)))
        self.assertNotIn(foreign, list(a10_r1_returned_strings(payload)),
                         "A10-R1 key scrub retained unsafe sibling content")

    def test_final_serialized_revision_has_no_foreign_key(self):
        foreign, payload = self.revision_payload()
        self.assertNotIn(foreign, list(a10_r1_returned_strings(payload, keys_only=True)),
                         "A10-R1 final safe-payload JSON key verification failed")
        self.assertNotIn("unsafe-widget", list(a10_r1_returned_strings(payload)))

    def test_legitimate_metadata_keys_are_returned_unchanged(self):
        from copy import deepcopy
        a = self.res["A"]
        raw = {"width": 1920, "label": "ordinary", "https://external.example/x": [
            {a["content"].file_path: {"custom-config": {"nested": True}}}],
            'label "100% natural"': "ordinary", "label '100%'": "ordinary"}
        a["content"].metadata_json = deepcopy(raw)
        self.db.commit()
        response = self.request("GET", "/api/contents/")
        self.assertEqual(response.status_code, 200, response.text)
        item = next((row for row in response.json() if row["id"] == a["content"].id), None)
        self.assertIsNotNone(item, "A10-R1 ordinary non-media JSON key omitted content")
        self.assertEqual(item["metadata_json"], raw)
        self.db.expire_all()
        self.assertEqual(a["content"].metadata_json, raw)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A10-R1")
class A10R1SerializationProjectionPostgresTests(LocalPostgresFixture, A10R1SerializationProjectionTests):
    pass


# A10-R2: opaque JSON-key URI candidates, independently checked after projection.
A10_R2_JSON_KEY_OBSERVATIONS = []
A10_R2_READ_EFFECTS = []
A10_R2_STORED_INVARIANTS = []
A10_R2_AUDIT_VARIANTS = (
    "top_parentheses_m65", "top_slash_parentheses_m65",
    "nested_parentheses_med69", "list_brackets_m65", "deep_equals_m65",
)


def a10_r2_audit_key_payload(foreign, variant):
    """Exact constructors from the independent 256-response/byte-retrieval audit."""
    encoded = foreign.replace("/media/", "med%69a/" if variant == "nested_parentheses_med69" else "m%65dia/")
    encoded = ("/" + encoded) if variant == "top_slash_parentheses_m65" else encoded
    key = ("label[" + encoded + "]") if variant == "list_brackets_m65" else (
        "label=" + encoded) if variant == "deep_equals_m65" else "label(" + encoded + ")"
    raw = {key: "synthetic-label"}
    if variant == "nested_parentheses_med69":
        raw = {"outer": raw}
    elif variant == "list_brackets_m65":
        raw = {"outer": [raw]}
    elif variant == "deep_equals_m65":
        for _ in range(24):
            raw = {"layer": [raw]}
    return raw, encoded


def a10_r2_assert_no_foreign_media(case, payload, foreign):
    """QA assertion on final keys AND values; never sanitizes or changes payloads."""
    from urllib.parse import unquote
    needle = foreign.removeprefix("/")
    leaks = [text for text in a10_r1_returned_strings(payload)
             if needle in unquote(text, encoding="utf-8", errors="strict")]
    case.assertFalse(leaks, "A10-R2 final payload returned foreign media key/value")


class A10R2JSONKeyContextTests(__import__("unittest").TestCase):
    def keys(self, value, context="STRUCTURED"):
        from app.services import media_references as refs
        return set(refs._read_local_media_paths(value, context=getattr(refs.MediaReadContext, context)))

    def assert_embedded_rejected(self, key):
        from app.services.media_storage import MediaPathError
        with self.assertRaises(MediaPathError, msg="A10-R2 embedded media candidate bypassed"):
            self.keys({key: "ordinary"})

    def test_embedded_prefix_scan(self):
        for key in ("prefix:/media/B/file.png", "foo(media/B/file.png)",
                    "foo(/media/B/file.png)", "label=media/B/file.png",
                    "text[media/B/file.png]", "x{media/B/file.png}"):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_leading_slash_not_required(self):
        self.assert_embedded_rejected("label(m%65dia/B/file.png)")

    def test_percent_encoded_embedded(self):
        for token in ("m%65dia/B/file.png", "%6dedia/B/file.png", "med%69a/B/file.png",
                      "%6d%65%64%69%61/B/file.png", "m%65%64ia/B/file%2Epng"):
            with self.subTest(token=token):
                self.assert_embedded_rejected("wrapper(" + token + ")")

    def test_relative_media_candidate(self):
        for raw in ("media/B/file.png", "m%65dia/B/file.png", "%6dedia/B/file.png"):
            with self.subTest(raw=raw):
                self.assertEqual(self.keys({raw: "ordinary"}), {"/media/B/file.png"},
                                 "A10-R2 relative whole-key URI was treated as external")
                self.assert_embedded_rejected("prefix:" + raw)

    def test_nested_key_walk(self):
        raw = {"label(m%65dia/B/file.png)": "ordinary"}
        variants = [raw, {"outer": raw}, {"outer": [raw]}, {"props_json": {"config": raw}}]
        deep = raw
        for _ in range(24):
            deep = {"layer": [deep]}
        variants.append(deep)
        from app.services.media_storage import MediaPathError
        for value in variants:
            for context in ("STRUCTURED", "WIDGET"):
                with self.subTest(value=value, context=context):
                    with self.assertRaises(MediaPathError, msg="A10-R2 nested JSON key was skipped"):
                        self.keys(value, context)

    def test_wrapper_equivalence(self):
        for template in ("label({})", "other({})", "[{}]", "{{{}}}", "field={}", "prefix:{}"):
            with self.subTest(template=template):
                self.assert_embedded_rejected(template.format("m%65dia/B/file.png"))

    def test_malformed_percent_encoding(self):
        for raw in ("m%65dia/B/file%ZZ.png", "media/B/file%2.png", "m%ZZdia/B/file.png",
                    "%6dedia/B/file%ff.png", "med%69a/B/%00.png"):
            with self.subTest(raw=raw):
                self.assert_embedded_rejected("label(" + raw + ")")

    def test_nested_percent_encoding(self):
        for raw in ("m%2565dia/B/file.png", "%256dedia/B/file.png",
                    "%252fmedia%252fB%252ffile.png", "med%2569a/B/file.png"):
            with self.subTest(raw=raw):
                self.assert_embedded_rejected("label(" + raw + ")")

    def test_encoded_separator_and_wrapper_candidates(self):
        for key in ("label(media%2fB%2ffile.png)", "label(m%65dia%2fB/file.png)",
                    "label%28m%65dia/B/file.png%29", "label%5bm%65dia/B/file.png%5d"):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_nested_encoded_wrappers_fail_closed(self):
        for key in ("label%2528m%2565dia/B.png%2529", "label%253Am%2565dia/B.png",
                    "label%2528%256dedia/B.png%2529"):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_later_components_fail_closed(self):
        for key in ("label(foo/x/m%252565dia/B.png)", "label(foo/x/m%25252565dia/B.png)",
                    "label(foo/x/m%6Gdia/B.png)", "label(prefix:m%6Gdia/B.png)",
                    "label(foo/x/m%256Gdia/B.png)"):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_whole_local_raw_path_boundaries_cannot_hide_candidate(self):
        for key in ("/media/A/owned(label(m%65dia/B/file.png))",
                    "/media/A/owned.png m%65dia/B/file.png",
                    "/media/A/owned/x/m%65dia/B/synthetic.mp4",
                    'label "/media/A/owned(label(m%65dia/B/file.png))"'):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_slash_control_rejected(self):
        self.assert_embedded_rejected("label(/m%65dia/B/file.png)")

    def test_multiple_quoted_candidates(self):
        key = 'pair "m%65dia/A/owned.png" "med%69a/B/foreign.png"'
        self.assertEqual(self.keys({key: "ordinary"}), {"/media/A/owned.png", "/media/B/foreign.png"},
                         "A10-R2 JSON key did not inspect every bounded URI candidate")
        self.assert_embedded_rejected('pair "https://external.example/x" label(m%65dia/B/foreign.png)')

    def test_uri_query_and_fragment_candidates(self):
        for key in ("/media/A/owned.png?next=m%65dia/B/foreign.png",
                    "/media/A/owned.png#label(m%65dia/B/foreign.png)",
                    'label "/media/A/owned.png?next=m%65dia/B/foreign.png"',
                    "https://external.example/x?next=m%65dia/B/foreign.png"):
            with self.subTest(key=key):
                self.assert_embedded_rejected(key)

    def test_external_query_field_cannot_mask_later_local_candidate(self):
        for separator in ("&", ";"):
            tail = "redirect=https://evil.example/x" + separator + "next=m%65dia/B/synthetic.mp4"
            for key in ("a=https://evil.example/x" + separator + "next=m%65dia/B/synthetic.mp4",
                        "/media/A/owned.png?" + tail, "/media/A/owned.png#" + tail,
                        'label "/media/A/owned.png?' + tail + '"',
                        'label "https://evil.example/x?' + tail + '"'):
                with self.subTest(separator=separator, key=key):
                    self.assert_embedded_rejected(key)

    def test_normal_percent_labels_and_external_hosts_preserved(self):
        keys = ("width", "height", "label", "foo.bar", "ordinary text", "100% width",
                'label "100% natural"', "label '100%'", 'ratio "100% / 20%"',
                'label "discount%2520"', 'label "path/100%"',
                "https://external.example/path", "https://evil.example/media/x",
                "https://api.tecnocontrolcloud.com.evil.example/media/x", "text(media.example/x)",
                "text(https://evil.example/media/x)", "https://evil.example/m%65dia/x",
                "https://api.tecnocontrolcloud.com.evil.example/m%65dia/x",
                "text(https://evil.example/m%65dia/x)",
                "text(https://api.tecnocontrolcloud.com.evil.example/m%65dia/x)",
                'label "https://evil.example/m%65dia/x"', "https://evil.example/a(media/B.png)",
                'label "https://evil.example/a%28media/B.png%29"',
                "https://evil.example/a&media/B.png", "https://evil.example/a;media/B.png",
                'label "https://evil.example/a&media/B.png"', 'label "https://evil.example/a;media/B.png"')
        for key in keys:
            with self.subTest(key=key):
                value = {key: "ordinary"}
                original = json.dumps(value, ensure_ascii=False)
                self.assertEqual(self.keys(value), set(), "A10-R2 ordinary/external JSON key was overblocked")
                self.assertEqual(json.dumps(value, ensure_ascii=False), original)

    def test_whole_and_quoted_uri_path_semantics_preserved(self):
        cases = (("/media/&#x41;/file.png", "/media/&"),
                 ("/media/a%3Fb%23c?q=1#part", "/media/a?b#c"),
                 ("/media/a%20", "/media/a "),
                 ('label "m%65dia/A/owned.png?q=1#part"', "/media/A/owned.png"))
        for key, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(self.keys({key: "ordinary"}), {expected})

    def test_json_values_remain_traversed(self):
        value = {'label "m%65dia/A/owned.png"': {"ordinary": "/media/B/foreign.png"}}
        self.assertEqual(self.keys(value), {"/media/A/owned.png", "/media/B/foreign.png"},
                         "A10-R2 JSON values stopped sharing authoritative ownership resolution")

    def test_json_key_context_never_calls_document_parsers(self):
        from unittest.mock import patch
        from app.services import media_references as refs
        with patch.object(refs, "_html_media_view", side_effect=AssertionError("HTML parser forbidden")), \
                patch.object(refs, "_css_media_urls", side_effect=AssertionError("CSS parser forbidden")), \
                patch.object(refs, "_srcset_media_urls", side_effect=AssertionError("srcset parser forbidden")):
            self.assertEqual(self.keys('<img src="/media/owned&amp;.png">', "JSON_KEY"),
                             {"/media/owned&amp;.png"})
            self.assert_embedded_rejected("label(m%65dia/B/file.png)")

    def test_reference_budget_still_applies_to_json_keys(self):
        from app.services.media_storage import MediaPathError
        value = {"/media/A/" + str(index) + ".png": "ordinary" for index in range(2049)}
        with self.assertRaises(MediaPathError):
            self.keys(value)

    def test_final_payload_verifier_negative_control(self):
        returned = {"outer": [{"label(m%65dia/B/foreign.png)": "ordinary"}]}
        with self.assertRaises(AssertionError, msg="A10-R2 final payload verifier was omitted"):
            a10_r2_assert_no_foreign_media(self, returned, "/media/B/foreign.png")
        a10_r2_assert_no_foreign_media(self, {"ordinary": "owned"}, "/media/B/foreign.png")
        with self.assertRaises(AssertionError):
            a10_r2_assert_no_foreign_media(self, {"ordinary": "m%65dia/B/foreign.png"}, "/media/B/foreign.png")


class A10R2EmbeddedMediaProjectionTests(TenantFixture):
    def media_read(self, path, who="A_client_admin"):
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        rows = lambda: {table.name: [dict(row._mapping) for row in self.db.execute(select(table).order_by(table.c.id))]
                        for table in Base.metadata.sorted_tables if "id" in table.c}
        self.db.expire_all()
        before = rows()
        self.db.rollback()
        counters = dict(INSERT=0, UPDATE=0, DELETE=0, FLUSH=0, COMMIT=0)
        def sql(conn, cursor, statement, params, context, many):
            op = statement.lstrip().split(None, 1)[0].upper()
            if op in counters:
                counters[op] += 1
        def flushing(*_):
            counters["FLUSH"] += 1
        def committing(*_):
            counters["COMMIT"] += 1
        event.listen(self.engine, "before_cursor_execute", sql)
        event.listen(Session, "before_flush", flushing)
        event.listen(Session, "before_commit", committing)
        try:
            response = A9R1MediaReadTests.media_read(self, path, who)
        finally:
            event.remove(self.engine, "before_cursor_execute", sql)
            event.remove(Session, "before_flush", flushing)
            event.remove(Session, "before_commit", committing)
        self.db.expire_all()
        after = rows()
        self.db.rollback()
        changes = []
        for table, old in before.items():
            old_map = {row["id"]: row for row in old}
            new_map = {row["id"]: row for row in after[table]}
            self.assertEqual(old_map.keys(), new_map.keys())
            for identity, row in old_map.items():
                for key, value in row.items():
                    if value != new_map[identity][key]:
                        changes.append((table, identity, key))
        self.assertTrue(all(table == "player_devices" and key in ("last_seen_at", "updated_at")
                            for table, identity, key in changes), "A10-R2 read changed stored business data")
        expected = dict(INSERT=0, UPDATE=1, DELETE=0, FLUSH=1, COMMIT=1) if path == "/api/player/runtime-config" else dict.fromkeys(counters, 0)
        self.assertEqual(counters, expected, "A10-R2 read side effects exceeded historical Player presence")
        A10_R2_READ_EFFECTS.append(dict(test=self.id(), platform=os.name, engine=self.engine.dialect.name,
            path=path, counters=counters, business_changes=0, presence_changes=changes))
        return response

    def exercise_audit_variant(self, variant):
        from copy import deepcopy
        from urllib.parse import unquote
        from app.services import media_references as refs
        import re
        a, b = self.res["A"], self.res["B"]
        foreign = b["content"].file_path
        raw, encoded = a10_r2_audit_key_payload(foreign, variant)
        self.assertEqual(refs.canonical_media_reference(encoded), ("LOCAL", foreign))
        control = refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                    {foreign: "ordinary"}, context=refs.MediaReadContext.STRUCTURED)
        self.assertEqual(control, refs.MediaReadDecision.FOREIGN)
        decision = refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                    raw, context=refs.MediaReadContext.STRUCTURED)
        self.assertNotIn(decision, (refs.MediaReadDecision.SAFE_EXTERNAL, refs.MediaReadDecision.SAFE_LOCAL_OWNED),
                         "A10-R2 encoded embedded relative media was external-safe")
        # H1 remains open; this direct control is not counted as a retrieval from a returned reference.
        direct = self.http.get(foreign)
        self.assertEqual((direct.status_code, direct.content), (200, b"MEDIA_SECRET_B"))
        a["content"].metadata_json = deepcopy(raw)
        self.db.commit()
        foreign_ids = {self.tenants["B"].id, *[row.id for row in b.values()]}
        pattern = re.compile(r"(?:m%65dia|med%69a)/[^\s()\[\]{}<>\"']+", re.IGNORECASE)

        def visit(surface, path, who):
            response = self.media_read(path, who)
            payload = response.json()
            serialized = json.dumps(payload)
            key_hits = [dict(returned_key=text, token=match.group())
                        for text in a10_r1_returned_strings(payload, keys_only=True)
                        for match in pattern.finditer(text)
                        if unquote(match.group(), encoding="utf-8", errors="strict") == foreign.removeprefix("/")]
            deliveries = []
            if key_hits:
                reference = "/" + key_hits[0]["token"]
                delivered = self.http.get(reference)
                self.assertEqual((delivered.status_code, delivered.content), (200, b"MEDIA_SECRET_B"))
                deliveries.append(dict(reference_from_returned_key=reference, status=delivered.status_code,
                                       body_hex=delivered.content.hex(), foreign_bytes=True))
            counters = dict(ID=sum(serialized.count(identity) for identity in foreign_ids), PATH=len(key_hits),
                            URL=len(key_hits), JSON_KEY=len(key_hits), NAME=len(re.findall(r"SECRET_[A-Z_]*_B", serialized)),
                            METADATA=int("DATA_B" in serialized), CONTENT=int("MEDIA_SECRET_B" in serialized))
            A10_R2_JSON_KEY_OBSERVATIONS.append(dict(test=self.id(), variant=variant,
                original_unsafe_case=variant != "top_slash_parentheses_m65", platform=os.name,
                engine=self.engine.dialect.name, surface=surface, path=path, status=response.status_code,
                raw=raw, decision=decision.value, counters=counters, foreign_reference_returned=bool(key_hits),
                foreign_bytes_from_response=bool(deliveries), deliveries=deliveries, response=payload))
            with self.subTest(variant=variant, surface=surface):
                a10_r2_assert_no_foreign_media(self, payload, foreign)
                self.assertEqual(sum(counters.values()), 0, "A10-R2 final projection leaked tenant B data")
                self.assertNotIn("delimiter-foreign-widget", serialized)
                if surface.startswith(("draft:", "published:")):
                    self.assertIn("independent-owned-widget", serialized)
        for args in (("contents", "/api/contents/", "A_client_admin"),
                     ("campaign_preview", "/api/campaigns/" + a["campaign"].id + "/sequence-preview", "A_client_admin"),
                     ("admin_runtime", "/api/channels/" + a["channel"].id + "/runtime-config", "A_client_admin"),
                     ("player_runtime", "/api/player/runtime-config", "player_A")):
            visit(*args)
        good = dict(id="independent-owned-widget", widget_type="image", props_json={"sourceUrl": a["content"].file_path})
        bad = dict(id="delimiter-foreign-widget", widget_type="image", props_json={"sourceUrl": a["content"].file_path, **deepcopy(raw)})
        state = dict(widgets=[good, bad])
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="Delimiter QA revision", status="draft", is_current_draft=True,
                            editor_state_json=deepcopy(state))
        self.db.commit()
        for phase in ("draft", "published"):
            revision.status = phase
            revision.is_current_draft = phase == "draft"
            revision.is_current_published = phase == "published"
            self.db.commit()
            for suffix in ("revisions", "revisions/" + revision.id, "preview", "editor-state"):
                visit(phase + ":" + ("revision_detail" if suffix.startswith("revisions/") else suffix),
                      "/api/layouts/" + a["layout"].id + "/" + suffix, "A_operator")
            visit(phase + ":admin_runtime", "/api/channels/" + a["channel"].id + "/runtime-config", "A_client_admin")
            visit(phase + ":player_runtime", "/api/player/runtime-config", "player_A")
            self.db.expire_all()
            self.assertEqual(revision.editor_state_json, state, "A10-R2 renamed or repaired stored JSON keys")
        self.db.expire_all()
        self.assertEqual(a["content"].metadata_json, raw, "A10-R2 changed stored metadata keys")
        A10_R2_STORED_INVARIANTS.append(dict(test=self.id(), variant=variant, platform=os.name,
            engine=self.engine.dialect.name, stored_metadata_unchanged=True, stored_revision_unchanged=True))

    def test_foreign_relative_key_not_external_safe(self):
        from app.services import media_references as refs
        foreign = self.res["B"]["content"].file_path.removeprefix("/")
        decision = refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                    {foreign: "ordinary"}, context=refs.MediaReadContext.STRUCTURED)
        self.assertEqual(decision, refs.MediaReadDecision.FOREIGN,
                         "A10-R2 relative candidate was external-safe instead of ownership checked")

    def test_multiple_owned_and_foreign_quoted_candidates_fail_closed(self):
        from app.services import media_references as refs
        own = self.res["A"]["content"].file_path.removeprefix("/").replace("media/", "m%65dia/", 1)
        foreign = self.res["B"]["content"].file_path.removeprefix("/").replace("media/", "med%69a/", 1)
        raw = {'pair "' + own + '" "' + foreign + '"': "ordinary"}
        decision = refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                    raw, context=refs.MediaReadContext.STRUCTURED)
        self.assertEqual(decision, refs.MediaReadDecision.FOREIGN,
                         "A10-R2 multi-candidate key accepted a foreign reference")

    def test_owned_quoted_candidates_and_normal_keys_preserve_original_bytes(self):
        from copy import deepcopy
        from app.services import media_references as refs
        a = self.res["A"]
        own = a["content"].file_path.removeprefix("/").replace("media/", "m%65dia/", 1)
        raw = {"width": 1920, "height": 1080, "foo.bar": "ordinary", 'label "' + own + '"': "literal",
               "https://evil.example/media/x": "external", 'label "100% natural"': "ordinary"}
        self.assertEqual(refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                         raw, context=refs.MediaReadContext.STRUCTURED), refs.MediaReadDecision.SAFE_LOCAL_OWNED)
        a["content"].metadata_json = deepcopy(raw)
        self.db.commit()
        response = self.media_read("/api/contents/")
        row = next((row for row in response.json() if row["id"] == a["content"].id), None)
        self.assertIsNotNone(row, "A10-R2 legitimate metadata was omitted")
        self.assertEqual(json.dumps(row["metadata_json"], ensure_ascii=False, sort_keys=True),
                         json.dumps(raw, ensure_ascii=False, sort_keys=True), "A10-R2 rewrote an owned JSON key")
        self.db.expire_all()
        self.assertEqual(a["content"].metadata_json, raw)


    def test_owned_outer_uri_does_not_hide_embedded_foreign_key(self):
        from copy import deepcopy
        from app.services import media_references as refs
        foreign = self.res["B"]["content"].file_path.removeprefix("/").replace("media/", "m%65dia/", 1)
        client_id = self.tenants["A"].id
        for suffix in ("owned(label(" + foreign + "))", "owned.png " + foreign):
            with self.subTest(suffix=suffix):
                raw_uri = "/media/" + client_id + "/" + suffix
                classification, canonical = refs.canonical_media_reference(raw_uri)
                self.assertEqual(classification, "LOCAL")
                file_path = self.root / canonical.removeprefix("/media/")
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(b"A10_R2_OUTER_URI_OWNED_A")
                self.add(ContentItem, client_id=client_id, name="QA owned outer URI",
                         type=ContentType.IMAGE, file_path=canonical)
                self.db.commit()
                direct = refs.resolve_effective_media_reference(self.db, client_id, raw_uri,
                             context=refs.MediaReadContext.TYPED_URL)
                self.assertEqual(direct.decision, refs.MediaReadDecision.SAFE_LOCAL_OWNED,
                                 "A10-R2 outer URI control was not authoritatively A-owned")
                payload = {raw_uri: "ordinary"}
                before = deepcopy(payload)
                decision = refs.read_payload_media_decision(self.db, client_id, payload,
                               context=refs.MediaReadContext.STRUCTURED)
                self.assertEqual(decision, refs.MediaReadDecision.AMBIGUOUS,
                                 "A10-R2 owned outer URI concealed an independent foreign candidate")
                self.assertEqual(payload, before)


    def test_external_query_field_in_owned_uri_key_omits_metadata_content(self):
        from copy import deepcopy
        from app.services import media_references as refs
        a = self.res["A"]
        foreign_path = self.res["B"]["content"].file_path
        foreign = foreign_path.removeprefix("/").replace("media/", "m%65dia/", 1)
        owned_path = a["content"].file_path
        for separator in ("&", ";"):
            raw_uri = owned_path + "?redirect=https://evil.example/x" + separator + "next=" + foreign
            direct = refs.resolve_effective_media_reference(self.db, self.tenants["A"].id, raw_uri,
                         context=refs.MediaReadContext.TYPED_URL)
            self.assertEqual(direct.decision, refs.MediaReadDecision.SAFE_LOCAL_OWNED,
                             "A10-R2 typed owned URI control changed query semantics")
            for key in (raw_uri, 'label "' + raw_uri + '"'):
                with self.subTest(separator=separator, key=key):
                    raw = {key: "ordinary"}
                    before = deepcopy(raw)
                    decision = refs.read_payload_media_decision(self.db, self.tenants["A"].id,
                                   raw, context=refs.MediaReadContext.STRUCTURED)
                    self.assertEqual(decision, refs.MediaReadDecision.AMBIGUOUS,
                                     "A10-R2 external URI consumed a later foreign query-field candidate")
                    a["content"].metadata_json = deepcopy(raw)
                    self.db.commit()
                    response = self.media_read("/api/contents/")
                    self.assertNotIn(a["content"].id, {item["id"] for item in response.json()},
                                     "A10-R2 unsafe metadata key preserved its content row")
                    a10_r2_assert_no_foreign_media(self, response.json(), foreign_path)
                    self.assertNotIn(self.tenants["B"].id, response.text)
                    self.db.expire_all()
                    self.assertEqual(a["content"].metadata_json, before,
                                     "A10-R2 changed stored query/fragment key")
                    self.assertEqual(a["content"].file_path, owned_path)


for _a10r2_variant in A10_R2_AUDIT_VARIANTS:
    def _a10r2_case(self, variant=_a10r2_variant):
        self.exercise_audit_variant(variant)
    setattr(A10R2EmbeddedMediaProjectionTests, "test_audit_" + _a10r2_variant, _a10r2_case)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL A10-R2")
class A10R2EmbeddedMediaProjectionPostgresTests(LocalPostgresFixture, A10R2EmbeddedMediaProjectionTests):
    pass
