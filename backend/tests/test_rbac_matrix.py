"""All eight real roles, 503 HTTP cases and 37 WebSocket cases."""
from test_tenant_isolation import TenantFixture
from app.models.entities import *
from app.api.routes import realtime
from datetime import datetime, UTC


class RbacMatrixTests(TenantFixture):
    def test_http_matrix(self):
        db, http, app, add = self.db, self.http, self.app, self.add
        tenants, res, users = self.tenants, self.res, self.users
        tokens, ptokens = self.tokens, self.ptokens
        A, B = res["A"], res["B"]
        ca, cb = tenants["A"].id, tenants["B"].id
        results = []
        def req(label, method, path, *, expected=None, **kw):
            response = self.request(method, path, **kw)
            results.append(dict(label=label, safe=expected is None or response.status_code in expected))
            return response
        
        from fastapi.routing import APIRoute
        from enum import Enum
        from typing import get_origin,get_args,Union
        import types,re
        for side in ["A","B"]:
            r=res[side]
            r["revision"]=add(LayoutRevision,layout_id=r["layout"].id,revision_number=1,name="Rev "+side,status="draft")
            r["node"]=add(VideowallNode,videowall_id=r["wall"].id,channel_id=r["channel"].id,position_index=1,row_index=0,column_index=0,x=0,y=0,width=1920,height=1080)
            r["button"]=add(KioskButton,screen_id=r["screen"].id,label="Button "+side,x=0,y=0,width=50,height=50,action_type=list(KioskActionType)[0])
            r["map"]=add(TouchMap,experience_id=r["experience"].id,name="Map "+side)
            r["touch_assignment"]=add(TouchExperienceAssignment,client_id=tenants[side].id,experience_id=r["experience"].id,branch_id=r["branch"].id,channel_id=r["channel"].id)
        db.commit()
        ids={k+"_id":v.id for k,v in B.items()}
        ids.update(client_id=cb,user_id=users["B_client_operator"].id,videowall_id=B["wall"].id,content_id=B["content"].id,folder_id=B["folder"].id,playlist_id=B["playlist"].id,item_id=B["audioitem"].id,playlist_item_id=B["legacy"].id,sequence_item_id=B["sequence"].id,revision_id=B["revision"].id,touch_map_id=B["map"].id,map_id=B["map"].id,assignment_id=B["touch_assignment"].id)
        # Foreign-wall scope must reject before publication lookup.
        ids["publication_id"] = "11111111-1111-4111-8111-111111111111"
        def field_value(name,annotation):
            if name in ids:return ids[name]
            if name=="data_source_id":return B["source"].id
            if name=="password":return "Synthetic-Audit-Password!42"
            if name=="email":return "new-audit@example.com"
            if name=="confirm_text":return "ELIMINAR"
            if name=="playback_mode":return "sequential"
            if name=="title":return "Audit schedule"
            if name=="slug":return "audit-new"
            origin=get_origin(annotation)
            if origin is not None:
                args=get_args(annotation)
                if origin in (Union,types.UnionType):return field_value(name,next(a for a in args if a is not type(None)))
                if str(origin).endswith("Literal"):return args[0]
                if origin is list:return []
                if origin is dict:return {}
            if isinstance(annotation,type) and issubclass(annotation,Enum):return list(annotation)[0].value
            if annotation is int:return 1
            if annotation is bool:return True
            if annotation is datetime:return datetime.now(UTC).isoformat()
            return "audit"
        for who in ["A_client_admin","A_operator"]:
            for route in app.routes:
                if not isinstance(route,APIRoute):continue
                path=route.path
                if path.startswith(("/api/auth/","/api/player/","/media/")):continue
                method=next(iter(route.methods))
                has_id="{" in path
                if method=="GET" and not has_id:continue
                path=re.sub(r"\{(\w+)\}",lambda m:ids[m.group(1)],path)
                body={}
                kw={}
                if route.dependant.body_params:
                    if any(p.name=="file" for p in route.dependant.body_params):
                        kw["files"]={"file":("audit.csv",b"value\nsynthetic\n","text/csv")}
                        kw["data"]={"name":"audit","client_id":cb}
                    else:
                        p=route.dependant.body_params[0]
                        for name,f in p.type_.model_fields.items():
                            if f.is_required():body[name]=field_value(name,f.annotation)
                        if "client_id" in p.type_.model_fields:body["client_id"]=cb
                        if route.name=="create_user":body["role"]="client_admin"
                        if route.name=="upsert_audio_assignment":body["channel_id"]=B["channel"].id
                        if route.name=="create_audio_playback_event":body["channel_id"]=B["channel"].id
                        kw["body"]=body
                req(who+" foreign "+method+" "+route.path,method,path,who=who,expected=[403,404],**kw)
        # All read collections against two real tenants for all eight roles.
        for role in UserRole:
            who="A_"+role.value;global_role=role in {UserRole.SUPER_ADMIN,UserRole.STAFF_ADMIN,UserRole.STAFF_OPERATOR}
            for route in app.routes:
                if not isinstance(route,APIRoute) or "GET" not in route.methods or "{" in route.path:continue
                if route.path.startswith(("/api/auth","/api/player","/media")):continue
                params={}
                if route.name=="presence":params={"client_id":ca}
                if route.name=="get_audio_runtime_config":params={"channel_id":A["channel"].id}
                response=req(who+" list "+route.path,"GET",route.path,who=who,params=params)
                records=results[-1]
                records["safe"]=response.status_code in [200,403] and (global_role or (cb not in response.text and "SECRET_" not in response.text.replace("SECRET_CONTENT_A","").replace("SECRET_LAYOUT_A","").replace("SECRET_CAMPAIGN_A","").replace("SECRET_AUDIO_A","").replace("SECRET_PLAYLIST_A","")))
        
        # Explicit parent context must stay coherent even for global roles.
        from test_tenant_isolation import ScopedReadIntegrityTests, R4_ROLES
        for kind in ("contents", "schedules", "channels", "audio_items", "audio_report",
                     "audio_assignments", "touch_assignments", "layout_bindings"):
            path, own_id, bad_id = ScopedReadIntegrityTests.seed_case(self, kind)
            for who in R4_ROLES:
                response = req(f"{who} context {kind}", "GET", path, who=who, expected=[200])
                results[-1]["safe"] &= own_id in response.text and bad_id not in response.text
                results[-1]["safe"] &= all(row.id not in response.text for row in B.values())
                results[-1]["safe"] &= "FOREIGN_REFERENCE_FIXTURE" not in response.text
        # Global access must preserve valid B rows, not the corrupted fixtures
        # above that A-R5 now excludes independently of the requesting role.
        B["content"].folder_id = B["folder"].id
        B["schedule"].channel_id = B["channel"].id
        B["channel"].branch_id = B["branch"].id
        db.commit()
        for who in R4_ROLES[1:]:
            for route, key in (("contents", "content"), ("schedules", "schedule"), ("channels", "channel")):
                response = req(f"{who} global {route}", "GET", f"/api/{route}/", who=who, expected=[200])
                results[-1]["safe"] &= A[key].id in response.text and B[key].id in response.text
        from test_tenant_isolation import ReadSideIntegrityTests, R5_READ_CASES
        for kind in R5_READ_CASES:
            fixture = ReadSideIntegrityTests()
            fixture.setUp()
            try:
                case = fixture.seed_read_case(kind)
                for who in R4_ROLES:
                    response = fixture.observe_unchanged(lambda: fixture.request("GET", case["path"], who=who))
                    fixture.assert_no_foreign_response(response)
                    rows = {row["id"]: row for row in response.json()}
                    safe = response.status_code == 200 and case["good"].id in rows
                    if case["strategy"] == "ROW_OMITTED":
                        safe &= case["bad"].id not in rows
                    else:
                        safe &= rows.get(case["bad"].id, {}).get(case["field"], "not-null") is None
                    results.append(dict(label=f"{who} cross direction {kind}", safe=safe))
            finally:
                fixture.doCleanups()
        self.assertEqual(len(results), 509)
        self.assertEqual([r for r in results if not r["safe"]], [])

    def test_websocket_matrix(self):
        db, http, app, add = self.db, self.http, self.app, self.add
        tenants, res, users = self.tenants, self.res, self.users
        tokens, ptokens = self.tokens, self.ptokens
        A, B = res["A"], res["B"]
        ca, cb = tenants["A"].id, tenants["B"].id
        results = []
        def req(label, method, path, *, expected=None, **kw):
            response = self.request(method, path, **kw)
            results.append(dict(label=label, safe=expected is None or response.status_code in expected))
            return response
        
        from starlette.websockets import WebSocketDisconnect
        http.__enter__()
        for role in UserRole:
            who="A_"+role.value
            glob=role in {UserRole.SUPER_ADMIN,UserRole.STAFF_ADMIN,UserRole.STAFF_OPERATOR}
            branchrole=role in {UserRole.BRANCH_MANAGER,UserRole.OPERATOR}
            for kind in ["clients","channels"]:
                for side in ["A","B"]:
                    target=tenants[side].id if kind=="clients" else res[side]["channel"].id
                    allowed=glob or (side=="A" and (not branchrole or kind=="channels"))
                    got=False;close=None
                    try:
                        with http.websocket_connect(f"/api/realtime/ws/{kind}/{target}?token={tokens[who]}"):got=True
                    except WebSocketDisconnect as e:close=e.code
                    results.append(dict(label=f"WS {who} {kind} {side}",safe=got==allowed,connected=got,close=close))
        for kind in ["clients","channels"]:
            ta=ca if kind=="clients" else A["channel"].id
            tb=cb if kind=="clients" else B["channel"].id
            broadcast=realtime.manager.broadcast_to_client if kind=="clients" else realtime.manager.broadcast_to_channel
            with http.websocket_connect(f"/api/realtime/ws/{kind}/{ta}?token={tokens['A_client_admin']}") as wa:
                with http.websocket_connect(f"/api/realtime/ws/{kind}/{tb}?token={tokens['B_client_admin']}") as wb:
                    http.portal.call(broadcast,tb,{"marker":"B"})
                    http.portal.call(broadcast,ta,{"marker":"A"})
                    # Ordered sends followed by A sentinel prove no B data was queued for A.
                    results.append(dict(label="WS broadcast separation "+kind,safe=wa.receive_json()=={"marker":"A"} and wb.receive_json()=={"marker":"B"}))
                    if kind=="channels":
                        wa.send_json({"type":"heartbeat","channel_id":tb,"client_id":cb,"marker":"forged"})
                        same=wa.receive_json()
                        http.portal.call(broadcast,tb,{"marker":"B sentinel"})
                        # A9 rebuilds the payload from the authorized room. The
                        # forged marker/tenant fields must not be reflected.
                        results.append(dict(label="WS forged body cannot switch room",safe=(
                            same.get("type") == "heartbeat" and same.get("channel_id") == ta
                            and same.get("current_playback") is None and isinstance(same.get("timestamp"), str)
                            and "marker" not in same and "client_id" not in same
                            and wb.receive_json() == {"marker":"B sentinel"})))
                    users["A_client_admin"].is_active=False;db.commit()
                    http.portal.call(broadcast,ta,{"marker":"revoked"})
                    denied=False
                    try:wa.receive_json()
                    except WebSocketDisconnect as e:denied=e.code==4403
                    results.append(dict(label="WS revoke before broadcast "+kind,safe=denied))
            users["A_client_admin"].is_active=True;db.commit()
        
        self.assertEqual(len(results), 37)
        self.assertEqual([r for r in results if not r["safe"]], [])
        http.__exit__(None, None, None)


def run_phase_a_mutations(*, only=None):
    """Reintroduce each defect in memory; never edit source files for mutations."""
    import inspect
    import io
    import unittest
    from contextlib import ExitStack
    from unittest.mock import patch
    from sqlalchemy import select
    from app.api.routes import campaigns, kiosk, audio, schedules, player, layouts, touch, channels, contents, data_sources
    from app.services import tenancy, content_cleanup, media_references, player_runtime, layout_editor
    from app.models.entities import ContentItem
    from app.services.media_storage import resolve_stored_media_path
    relations="test_tenant_isolation.TenantRelationsTests."
    runtime="test_runtime_tenant_safety.RuntimeTenantTests."
    r1="test_runtime_tenant_safety.PhaseAR1RuntimeTests."
    media="test_media_ownership.PhaseAR1MediaTests."
    historical="test_tenant_isolation.HistoricalRelationshipTests."
    historical_media="test_media_ownership.HistoricalContentDeleteTests."
    cases=[
        ("campaign_layout",campaigns.create_campaign,"    require_same_client_resource(db, Layout, payload.layout_id, client_id)\n","",relations+"test_foreign_relationship_writes_rejected_for_all_writer_roles"),
        ("kiosk_experience",kiosk.create_screen,"    require_same_client_resource(db, TouchExperience, payload.experience_id, client_id)\n","",relations+"test_foreign_relationship_writes_rejected_for_all_writer_roles"),
        ("audio_resource",audio.create_audio_playback_event,"    require_same_client_resource(db, ContentItem, payload.content_id, target_client_id)\n","",relations+"test_foreign_relationship_writes_rejected_for_all_writer_roles"),
        ("schedule_branch",schedules.create_schedule,"    require_same_client_resource(db, Branch, payload.branch_id, client_id)\n","",relations+"test_foreign_relationship_writes_rejected_for_all_writer_roles"),
        ("orphan_identity",tenancy.validate_authenticated_tenant_state,"if not user.client_id or db.get(Client, user.client_id) is None:","if False:",relations+"test_orphan_and_corrupt_branch_identity_denied_before_reads"),
        ("runtime_graph",tenancy.safe_tenant_resource,"resource.client_id != client_id or ","",runtime+"test_runtime_corrupt_campaign_layout_content_and_legacy_no_repair"),
        ("touch_target",tenancy.require_screen_target,"    target = require_same_client_resource","    return None\n    target = require_same_client_resource",relations+"test_touch_and_legacy_targets_same_tenant_and_experience"),
        ("layout_get_write",layouts.get_layout_editor_state,"persist=False","persist=True",relations+"test_operator_layout_gets_never_persist"),
        ("telemetry",player._sanitize_telemetry,"    updates = {}","    return payload\n    updates = {}",runtime+"test_player_invalid_telemetry_sanitizes_only_bad_fields"),
        ("runtime_file",player_runtime._runtime_contents," and content_file_owned(db, content)","",r1+"test_foreign_file_omitted_without_repair"),
        ("revision_preview",layouts.list_layout_revisions,"return [safe_revision_payload(revision, layout, db) for revision in get_layout_revisions(db, layout)]","return get_layout_revisions(db, layout)",r1+"test_all_revision_reads_sanitize_historical_preview"),
        ("inactive_output",player_runtime._runtime_output_allowed,"    channel = safe_tenant_resource","    return True\n    channel = safe_tenant_resource",r1+"test_inactive_secondary_branch_output_omitted"),
        ("get_sequence_write",campaigns._read_sequence_items,"    sequence_items = get_ordered_sequence_items(campaign_id, db)","    sequence_items = resolve_effective_sequence_items(campaign_id, db)\n    db.commit()",r1+"test_all_gets_are_read_only_for_all_roles"),
    ]
    cases.extend([
        ("touch_assignment_list", touch.list_touch_assignments,
         ",\n                   TouchExperienceAssignment.client_id == experience.client_id", "",
         historical+"test_touch_list_omits_foreign_assignment"),
        ("touch_assignment_patch", touch.ensure_experience_assignment,
         "    assert_related_clients(experience.client_id, [assignment.client_id])\n", "",
         historical+"test_touch_patch_rejects_foreign_assignment"),
        ("touch_assignment_delete", touch.ensure_experience_assignment,
         "    assert_related_clients(experience.client_id, [assignment.client_id])\n", "",
         historical+"test_touch_delete_rejects_foreign_assignment"),
        ("audio_assignment_upsert", audio.upsert_audio_assignment,
         "        assert_related_clients(target_client_id, [item.client_id for item in candidates])\n", "",
         historical+"test_audio_upsert_rejects_foreign_assignment"),
        ("experience_foreign_screen", touch.delete_touch_experience,
         "    preflight_experience_delete(experience, db)\n", "",
         historical+"test_experience_delete_preserves_foreign_screen"),
        ("channel_foreign_audio", channels.cleanup_channel_delete_dependencies,
         "    preflight_channel_delete(db, channel)\n", "",
         historical+"test_channel_delete_preserves_foreign_audio"),
        ("content_foreign_sequence", content_cleanup.preflight_content_relationships,
         "                (CampaignSequenceItem, Campaign, CampaignSequenceItem.campaign_id),\n", "",
         historical_media+"test_foreign_sequence_prevents_content_delete"),
        ("content_foreign_legacy", content_cleanup.preflight_content_relationships,
         "                (CampaignPlaylistItem, Campaign, CampaignPlaylistItem.campaign_id),\n", "",
         historical_media+"test_foreign_legacy_prevents_content_delete"),
        ("content_foreign_audio", content_cleanup.preflight_content_relationships,
         "                (AudioPlaylistItem, AudioPlaylist, AudioPlaylistItem.playlist_id),\n", "",
         historical_media+"test_foreign_audio_item_prevents_content_delete"),
        ("suspended_client", tenancy.require_active_channel_tenant,
         'client.status != "active" or ', "",
         runtime+"test_suspension_and_inactive_branch_block_players_without_token_changes"),
        ("ambiguous_owner", media_references.media_owned_by,
         "return owners == {client_id}", "return client_id in owners",
         "test_media_ownership.MediaOwnershipTests.test_reference_ownership_same_foreign_dataset_and_unknown"),
        ("request_upload_provenance", media_references.save_content_reference,
         "                require_media_owner(db, root, new, content.client_id)", "                pass",
         media+"test_untrusted_references_cannot_grant_ownership"),
        ("duplicate_widget_id", layout_editor.safe_revision_payload,
         '    payload["editor_state_json"] = editor',
         '    allowed_ids = {widget["id"] for widget in preview["widgets"]}\n'
         '    editor = normalize_editor_state(revision.editor_state_json, layout)\n'
         '    editor["widgets"] = [widget for widget in editor["widgets"] if widget["id"] in allowed_ids]\n'
         '    payload["editor_state_json"] = editor',
         "test_runtime_tenant_safety.PhaseAR2WidgetTests"),
    ])
    alternate = "test_tenant_isolation.AlternateMutationTests."
    scoped = "test_tenant_isolation.ScopedReadIntegrityTests."
    cases.extend([
        ("screen_incoming_preflight", touch.delete_touch_screen,
         "    preflight_screen_references(db, experience.client_id, [screen.id])\n", "",
         alternate+"test_touch_screen_incoming_references"),
        ("campaign_schedule_preflight", campaigns.preflight_campaign_delete,
         "        assert_related_clients(campaign.client_id, db.scalars(\n"
         "            select(Schedule.client_id).where(Schedule.campaign_id == campaign.id)))\n", "",
         alternate+"test_campaign_delete_foreign_schedule"),
        ("channel_replace_preflight", channels.replace_channel_campaign_assignment,
         "    preflight_channel_campaign_assignments(db, channel)\n", "",
         alternate+"test_channel_campaign_replace_foreign_assignment"),
        ("folder_context", contents.list_contents, ", ContentItem.client_id == folder.client_id", "",
         scoped+"test_contents_folder_context"),
        ("schedule_context", schedules.list_schedules, ", Schedule.client_id == channel.client_id", "",
         scoped+"test_schedules_channel_context"),
        ("branch_context", channels.list_channels, ", Channel.client_id == branch.client_id", "",
         scoped+"test_channels_branch_context"),
        ("audio_item_fk", audio.list_audio_playlist_items, " if contents.get(item.content_id)", "",
         scoped+"test_audio_items_foreign_content"),
        ("audio_report_fk", audio.get_audio_report_summary, "    query = query.where(audio_event_integrity())\n", "",
         scoped+"test_audio_report_foreign_resources"),
        ("audio_assignment_fk", audio.get_assignment_query_for_user, "    query = query.where(audio_assignment_integrity())\n", "",
         scoped+"test_audio_assignments_foreign_playlist"),
        ("touch_assignment_fk", touch.list_touch_assignments,
         ",\n                   related_client_matches(Branch, TouchExperienceAssignment.branch_id, experience.client_id, optional=True)"
         ",\n                   related_client_matches(Channel, TouchExperienceAssignment.channel_id, experience.client_id, optional=True)", "",
         scoped+"test_touch_assignments_foreign_targets"),
        ("layout_binding_fk", layouts.list_layout_bindings,
         ",\n                   related_client_matches(Dataset, LayoutDataBinding.dataset_id, layout.client_id)", "",
         scoped+"test_layout_bindings_foreign_dataset"),
        ("videowall_generic_error", channels.delete_channel, "    preflight_channel_videowall(db, channel)\n", "",
         alternate+"test_videowall_conflict_is_generic"),
        ("schedule_campaign_context", schedules.list_schedules, ", Schedule.client_id == campaign.client_id", "",
         scoped+"test_schedule_campaign_context"),
        ("campaign_sequence_read_fk", campaigns.get_sequence, " if campaign_item_read_allowed(item, campaign, db)", "",
         scoped+"test_campaign_scoped_item_reads"),
        ("touch_button_read_fk", touch.list_touch_buttons,
         ", or_(\n                KioskButton.target_screen_id.is_(None),\n"
         "                select(KioskScreen.id).where(KioskScreen.id == KioskButton.target_screen_id,\n"
         "                    KioskScreen.client_id == experience.client_id,\n"
         "                    KioskScreen.experience_id == experience.id).exists())", "",
         scoped+"test_touch_button_scoped_read"),
    ])
    r5 = "test_tenant_isolation.ReadSideIntegrityTests."
    cases.extend([
        ("r5_kiosk_target", kiosk.safe_button_read, "    target_id = optional_relation_id",
         "    return KioskButtonRead.model_validate(button)\n    target_id = optional_relation_id", r5+"test_kiosk_target"),
        ("r5_dataset_source", data_sources.list_datasets,
         "related_client_matches(DataSource, Dataset.data_source_id, Dataset.client_id)",
         "Dataset.id.is_not(None)", r5+"test_dataset_source"),
        ("r5_dataset_import", data_sources.safe_dataset_read, "return safe_model_read(dataset, DatasetRead, db)",
         "return DatasetRead.model_validate(dataset)", r5+"test_dataset_import"),
        ("r5_campaign_layout", campaigns.safe_campaign_read, "    layout_id = optional_relation_id",
         "    return CampaignRead.model_validate(campaign)\n    layout_id = optional_relation_id", r5+"test_campaign_layout"),
        ("r5_schedule_relations", tenancy.schedule_read_integrity, "    return and_(",
         "    return Schedule.id.is_not(None)\n    return and_(", r5+"test_schedule_campaign"),
        ("r5_content_folder", contents.safe_content_read, "    folder_id = optional_relation_id",
         "    return ContentRead.model_validate(content)\n    folder_id = optional_relation_id", r5+"test_content_folder"),
        ("r5_content_file", contents.list_contents, "ContentItem.id.in_(readable_content_ids(db))",
         "ContentItem.id.is_not(None)", r5+"test_content_file"),
        ("r5_channel_branch", tenancy.channel_read_integrity,
         "return related_client_matches(Branch, Channel.branch_id, Channel.client_id, optional=True)",
         "return Channel.id.is_not(None)", r5+"test_channel_branch"),
        ("r5_experience_home", touch.safe_experience_read, "    return TouchExperienceRead.model_validate",
         '    updates["home_screen_id"] = experience.home_screen_id\n    return TouchExperienceRead.model_validate', r5+"test_experience_home"),
        ("r5_experience_attract", touch.safe_experience_read, "    return TouchExperienceRead.model_validate",
         '    updates["attract_screen_id"] = experience.attract_screen_id\n    return TouchExperienceRead.model_validate', r5+"test_experience_attract"),
        ("r5_branch_grant", tenancy.branch_layout_ids_query, "    campaign_layouts = (",
         "    return (select(Campaign.layout_id).join(CampaignChannelAssignment, CampaignChannelAssignment.campaign_id == Campaign.id)"
         ".join(Channel, Channel.id == CampaignChannelAssignment.channel_id).where(Channel.branch_id == branch_id))\n    campaign_layouts = (",
         r5+"test_branch_transitive_foreign_grant"),
        ("r5_prevalidation", touch.update_touch_experience,
         "    preflight_experience_update(experience, changes, db)",
         '    experience.name = changes.get("name", experience.name)\n    preflight_experience_update(experience, changes, db)',
         r5+"test_experience_patch_preflight"),
        ("r5_runtime_audio", player_runtime._audio_assignment_allowed, "    if assignment.client_id",
         "    return True\n    if assignment.client_id",
         "test_runtime_tenant_safety.PhaseAR5AudioTests.test_corrupt_channel_assignment_falls_back_on_actual_player_endpoint"),
        ("r5_folder_code", content_cleanup.delete_folder_tree,
         'raise MediaPathError("TENANT_RELATION_CONFLICT")', 'raise MediaPathError("FOLDER_FOREIGN_REFERENCE")',
         r5+"test_folder_conflict_code"),
    ])
    results=[]
    from test_tenant_relation_matrix import _run_delete_mutation, _legacy_defense_sites
    def run(name, test, fn, replacement, companions=(), binding=None):
        if only is not None and name != only:
            return
        if test.endswith("PhaseAR2WidgetTests"):
            test += ".test_mixed_duplicate_ids_in_both_orders"
        suite = unittest.defaultTestLoader.loadTestsFromName(test)
        def leaves(items):
            for item in items:
                if isinstance(item, unittest.TestSuite):
                    yield from leaves(item)
                else:
                    yield item
        selected = list(leaves(suite))
        assert len(selected) == 1, "Legacy mutation must name its exact defense test"
        probe = selected[0]
        target, attribute = binding if binding is not None else (fn, "__code__")
        result = _run_delete_mutation(name, target, attribute, replacement,
            type(probe), probe._testMethodName, _legacy_defense_sites("historical", name),
            code_target=binding is None, extra_mutant_patches=companions)
        result["name"] = name
        results.append(result)
    for name,fn,old,new,test in cases:
        fn = inspect.unwrap(fn)
        src=inspect.getsource(fn);src=src[src.index("def "):]
        assert old in src,name
        src = src.replace(old, new)
        # A-R5 independently rejects these same corrupt rows. Reintroducing the
        # original context leak now requires bypassing both SQL defenses.
        if name in {"schedule_context", "schedule_campaign_context"}:
            assert ".where(schedule_read_integrity())" in src
            src = src.replace(".where(schedule_read_integrity())", "")
        if name == "branch_context":
            assert ".where(channel_read_integrity())" in src
            src = src.replace(".where(channel_read_integrity())", "")
        namespace={};exec(compile(src,"<phase-a-mutation>","exec"),fn.__globals__,namespace)
        run(name, test, fn, namespace[fn.__name__])
    def old_refs(db):
        return {resolve_stored_media_path(content_cleanup.settings.MEDIA_ROOT,p,must_exist=False)
                for p in db.scalars(select(ContentItem.file_path).where(ContentItem.file_path.is_not(None)))}
    def accept_owner(*args, **kwargs):
        return True
    run("dataset_delete", "test_media_ownership.MediaOwnershipTests.test_corrupt_content_delete_preserves_dataset_file",
        content_cleanup.media_owned_by, accept_owner,
        ((content_cleanup, "_referenced_media_paths", old_refs),), binding=(content_cleanup, "media_owned_by"))
    real=media_references.media_reference_owners
    def url_owners(db,root,**kw):
        refs=real(db,root,**kw)
        for owner,url in db.execute(select(ContentItem.client_id,ContentItem.source_url)):
            if url and url.startswith("/media/"):
                refs.setdefault(resolve_stored_media_path(root,url,must_exist=False),set()).add(owner)
        return refs
    run("source_url_ownership", media+"test_untrusted_references_cannot_grant_ownership",
        media_references.media_reference_owners, url_owners, binding=(media_references, "media_reference_owners"))
    def poison_owners(db,root,**kw):
        refs=real(db,root,**kw)
        for value in db.scalars(select(ContentItem.metadata_json)):
            for text in media_references._strings(value):
                local=media_references._local_url(text)
                if local:resolve_stored_media_path(root,local,must_exist=False)
        return refs
    run("metadata_dos", media+"test_invalid_metadata_does_not_block_other_tenant",
        media_references.media_reference_owners, poison_owners, binding=(media_references, "media_reference_owners"))
    fn=content_cleanup.delete_folder_tree;src=inspect.getsource(fn)
    start=src.index("    # Deleting the parent")
    end=src.index("    contents = collect_folder_contents",start)
    namespace={};exec(compile(src[:start]+src[end:],"<folder-mutation>","exec"),fn.__globals__,namespace)
    def foreign_children(folder_ids,db,**kw):
        return list(db.scalars(select(ContentItem).where(ContentItem.folder_id.in_(folder_ids))))
    run("folder_foreign_delete", media+"test_folder_foreign_child_survives_unchanged",
        fn, namespace[fn.__name__], ((content_cleanup, "collect_folder_contents", foreign_children),))
    return results


def run_a9_read_mutations():
    """A7-R3 controls/provenance and exact security assertions for A9 defects."""
    import inspect
    from app.services import tenant_responses as responses, tenant_queries as queries
    from app.services import media_references as media_refs, layout_editor, player_runtime
    from app.api.routes import videowalls, audio
    from test_tenant_isolation import A9ReadHTTPTests
    from test_runtime_tenant_safety import A9ReadSurfaceTests, PhaseAR1RuntimeTests, PhaseAR2WidgetTests
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    catalog = []

    def altered(function, edits):
        source = inspect.getsource(function)
        for old, new in edits:
            if source.count(old) != 1:
                raise AssertionError("A9 mutation edit not unique: " + old)
            source = source.replace(old, new)
        namespace = {}
        exec(compile(source, "<A9 declared production mutation>", "exec"), function.__globals__, namespace)
        return namespace[function.__name__]

    def add(name, module, attribute, edits, cls, method, anchor, defense=None):
        original = getattr(module, attribute)
        replacement = altered(original, edits)
        sites = _assertion_sites(defense or getattr(cls, method), anchor)
        catalog.append((name, module, attribute, replacement, cls, method, sites))

    add("serializer_foreign_fk", responses, "safe_model_read",
        [('changes = {edge.field: None for edge in invalid}', 'changes = {}')],
        A9ReadHTTPTests, "test_user_read_neutralizes_foreign_branch_without_repair",
        "self.assertIsNone(row['branch_id'])")

    add("projection_repairs_orm", responses, "safe_model_read",
        [('return schema.model_validate(row).model_copy(deep=True, update=changes)',
          'for key, value in changes.items():\n        setattr(row, key, value)\n    return schema.model_validate(row).model_copy(deep=True, update=changes)')],
        A9ReadSurfaceTests, "test_optional_user_projection_never_repairs_persistent_row",
        'self.assertEqual(user.branch_id, self.res["B"]["branch"].id, "A9 serializer repaired ORM")')

    add("filter_after_page", queries, "readable_query",
        [('name = model if isinstance(model, str) else model.__name__',
          'name = model if isinstance(model, str) else model.__name__\n    if name == "Channel":\n        return statement')],
        A9ReadSurfaceTests, "test_sql_filters_corrupt_rows_before_small_page",
        'self.assertEqual([r.id for r in page], [good[0].id], "A9 pagination included corrupt row")')

    add("lazy_field_media_bypass", media_refs, "read_payload_media_owned",
        [('return read_payload_media_decision(', 'return True\n    return read_payload_media_decision(')],
        A9ReadSurfaceTests, "test_dataset_binding_lazy_fields_with_foreign_media_are_omitted",
        'self.assertNotIn(b["content"].file_path, response.text, "A9 lazy field leaked foreign media")')

    add("preview_widget_bypass", layout_editor, "_widget_is_tenant_safe",
        [('if isinstance(value, dict):', 'return True\n    if isinstance(value, dict):')],
        PhaseAR1RuntimeTests, "test_revision_embedded_media_and_binding_ids_are_filtered",
        'self.assertNotIn(b["content"].file_path,r.text);self.assertNotIn(b["dataset"].id,r.text)')

    add("report_count_contamination", audio, "readable_query",
        [('name = model if isinstance(model, str) else model.__name__',
          'name = model if isinstance(model, str) else model.__name__\n    if name == "AudioPlaybackEvent":\n        return statement.where(read_integrity_predicate(model))')],
        A9ReadSurfaceTests, "test_audio_foreign_media_event_does_not_increase_report_count",
        'self.assertEqual(response.json()["music"], [], "A9 unsafe event contaminated count")')

    add("runtime_foreign_content", player_runtime, "_runtime_contents",
        [('eligible = {content.id: content for content in db.scalars(statement.where(ContentItem.id.in_(sorted(batch))))\n'
          '                    if content.client_id == client_id and content_file_owned(db, content)}',
          'eligible = {identity: content for identity in batch\n'
          '                    if (content := db.get(ContentItem, identity)) is not None and content_file_owned(db, content)}')],
        A9ReadSurfaceTests, "test_runtime_foreign_sequence_content_is_not_serialized",
        'self.assertNotIn(b["content"].id, response.text, "A9 runtime accepted foreign item")')

    add("corrupt_assignment_priority", audio, "readable_query",
        [('name = model if isinstance(model, str) else model.__name__',
          'name = model if isinstance(model, str) else model.__name__\n    if name == "AudioAssignment":\n        return statement')],
        A9ReadSurfaceTests, "test_admin_and_player_audio_skip_inactive_channel_priority",
        'self.assertEqual(response.json()["assignment_scope"], "branch", "A9 corrupt priority won admin runtime")')

    add("alternate_videowall_preview", videowalls, "safe_model_read",
        [('changes = {edge.field: None for edge in invalid}', 'changes = {}')],
        A9ReadSurfaceTests, "test_videowall_preview_neutralizes_foreign_primary_for_global_actor",
        'self.assertIsNone(response.json()["videowall"]["primary_channel_id"], "A9 foreign wall primary")')

    add("duplicate_widget_trust_by_id", layout_editor, "_build_safe_layout_states",
        [('for widget in normalized["widgets"]:',
          'trusted_ids = {w["id"] for w in normalized["widgets"] if _widget_is_tenant_safe(w, layout, db)}\n    for widget in normalized["widgets"]:'),
         ('if not _widget_is_tenant_safe(widget, layout, db, references=references):', 'if widget["id"] not in trusted_ids:')],
        PhaseAR2WidgetTests, "test_mixed_duplicate_ids_in_both_orders",
        'self.assertNotIn(value, response.text)', defense=PhaseAR2WidgetTests._assert_safe_reads)

    add("global_parent_context_bypass", queries, "read_integrity_predicate",
        [('conditions.append(owner == client_id)', 'pass  # mutant removes parent ownership constraint')],
        A9ReadSurfaceTests, "test_global_parent_context_still_filters_foreign_owner",
        'self.assertEqual([row.id for row in self.db.scalars(query)], [self.res["A"]["channel"].id],')

    add("foreign_media_url_generated", media_refs, "readable_media_ids",
        [('return [row[0] for row in rows if _readable_media_row(db, model_name, row, root, references)]',
          'return list(db.scalars(select(ContentItem.id))) if model_name == "ContentItem" else '
          '[row[0] for row in rows if _readable_media_row(db, model_name, row, root, references)]')],
        A9ReadHTTPTests, "test_audio_library_omits_foreign_authoritative_media_before_serialization",
        "self.assertNotIn(b['content'].file_path, response.text)")

    add("lazy_channel_output_bypass", responses, "safe_model_read",
        [('return schema.model_validate(copied).model_copy(deep=True, update=changes)',
          'copied["device_outputs"] = [DeviceOutputRead.model_validate(output) for output in row.device_outputs]\n            return schema.model_validate(copied).model_copy(deep=True, update=changes)')],
        A9ReadSurfaceTests, "test_channel_read_filters_lazy_device_outputs_before_serialization",
        'self.assertNotIn(b["channel"].id, response.text, "A9 lazy output exposed foreign channel")')

    from app.services import layout_bindings
    add("alternate_editor_binding_options", layout_bindings, "build_layout_data_preview",
        [('readable_query(select(LayoutDataBinding), LayoutDataBinding,\n                                                 db=db, client_id=layout.client_id)',
          'select(LayoutDataBinding)')],
        A9ReadSurfaceTests, "test_editor_binding_options_use_same_media_guard_as_preview",
        'self.assertEqual(direct["bindings"], [], "A9 direct preview accepted foreign binding options")')

    add("import_summary_media_bypass", media_refs, "readable_media_ids",
        [('DatasetImport.source_filename, DatasetImport.summary_json,', 'literal(None), literal(None),')],
        A9ReadSurfaceTests, "test_import_summary_cannot_reveal_foreign_media",
        'self.assertNotIn(b["content"].file_path, response.text, "A9 import summary leaked foreign media")')
    add("embedded_media_parser_bypass", media_refs, "_read_local_media_paths",
        [('if not isinstance(context, MediaReadContext):',
          'if context in {MediaReadContext.HTML, MediaReadContext.OPAQUE}:\n        return\n    if not isinstance(context, MediaReadContext):')],
        A9ReadSurfaceTests, "test_content_text_and_html_media_references_never_grant_ownership",
        'self.assertNotIn(b["content"].file_path, response.text, "A9 embedded content leaked foreign media")')
    add("root_runtime_media_bypass", player_runtime, "_build_player_runtime_config",
        [('if not read_payload_media_owned(db, channel.client_id,', 'if False and not read_payload_media_owned(db, channel.client_id,')],
        A9ReadSurfaceTests, "test_player_root_channel_opaque_media_is_not_trusted",
        'self.assertEqual(response.status_code, 403, "A9 root channel bypassed media ownership")')

    return [_run_delete_mutation(*case) for case in catalog]


def run_a9_r2_mutations():
    """Explicit URL contexts, checked with A7-R3 intact/identity controls."""
    import ast
    import inspect
    from app.services import media_references as refs
    from app.api.routes import contents, campaigns, channels, player
    from test_runtime_tenant_safety import A9R2ContextTests as Keys, A9R2TypedURLTests as Reads
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    catalog = []

    def add(name, module, attribute, edits, cls, method, anchor, defense=None):
        original = getattr(module, attribute)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                raise AssertionError("A9-R2 mutation edit not unique: " + old)
            source = source.replace(old, new)
        namespace = {}
        exec(compile(source, "<A9-R2 declared production mutation>", "exec"), original.__globals__, namespace)
        catalog.append((name, module, attribute, namespace[original.__name__], cls, method,
                        _assertion_sites(defense or getattr(cls, method), anchor)))

    add("typed_url_html_unescape", refs, "_typed_media_keys",
        [('canonical_media_reference(value)', 'canonical_media_reference(__import__("html").unescape(value))')],
        Keys, "test_typed_url_never_decodes_html_entities",
        'self.assertEqual(self.keys("/media/&#x41;/file.png", C.TYPED_URL), {"/media/&"},')
    add("html_attribute_entities_not_decoded", refs, "_html_candidates",
        [('views = tuple(_html_media_view(string, mode=mode) for mode in ("document", "fragment"))',
          'import re\n    for raw in re.findall(r\'src=["\\\']([^"\\\']*)["\\\']\', string):\n        yield MediaURLCandidate(raw, "HTML_ATTRIBUTE", effective_url=raw)\n    return')],
        Keys, "test_differential_typed_and_html_attribute",
        'self.assertEqual(self.keys(\'<img src="\' + raw + \'">\', C.HTML), {html},')
    add("ownership_decodes_before_uri_boundary", refs, "_typed_media_keys",
        [('canonical_media_reference(value)', 'canonical_media_reference(__import__("urllib.parse", fromlist=["unquote"]).unquote(value))')],
        Keys, "test_effective_url_key_matches_uri_path_not_decoded_document",
        'self.assertEqual(self.keys(url, C.TYPED_URL), {expected}, "A9-R2 validated/effective media key mismatch")')

    # Reintroduce wrong context only during each real surface's imported
    # collaborator. No route registration replacement or test observer patch.
    for name, module, attribute, method in (
            ("content_caller_html_context", contents, "readable_content_ids", "test_contents_typed_context"),
            ("preview_caller_html_context", campaigns, "build_campaign_sequence_preview_payload", "test_preview_typed_context"),
            ("admin_runtime_caller_html_context", channels, "build_player_runtime_from_snapshot", "test_admin_runtime_typed_context"),
            ("player_runtime_caller_html_context", player, "build_player_runtime_from_snapshot", "test_player_runtime_typed_context")):
        original = getattr(module, attribute)
        tree = ast.parse(inspect.getsource(original))
        function = tree.body[0]
        injected = ast.parse('from unittest.mock import patch as context_patch\n'
            'from app.services import media_references as media_context\n'
            'with context_patch.object(media_context, "_typed_media_keys", '
            'lambda value: set(media_context._read_local_media_paths(value, context=media_context.MediaReadContext.HTML))):\n'
            '    pass\n').body
        injected[-1].body = function.body
        function.body = injected
        namespace = {}
        exec(compile(ast.fix_missing_locations(tree), "<A9-R2 wrong surface context>", "exec"), original.__globals__, namespace)
        sites = (_assertion_sites(Reads.test_contents_typed_context,
                    'self.assertNotIn(self.res["A"]["content"].id, readable_content_ids(self.db),')
                 if name == "content_caller_html_context" else _assertion_sites(Reads.typed_url_defense,
                    'self.assertNotIn(url, response.text, "A9-R2 typed URL exposed foreign effective target")'))
        catalog.append((name, module, attribute, namespace[original.__name__], Reads, method, sites))

    add("unknown_context_permissive", refs, "_read_local_media_paths",
        [('raise MediaPathError("UNKNOWN_MEDIA_CONTEXT")', 'return')],
        Keys, "test_unknown_context_fails_closed",
        'with self.assertRaises(MediaPathError, msg="A9-R2 unknown context accepted"):')
    return [_run_delete_mutation(*case) for case in catalog]


def run_a9_r1_mutations():
    """Twelve declared production defects, with the A7-R3 identity harness."""
    import inspect
    from app.services import media_references as refs, layout_editor, player_runtime
    from app.api.routes import layouts
    from test_runtime_tenant_safety import A9R1CanonicalMediaTests as Keys, A9R1MediaReadTests as Reads
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    catalog = []

    def add(name, module, attribute, edits, cls, method, anchor, defense=None):
        original = getattr(module, attribute)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                raise AssertionError("A9-R1 mutation edit not unique: " + old)
            source = source.replace(old, new)
        namespace = {}
        exec(compile(source, "<A9-R1 declared production mutation>", "exec"), original.__globals__, namespace)
        sites = _assertion_sites(defense or getattr(cls, method), anchor)
        catalog.append((name, module, attribute, namespace[original.__name__], cls, method, sites))

    add("host_case_sensitive", refs, "canonical_media_reference",
        [("_media_origin(parsed) != _media_origin(public)", "parsed.netloc != public.netloc")],
        Keys, "test_host_case_defense",
        'self.assertEqual(self.classify("HTTP://TESTSERVER/media/A/MixedCase.PNG"), ("LOCAL", "/media/A/MixedCase.PNG"), "A9-R1 host case bypass")')
    add("default_port_not_canonical", refs, "_media_origin",
        [('port = parsed.port if parsed.port is not None else {"http": 80, "https": 443}.get(scheme)', 'port = parsed.port')],
        Keys, "test_default_port_defense",
        'self.assertEqual(self.classify("http://testserver:80/media/A/a.png"), ("LOCAL", "/media/A/a.png"), "A9-R1 default port bypass")')
    add("encoded_route_not_recognized", refs, "canonical_media_reference",
        [('path = unquote(raw_path, encoding="utf-8", errors="strict")', 'path = raw_path')],
        Keys, "test_percent_path_defense",
        'self.assertEqual(self.classify("/m%65dia/A/a.png"), ("LOCAL", "/media/A/a.png"), "A9-R1 encoded route bypass")')
    add("query_fragment_in_key", refs, "canonical_media_reference",
        [('raw_path = parsed.path', 'raw_path = parsed.path + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")')],
        Keys, "test_query_fragment_defense",
        'self.assertEqual(self.classify("/media/A/a.png?x=1#part"), ("LOCAL", "/media/A/a.png"), "A9-R1 query fragment key changed")')
    add("ambiguous_encoding_external", refs, "canonical_media_reference",
        [('except (ValueError, UnicodeError, MediaPathError):\n        return "AMBIGUOUS", None',
          'except (ValueError, UnicodeError, MediaPathError):\n        return "EXTERNAL", None')],
        Keys, "test_ambiguous_encoding_defense",
        'self.assertEqual(self.classify("/m%2565dia/A/a.png"), ("AMBIGUOUS", None), "A9-R1 ambiguous encoding accepted")')
    add("html_src_not_inspected", refs, "_html_media_view",
        [('for name, value in attrs.items():', 'for name, value in attrs.items():\n                if name == "src":\n                    continue')],
        Reads, "test_html_src_defense",
        'self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("css_url_not_inspected", refs, "_html_text_candidates",
        [('yield from css_urls', 'pass  # mutant discards CSS URL candidates')],
        Reads, "test_css_url_defense",
        'self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("entities_not_inspected", refs, "_html_candidates",
        [('views = tuple(', 'if "&#" in string or "&quot;" in string:\n        return\n    views = tuple(')],
        Reads, "test_html_entities_defense",
        'self.assertNotIn(b["content"].file_path, unescape(response.text), "A9-R1 encoded HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("editor_state_guard_bypass", layouts, "build_editor_state_payload",
        [('"active_revision": safe_revision_payload(active_revision, layout, db)', '"active_revision": serialize_layout_revision(active_revision)')],
        Reads, "test_editor_html_defense",
        'self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("player_runtime_guard_bypass", player_runtime, "_resolve_layout_runtime",
        [('preview_state = build_layout_preview_state(layout, selected_revision.editor_state_json, db)', 'preview_state = selected_revision.editor_state_json')],
        Reads, "test_player_html_defense",
        'self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("duplicate_widget_trust_transfer", layout_editor, "_build_safe_layout_states",
        [('for widget in normalized["widgets"]:', 'trusted_ids = {w["id"] for w in normalized["widgets"] if _widget_is_tenant_safe(w, layout, db, references=references)}\n    for widget in normalized["widgets"]:'),
         ('if not _widget_is_tenant_safe(widget, layout, db, references=references):', 'if widget["id"] not in trusted_ids:')],
        Reads, "test_duplicate_widget_html_defense",
        'self.assertNotIn(b["content"].file_path, response.text, "A9-R1 HTML defense leaked foreign media")', defense=Reads.html_defense)
    add("foreign_widget_partially_retained", layout_editor, "_build_safe_layout_states",
        [('if not _widget_is_tenant_safe(widget, layout, db, references=references):\n            continue',
          'if not _widget_is_tenant_safe(widget, layout, db, references=references):\n            widget = deepcopy(widget)\n            widget["props_json"] = {}')],
        Reads, "test_html_src_defense",
        'self.assertNotIn("A9R1_FOREIGN_WIDGET_SENTINEL", response.text, "A9-R1 partial widget survived")', defense=Reads.html_defense)
    return [_run_delete_mutation(*case) for case in catalog]



def run_a9_r3_mutations():
    """Twelve effective-URL defects: intact controls and exact defense sites."""
    import inspect
    from app.services import media_references as refs
    from test_runtime_tenant_safety import A9R3ContextContractTests as Cases
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    results = []

    def add(name, target, edits, method, anchor):
        original = getattr(refs, target)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                results.append(dict(mutation=name, detected=False, mutation_result="INVALID_MUTATION",
                                    reason="source target not found exactly once", errors=0))
                return
            source = source.replace(old, new)
        namespace = {}
        try:
            exec(compile(source, "<A9-R3 declared production mutation>", "exec"), original.__globals__, namespace)
        except (SyntaxError, ImportError, NameError) as error:
            results.append(dict(mutation=name, detected=False, mutation_result="INVALID_HARNESS", errors=1, reason=type(error).__name__))
            return
        results.append(_run_delete_mutation(name, refs, target, namespace[original.__name__], Cases, method,
                                            _assertion_sites(getattr(Cases, method), anchor)))

    add("srcset_comma_split", "_srcset_media_urls",
        [('url = value[start:position]', 'url = value[start:position].split(",")[0]')],
        "test_srcset_comma_is_url_data", 'self.assertEqual(self.keys(\'<img srcset="/media/owned.png,foreign.png 1x">\'),')
    add("css_escape_before_tokenization", "_css_media_urls",
        [('import tinycss2', 'import tinycss2\n    import re\n    decoded = value.replace("\\\\)", ")")\n    yield from re.findall(r"url\\(([^)]*)\\)", decoded)\n    return')],
        "test_css_escapes_are_decoded_inside_tokens", "self.assertEqual(self.keys(r'url(/media/owned.png\\).png)',")
    add("css_unicode_strip", "_css_media_urls",
        [('return args[0].value', 'return args[0].value.strip()')],
        "test_css_nbsp_is_url_data", 'self.assertEqual(self.keys(\'url("/media/owned.png')
    add("image_set_strings_ignored", "_css_media_urls",
        [('candidate = first.value if first.type == "string" else url_token(first)',
          'if first.type == "string":\n                continue\n            candidate = url_token(first)')],
        "test_image_set_string_urls_checked", 'self.assertEqual(self.keys(\'image-set("/media/foreign.png" 1x)\',')
    add("html_base_ignored", "_html_media_view",
        [('bases.append(href)', 'pass'), ('raise MediaPathError("UNKNOWN_RELATIVE_DOCUMENT_BASE")', 'continue')],
        "test_base_is_resolved_before_relative_reference", 'self.assertEqual(self.keys(\'<base href="/media/owned.html">')
    add("relative_checked_before_base", "_html_media_view",
        [('effective = candidate.effective_url or _effective_url(token, base=base)',
          'if not (token.startswith("/") or urlsplit(token).scheme):\n            continue\n        effective = candidate.effective_url or _effective_url(token, base=base)')],
        "test_relative_parent_resolution", 'self.assertEqual(self.keys(\'<base href="/media/A/owned.html">')
    add("typed_boundary_not_normalized", "_effective_url",
        [('value = value.strip(_URL_BOUNDARY_SPACE)', 'value = value')],
        "test_typed_boundary_whitespace_is_effective", "self.assertEqual(self.keys('/media/foreign.png ',")
    add("typed_url_html_decode", "_typed_media_keys",
        [('canonical_media_reference(value)', 'canonical_media_reference(__import__("html").unescape(value))')],
        "test_typed_entity_never_decoded", "self.assertEqual(self.keys('/media/&#x41;/file.png',")
    add("ambiguous_css_accepted", "_css_media_urls",
        [('raise MediaPathError("UNSUPPORTED_CSS_FUNCTION")', 'continue')],
        "test_ambiguous_css_is_omitted", 'with self.assertRaises(MediaPathError, msg="A9-R3 ambiguous CSS accepted"):')
    add("effective_key_diverges", "canonical_media_reference",
        [('raw_path = parsed.path', 'raw_path = parsed.path.split("%", 1)[0]')],
        "test_effective_key_is_not_renormalized_as_markup", "self.assertEqual(self.keys('/media/a%3Fb%23c?query=1#fragment',")
    add("srcset_foreign_candidate_ignored", "_srcset_media_urls",
        [('yield from candidates', 'yield from candidates[:1]')],
        "test_all_srcset_candidates_checked", 'self.assertEqual(self.keys(\'<img srcset="/media/own.png 1x,')
    add("image_set_foreign_candidate_ignored", "_css_media_urls",
        [('for group in groups:', 'for group in groups[:1]:')],
        "test_all_image_set_candidates_checked", 'self.assertEqual(self.keys(\'image-set(url("/media/own.png")')
    add("opaque_media_context_ignored", "_opaque_media_keys",
        [('literal = set()', 'return set()\n    literal = set()')],
        "test_opaque_mixed_text_requires_whole_url", 'with self.assertRaises(MediaPathError, msg="A9-R3 opaque partial URL approved")')
    add("html_text_partial_token_authorized", "_html_text_candidates",
        [('if classification in {"LOCAL", "AMBIGUOUS"}:', 'if False:'),
         (r'''[^\t\n\f\r "'<>]+''', r'''[^\s"'<>()[\]{},;]+''')],
        "test_opaque_mixed_text_requires_whole_url", 'with self.assertRaises(MediaPathError, msg="A9-R3 mixed HTML text cannot approve a substring")')
    add("meta_refresh_target_truncated", "_html_refresh_url",
        [('target = refresh.group("url").strip(_HTML_SPACE)', 'target = refresh.group("url").strip(_HTML_SPACE).split(",", 1)[0]')],
        "test_meta_refresh_explicit_url_context", 'self.assertEqual(self.keys(\'<meta http-equiv="refresh" content="0; url=/media/owned.png,foreign.png">\'),')
    return results



class A9R3MutationHarnessTests(__import__("unittest").TestCase):
    def test_intact_css_fingerprint_ignores_only_frozenset_order(self):
        from types import FunctionType
        from app.services import media_references as media
        import test_tenant_relation_matrix as matrix
        original = media._css_media_urls
        self.assertEqual(matrix._mutation_fingerprint(original),
                         matrix._mutation_fingerprint(matrix._intact_control(original)))

        def membership(value):
            return value in {"dpi", "dpcm", "x", "dppx"}

        original_constants = membership.__code__.co_consts
        index = next(i for i, value in enumerate(original_constants) if isinstance(value, frozenset))
        from itertools import permutations
        for order in permutations(("dpi", "dpcm", "x", "dppx")):
            constants = list(original_constants)
            constants[index] = frozenset(order)
            clone = FunctionType(membership.__code__.replace(co_consts=tuple(constants)),
                                 membership.__globals__)
            self.assertEqual(matrix._mutation_fingerprint(membership), matrix._mutation_fingerprint(clone))
        constants[index] = frozenset(("dpi", "dpcm", "x", "foreign"))
        changed = FunctionType(membership.__code__.replace(co_consts=tuple(constants)), membership.__globals__)
        self.assertNotEqual(matrix._mutation_fingerprint(membership), matrix._mutation_fingerprint(changed))
        constants[index] = tuple(("dpi", "dpcm", "x", "dppx"))
        changed_type = FunctionType(membership.__code__.replace(co_consts=tuple(constants)), membership.__globals__)
        self.assertNotEqual(matrix._mutation_fingerprint(membership), matrix._mutation_fingerprint(changed_type))

    def test_new_mutations_are_certified(self):
        results = run_a9_r3_mutations()
        self.assertEqual(len(results), 15)
        for row in results:
            with self.subTest(mutation=row['mutation']):
                self.assertEqual(row['mutation_result'], 'DETECTED', row)
                self.assertEqual(row['control_original']['status'], 'PASS')
                self.assertEqual(row['control_recompiled']['status'], 'PASS')
                self.assertEqual(row['mutation_provenance']['MUTATION_APPLIED'], 'YES')
                self.assertEqual(row['harness_health']['status'], 'PASS')
                self.assertEqual(row['errors'], 0)

    def test_exact_two_unrelated_control_failures_are_not_detections(self):
        from unittest.mock import patch
        from test_tenant_relation_registry import TenantRelationRegistryTests
        from test_runtime_tenant_safety import PhaseAR1RuntimeTests
        from test_tenant_mutation_guards import run_a6_mutations
        from test_tenant_relation_matrix import run_previous_mutations_with_a7_boundary
        for cls, method, runner, name in (
            (TenantRelationRegistryTests, 'test_removed_and_ghost_relation_rejected_by_validator', run_a6_mutations, 'removed_relation'),
            (PhaseAR1RuntimeTests, 'test_all_revision_reads_sanitize_historical_preview', run_previous_mutations_with_a7_boundary, 'revision_preview')):
            def unrelated(test):
                test.fail('QA_UNRELATED_CONTROL_FAILURE — intentionally unrelated to mutant defense')
            with self.subTest(name=name):
                with patch.object(cls, method, unrelated):
                    row, = runner(only=name)
                self.assertEqual(row['mutation_result'], 'INVALID_HARNESS', row)
                self.assertFalse(row['detected'])
                self.assertEqual(row['control_original']['status'], 'NOT_PASS')
                self.assertIsNone(row['mutant'])
                real, = runner(only=name)
                self.assertEqual(real['mutation_result'], 'DETECTED', real)

    def probe(self, *, replacement=None, attribute='validate_registry', cls=None):
        from app.services import tenant_relations as registry
        from test_tenant_relation_registry import TenantRelationRegistryTests
        from test_tenant_relation_matrix import _run_delete_mutation, _legacy_defense_sites
        if replacement is None:
            replacement = lambda *args, **kwargs: True
        return _run_delete_mutation('removed_relation', registry, attribute, replacement,
            cls or TenantRelationRegistryTests, 'test_removed_and_ghost_relation_rejected_by_validator',
            _legacy_defense_sites('A6', 'removed_relation'), after_setup=True)

    def test_legacy_missing_target_is_invalid_mutation(self):
        row = self.probe(attribute='unregistered_missing_target')
        self.assertEqual(row['mutation_result'], 'INVALID_MUTATION')
        self.assertFalse(row['detected'])

    def test_legacy_unchanged_target_is_invalid_mutation(self):
        from app.services import tenant_relations as registry
        row = self.probe(replacement=registry.validate_registry)
        self.assertEqual(row['mutation_result'], 'INVALID_MUTATION')
        self.assertFalse(row['detected'])

    def test_legacy_fixture_and_language_errors_are_invalid_harness(self):
        from test_tenant_relation_registry import TenantRelationRegistryTests
        for error in (SyntaxError, ImportError, NameError, AssertionError):
            def broken_setup(self, kind=error):
                raise kind('synthetic broken fixture')
            broken = type('BrokenLegacyFixture', (TenantRelationRegistryTests,), {'setUp': broken_setup})
            with self.subTest(error=error.__name__):
                row = self.probe(cls=broken)
                self.assertEqual(row['mutation_result'], 'INVALID_HARNESS', row)
                self.assertFalse(row['detected'])
                self.assertIsNone(row['mutant'])

    def test_legacy_disconnected_observer_is_invalid_harness(self):
        from types import FunctionType
        from app.services import tenant_relations as registry
        original = registry.validate_registry
        copied = FunctionType(original.__code__, dict(original.__globals__), original.__name__)
        copied.__defaults__ = original.__defaults__
        row = self.probe(replacement=copied)
        self.assertEqual(row['mutation_result'], 'INVALID_HARNESS', row)
        self.assertFalse(row['detected'])

    def test_legacy_missing_collaborator_is_invalid_harness(self):
        from unittest.mock import patch
        from app.services import tenant_relations as registry
        with patch.dict(vars(registry)):
            del registry.APPROVED_RELATION_FINGERPRINTS
            row = self.probe()
        self.assertEqual(row['mutation_result'], 'INVALID_HARNESS', row)
        self.assertFalse(row['detected'])


def run_a9_r4_mutations():
    """Declared HTML5 regressions under the unchanged A7-R3 identity contract."""
    import inspect
    from app.services import media_references as refs
    from test_runtime_tenant_safety import A9R4HTMLSemanticsTests as Cases
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    results = []

    def add(name, target, edits, method, anchor):
        original = getattr(refs, target)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                results.append(dict(mutation=name, detected=False, mutation_result="INVALID_MUTATION",
                                    reason="R4 source target not found exactly once", errors=0))
                return
            source = source.replace(old, new)
        namespace = {}
        exec(compile(source, "<A9-R4 declared production mutation>", "exec"), original.__globals__, namespace)
        results.append(_run_delete_mutation(name, refs, target, namespace[original.__name__], Cases, method,
                                            _assertion_sites(getattr(Cases, method), anchor)))

    attribute_anchor = 'self.assertEqual(self.keys(\'<img src="/media/a&amp;amp;b.png">\'),'
    inert_anchor = 'self.assertIsNone(view.base, "A9-R4 inactive base altered document origin")'
    add("attribute_html_unescape", "_html_media_view",
        [('append(value, "HTML_ATTRIBUTE")', 'append(__import__("html").unescape(value), "HTML_ATTRIBUTE")')],
        "test_attribute_not_reparsed", attribute_anchor)
    add("regex_template_base", "_html_media_view",
        [('walk(root)', 'import re\n    bases.extend(re.findall(r\'<base href="([^"]*)"\', string))\n    walk(root)')],
        "test_inert_base_tree_semantics", inert_anchor)
    add("textarea_base_active", "_html_media_view",
        [('if tag == "style":', 'if tag == "textarea" and node.text:\n            bases.extend(n.attrib["href"] for n in parser.parseFragment(node.text, scripting=True).iter() if n.tag == html_namespace + "base")\n        if tag == "style":')],
        "test_inert_base_tree_semantics", inert_anchor)
    add("noscript_scripting_disabled", "_html_media_view",
        [('root = parser.parse(string, scripting=True)', 'root = parser.parse(string, scripting=False)'),
         ('root = parser.parseFragment(string, container="div", scripting=True)', 'root = parser.parseFragment(string, container="div", scripting=False)'),
         ('if any(code not in harmless for _, code, _ in parser.errors):', 'if False:')],
        "test_inert_base_tree_semantics", inert_anchor)
    add("ambiguous_entity_forced_semicolon", "_html_media_view",
        [('import html5lib', 'import html5lib\n    string = string.replace("&ampx", "&amp;x")')],
        "test_ambiguous_entity_effective_attributes", "self.assertEqual({c.effective_url for c in self.view(case['html']).candidates}, {case['foreign']},")
    add("attribute_reparsed_after_html5lib", "_html_media_view",
        [('append(value, "HTML_ATTRIBUTE")', 'append(html5lib.parseFragment(\'<img src="\' + value + \'">\', scripting=True)[0].attrib["src"], "HTML_ATTRIBUTE")')],
        "test_attribute_not_reparsed", attribute_anchor)
    add("srcset_second_html_decode", "_html_media_view",
        [('for url in _srcset_media_urls(value):', 'for url in _srcset_media_urls(__import__("html").unescape(value)):')],
        "test_srcset_not_redecoded", 'self.assertEqual(self.keys(\'<img srcset="/media/a&amp;amp;b.png 1x">\'),')
    add("style_second_html_decode", "_html_media_view",
        [('values.extend(_css_media_urls(value, _with_context=True))', 'values.extend(_css_media_urls(__import__("html").unescape(value), _with_context=True))')],
        "test_style_not_redecoded", 'self.assertEqual(self.keys(\'<div style="background:url(/media/a&amp;amp;b.png)"></div>\'),')
    add("rawtext_reparsed_as_html", "_html_media_view",
        [('if tag == "style":', 'if tag == "textarea" and node.text:\n            values.extend(_html_media_view(node.text, mode=mode, _depth=_depth + 1).candidates)\n        if tag == "style":')],
        "test_rawtext_is_not_reparsed", 'self.assertEqual({c.effective_url for c in self.view(\'<textarea><img src="/media/foreign.png"></textarea><img src="/media/owned.png">\').candidates},')
    add("base_from_inert_dom_node", "_html_media_view",
        [('template_seen = True', 'template_seen = True\n            bases.extend(n.attrib["href"] for n in node.iter() if n.tag == html_namespace + "base" and "href" in n.attrib)')],
        "test_inert_base_tree_semantics", inert_anchor)
    add("effective_key_differs_from_attribute", "_html_media_view",
        [('effective = candidate.effective_url or _effective_url(token, base=base)',
          'effective = (candidate.effective_url or _effective_url(token, base=base)).replace("&ampx", "&x")')],
        "test_ambiguous_entity_effective_attributes", "self.assertEqual({c.effective_url for c in self.view(case['html']).candidates}, {case['foreign']},")
    add("stdlib_attribute_parser_fallback", "_html_media_view",
        [('import html5lib', '''import html5lib
    from html.parser import HTMLParser
    legacy = []
    class OldParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            legacy.extend(value for name, value in attrs if name == "src")
    old = OldParser(convert_charrefs=True)
    old.feed(string)
    return HTMLMediaView(mode, None, (), tuple(MediaURLCandidate(value, "HTML_ATTRIBUTE", mode, None, _effective_url(value), ("HTML_ATTRIBUTE",)) for value in legacy))''')],
        "test_attribute_not_reparsed", 'self.assertEqual({c.effective_url for c in self.view(\'<img src="/media/owned&ampx.png">\').candidates},')
    add("ambiguous_consumer_entity_guard_removed", "_html_candidates",
        [('if any(view.ambiguous_character_reference for view in views):', 'if False:')],
        "test_ambiguous_refs_projection_omitted", 'with self.subTest(case=case["name"]), self.assertRaises(MediaPathError, msg="A9-R4 cross-consumer entity ambiguity admitted"):'.replace('case["name"]', "case['name']"))
    add("inert_url_disclosure_guard_removed", "_html_candidates",
        [('if any(view.inert_markup_seen for view in views):', 'if False:')],
        "test_inert_markup_never_discloses_a_foreign_reference", 'with self.subTest(tag=tag), self.assertRaises(MediaPathError, msg="A9-R4 inert foreign URL disclosed"):')
    return results


class A9R4MutationCertificationTests(__import__("unittest").TestCase):
    def test_r4_mutations_have_full_identity_contract(self):
        results = run_a9_r4_mutations()
        self.assertEqual(len(results), 14)
        for row in results:
            with self.subTest(mutation=row['mutation']):
                self.assertEqual(row['mutation_result'], 'DETECTED', row)
                self.assertTrue(row['detected'])
                self.assertEqual(row['mutation_provenance']['MUTATION_APPLIED'], 'YES')
                self.assertEqual(row['control_original']['status'], 'PASS')
                self.assertEqual(row['control_recompiled']['status'], 'PASS')
                self.assertEqual(row['harness_health']['status'], 'PASS')
                self.assertEqual(row['errors'], 0)


def run_a10_r1_mutations():
    """Serialization regressions with the unchanged A7-R3 identity harness."""
    import inspect
    from app.services import media_references as refs, layout_editor
    from test_runtime_tenant_safety import A10R1SerializationContextTests as Context
    from test_runtime_tenant_safety import A10R1SerializationProjectionTests as Projection
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    results = []

    def add(name, module, target, edits, cls, method, anchor, site=None):
        original = getattr(module, target)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                results.append(dict(mutation=name, detected=False, mutation_result="INVALID_MUTATION",
                                    reason="A10-R1 source target not found exactly once", errors=0))
                return
            source = source.replace(old, new)
        namespace = {}
        exec(compile(source, "<A10-R1 declared production mutation>", "exec"), original.__globals__, namespace)
        results.append(_run_delete_mutation(name, module, target, namespace[original.__name__], cls, method,
                                            _assertion_sites(site or getattr(cls, method), anchor)))

    guard = 'if set(attrs) - {"href", "target"}:'
    for name, replacement, method in (
        ("base_extra_attrs_not_inspected", "if False:", "test_base_other_attribute"),
        ("base_style_preserved", 'if set(attrs) - {"href", "target", "style"}:', "test_base_style_attribute"),
        ("base_data_preserved", 'if {a for a in attrs if not a.startswith("data-")} - {"href", "target"}:', "test_base_data_attribute"),
        ("base_events_preserved", 'if {a for a in attrs if not a.startswith("on")} - {"href", "target"}:', "test_base_event_attribute"),
    ):
        add(name, refs, "_validate_html_base_attributes", [(guard, replacement)], Context, method,
            'with self.assertRaises(MediaPathError, msg="A10-R1 unsupported base attribute survived"):',
            Context.assert_base_rejected)

    key_lines = [(f'yield from _read_local_media_paths({name}, context=MediaReadContext.JSON_KEY)', 'pass')
                 for name in ("field", "prop", "key")]
    add("json_values_only", refs, "_structured_media_keys", key_lines, Context,
        "test_json_widget_keys_and_values", 'self.assertEqual(self.keys(value, "WIDGET"),')
    key_line = 'yield from _read_local_media_paths(key, context=MediaReadContext.JSON_KEY)'
    add("json_top_level_key_skipped", refs, "_structured_media_keys",
        [(key_line, 'if _depth > 0:\n                ' + key_line)], Context,
        "test_json_top_level_key", 'self.assertEqual(self.keys({"/media/foreign.png": "ordinary"}),')
    add("json_nested_key_skipped", refs, "_structured_media_keys",
        [(key_line, 'if _depth == 0:\n                ' + key_line)], Context,
        "test_json_nested_keys", 'self.assertEqual(self.keys(value),')
    add("json_foreign_key_external_safe", refs, "_json_key_media_keys",
        [('if len(value) > _MEDIA_TEXT_LIMIT:', 'return set()\n    if len(value) > _MEDIA_TEXT_LIMIT:')],
        Projection, "test_foreign_json_key_is_not_external_safe",
        'self.assertEqual(refs.read_payload_media_decision(')

    safe_build = 'editor, preview = _build_safe_layout_states(layout, revision.editor_state_json, db)'
    partial = '''from copy import deepcopy
    def remove_keys(value):
        if isinstance(value, dict):
            return {key: remove_keys(child) for key, child in value.items() if not str(key).startswith("/media/")}
        if isinstance(value, list):
            return [remove_keys(child) for child in value]
        return value
    editor = remove_keys(deepcopy(revision.editor_state_json))
    preview = deepcopy(editor)'''
    add("json_key_scrub_preserves_unsafe_sibling", layout_editor, "safe_revision_payload", [(safe_build, partial)],
        Projection, "test_partial_key_scrub_cannot_keep_unsafe_sibling",
        'self.assertNotIn(foreign, list(a10_r1_returned_strings(payload)),')
    add("final_payload_key_projection_bypassed", layout_editor, "safe_revision_payload",
        [(safe_build, 'from copy import deepcopy\n    editor = deepcopy(revision.editor_state_json)\n    preview = deepcopy(editor)')],
        Projection, "test_final_serialized_revision_has_no_foreign_key",
        'self.assertNotIn(foreign, list(a10_r1_returned_strings(payload, keys_only=True)),')
    return results


class A10R1MutationCertificationTests(__import__("unittest").TestCase):
    def test_serialization_mutations_have_full_identity_contract(self):
        from run_tenant_graph_mutations import certificate_errors
        results = run_a10_r1_mutations()
        self.assertEqual(len(results), 10)
        for row in results:
            with self.subTest(mutation=row["mutation"]):
                self.assertEqual(certificate_errors(row), [], row)


def run_a10_r2_mutations():
    """Declared JSON-key faults checked by the unchanged A7-R3 identity contract.

    The ninth mutation disables the independent QA assertion, not a production
    sanitizer: response verification is intentionally a test-only guarantee.
    """
    import ast
    import inspect
    import textwrap
    from app.services import media_references as refs
    import test_runtime_tenant_safety as media_tests
    from test_runtime_tenant_safety import A10R2JSONKeyContextTests as Context
    from test_runtime_tenant_safety import A10R2EmbeddedMediaProjectionTests as Projection
    from test_tenant_relation_matrix import _run_delete_mutation, _assertion_sites
    results = []

    def add(name, module, target, cls, method, anchor, *, prelude=None, edits=(), site=None):
        original = getattr(module, target)
        source = inspect.getsource(original)
        for old, new in edits:
            if source.count(old) != 1:
                results.append(dict(mutation=name, detected=False, mutation_result="INVALID_MUTATION",
                                    reason="A10-R2 source target not found exactly once", errors=0))
                return
            source = source.replace(old, new)
        if prelude is not None:
            definition = ast.parse(source).body[0]
            first = definition.body[0]
            insertion = (first.end_lineno if isinstance(first, ast.Expr)
                         and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
                         else definition.lineno)
            lines = source.splitlines(keepends=True)
            lines.insert(insertion, textwrap.indent(prelude.rstrip() + "\n", "    "))
            source = "".join(lines)
        namespace = {}
        exec(compile(source, "<A10-R2 declared mutation>", "exec"), original.__globals__, namespace)
        results.append(_run_delete_mutation(
            name, module, target, namespace[original.__name__], cls, method,
            _assertion_sites(site or getattr(cls, method), anchor)))

    rejection = 'with self.assertRaises(MediaPathError, msg="A10-R2 embedded media candidate bypassed"):'
    slash_boundary = r'(?:^|[\s()\[\]{}=,:;<>])/'
    scanner_cases = (
        ("json_key_scanner_start_only", "test_embedded_prefix_scan",
         'if not value.lstrip().startswith(("/", "./", "media/", "m%")):\n    return False'),
        ("json_key_leading_slash_required", "test_leading_slash_not_required",
         'import re\nif not re.search(' + repr(slash_boundary) + ', value):\n    return False'),
        ("json_key_encoded_embedded_candidate_ignored", "test_percent_encoded_embedded",
         'if "%" in value:\n    return False'),
        ("json_key_wrapper_hides_candidate", "test_wrapper_equivalence",
         r'import re' + '\n' + r'if re.search(r"\b[A-Za-z_][A-Za-z0-9_.-]*\(", value):'
         + '\n    return False'),
        ("json_key_malformed_encoding_safe", "test_malformed_percent_encoding",
         r'import re' + '\n' + r'if re.search(r"%(?![0-9a-fA-F]{2})", value):'
         + '\n    return False'),
        ("json_key_slash_control_reopened", "test_slash_control_rejected",
         'import re\nif re.search(' + repr(slash_boundary) + ', value):\n    return False'),
    )
    for name, method, prelude in scanner_cases:
        add(name, refs, "_json_key_has_embedded_media", Context, method, rejection,
            prelude=prelude, site=Context.assert_embedded_rejected)

    add("json_key_relative_media_external_safe", refs, "_json_key_media_keys",
        Projection, "test_foreign_relative_key_not_external_safe",
        'self.assertEqual(decision, refs.MediaReadDecision.FOREIGN,',
        prelude='if (canonical_media_reference(value)[0] == "LOCAL"\n'
                '        and not value.lstrip().startswith(("/", ".", "http://", "https://", "//"))):\n'
                '    return set()')
    key_line = 'yield from _read_local_media_paths(key, context=MediaReadContext.JSON_KEY)'
    add("json_key_nested_key_not_traversed", refs, "_structured_media_keys",
        Context, "test_nested_key_walk",
        'with self.assertRaises(MediaPathError, msg="A10-R2 nested JSON key was skipped"):',
        edits=[(key_line, 'if _depth == 0:\n                ' + key_line)])
    add("json_key_multi_candidate_keeps_first_only", refs, "_json_key_media_keys",
        Projection, "test_multiple_owned_and_foreign_quoted_candidates_fail_closed",
        'self.assertEqual(decision, refs.MediaReadDecision.FOREIGN,',
        edits=[('        token = match.group(2)',
                '        if keys:\n            return " " * len(match.group())\n'
                '        token = match.group(2)')])
    add("json_key_final_payload_verifier_omitted", media_tests, "a10_r2_assert_no_foreign_media",
        Context, "test_final_payload_verifier_negative_control",
        'with self.assertRaises(AssertionError, msg="A10-R2 final payload verifier was omitted"):',
        prelude='return None')
    return results


class A10R2MutationCertificationTests(__import__("unittest").TestCase):
    def test_json_key_mutations_have_full_identity_contract(self):
        from run_tenant_graph_mutations import certificate_errors
        results = run_a10_r2_mutations()
        self.assertEqual(len(results), 10)
        for row in results:
            with self.subTest(mutation=row["mutation"]):
                self.assertEqual(certificate_errors(row), [], row)
