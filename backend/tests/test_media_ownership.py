"""Ownership, ambiguous paths, corrupt rows and public delivery compatibility."""
from sqlalchemy import select
from test_tenant_isolation import TenantFixture
from app.models.entities import ContentItem, ContentType, KioskScreen, LayoutRevision


class MediaOwnershipTests(TenantFixture):
    def test_reference_ownership_same_foreign_dataset_and_unknown(self):
        a, b = self.res["A"], self.res["B"]
        (self.root / "unknown.mp4").write_bytes(b"synthetic")
        cases = [(a["content"].file_path, 201), (b["content"].file_path, 409),
                 ("/media/" + b["import_record"].storage_path, 409), ("/media/unknown.mp4", 409)]
        for role in ("super_admin", "staff_admin", "staff_operator", "client", "client_admin", "client_operator"):
            for path, expected in cases:
                with self.subTest(role=role, path=path):
                    response = self.request("POST", "/api/contents/", who="A_" + role,
                        body=dict(client_id=self.tenants["A"].id, name="synthetic", type="video", file_path=path))
                    self.assertEqual(response.status_code, expected, response.text)
        # Directory names alone cannot override a contradictory historical owner.
        self.add(ContentItem, client_id=self.tenants["B"].id, name="corrupt", type=ContentType.VIDEO, file_path=a["content"].file_path)
        self.db.commit()
        response = self.request("POST", "/api/contents/", body=dict(name="ambiguous", type="video", file_path=a["content"].file_path))
        self.assertEqual(response.status_code, 409)

    def test_corrupt_content_delete_preserves_dataset_file(self):
        b = self.res["B"]
        path = self.root / b["import_record"].storage_path
        before = path.read_bytes()
        item = self.add(ContentItem, client_id=self.tenants["A"].id, name="corrupt",
                        type=ContentType.VIDEO, file_path="/media/" + b["import_record"].storage_path)
        self.db.commit()
        item_id = item.id
        response = self.request("DELETE", f"/api/contents/{item_id}")
        self.assertEqual(response.status_code, 204, response.text)
        self.db.expire_all()
        self.assertIsNone(self.db.get(ContentItem, item_id))
        self.assertTrue(path.exists(), 'Foreign dataset file was deleted')
        self.assertEqual(path.read_bytes(), before)

    def test_remaining_url_and_json_references_protect_file(self):
        a = self.res["A"]
        screen = a["screen"]
        screen.background_url = a["content"].file_path
        self.db.commit()
        path = self.root / a["content"].file_path.removeprefix("/media/")
        response = self.request("DELETE", f"/api/contents/{a['content'].id}")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(path.exists())
        other = self.add(ContentItem, client_id=self.tenants["A"].id, name="json-protected",
                         type=ContentType.VIDEO, file_path="/media/" + self.tenants["A"].id + "/json.mp4")
        json_path = self.root / self.tenants["A"].id / "json.mp4"
        json_path.write_bytes(b"fixture")
        self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1, name="ref", status="draft",
                 editor_state_json={"widgets": [{"props_json": {"src": other.file_path}}]})
        self.db.commit()
        self.assertEqual(self.request("DELETE", f"/api/contents/{other.id}").status_code, 204)
        self.assertTrue(json_path.exists())

    def test_legacy_flat_owned_file_and_unreferenced_owned_upload(self):
        flat = self.root / "historical.mp4"
        flat.write_bytes(b"historical")
        self.add(ContentItem, client_id=self.tenants["A"].id, name="owner", type=ContentType.VIDEO, file_path="/media/historical.mp4")
        self.db.commit()
        response = self.request("POST", "/api/contents/", body=dict(name="same", type="video", file_path="/media/historical.mp4"))
        self.assertEqual(response.status_code, 201)
        response = self.request("POST", "/api/contents/upload", files={"file": ("synthetic.mp4", b"fixture", "video/mp4")},
                                data={"name": "upload", "client_id": self.tenants["A"].id, "type": "video"})
        self.assertEqual(response.status_code, 201, response.text)

    def test_h1_public_get_head_and_range_remain_open(self):
        url = self.res["B"]["content"].file_path
        response = self.http.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"MEDIA_SECRET_B")
        self.assertEqual(self.http.head(url).status_code, 200)
        response = self.http.get(url, headers={"Range": "bytes=0-4"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"MEDIA")

class PhaseAR1MediaTests(TenantFixture):
    def test_folder_foreign_child_survives_unchanged(self):
        a,b=self.res["A"],self.res["B"]
        b["content"].folder_id=a["folder"].id; self.db.commit()
        before=(b["content"].folder_id,b["content"].updated_at); foreign_id=b["content"].id
        path=self.root/b["content"].file_path.removeprefix("/media/")
        response=self.request("DELETE",f"/api/content-folders/{a['folder'].id}?cascade=true")
        self.assertIn(response.status_code,(204,409))
        self.db.expire_all()
        item=self.db.get(ContentItem,foreign_id)
        self.assertIsNotNone(item)
        self.assertEqual((item.folder_id,item.updated_at),before)
        self.assertTrue(path.exists())

    def test_untrusted_references_cannot_grant_ownership(self):
        for field in ("source_url","metadata_json"):
            path=self.root/(field+".mp4");path.write_bytes(b"synthetic")
            url="/media/"+path.name
            values={field:url if field=="source_url" else {"src":url}}
            r=self.request("POST","/api/contents/",body=dict(name="hint",type="url",**values))
            self.assertEqual(r.status_code,201)
            r=self.request("POST","/api/contents/",body=dict(name="claim",type="video",file_path=url,
                uploaded={"physical_path":str(path)},trusted_upload=True))
            self.assertEqual(r.status_code,409)
            self.assertTrue(path.exists())

    def test_invalid_metadata_does_not_block_other_tenant(self):
        r=self.request("POST","/api/contents/",body=dict(name="bad",type="text",metadata_json={"path":"/media/../synthetic","url":"http://["}))
        self.assertEqual(r.status_code,201)
        r=self.request("POST","/api/contents/",who="B_client_admin",body=dict(name="own",type="video",file_path=self.res["B"]["content"].file_path))
        self.assertEqual(r.status_code,201,r.text)
        r=self.request("POST","/api/contents/upload",who="B_client_admin",data={"name":"own upload"},files={"file":("fixture.mp4",b"new","video/mp4")})
        self.assertEqual(r.status_code,201,r.text)

    def test_authoritative_model_and_unknown_namespace(self):
        from app.services.media_references import media_owned_by
        a,b=self.res["A"],self.res["B"]
        self.assertTrue(media_owned_by(self.db,self.root,self.root/a["content"].file_path.removeprefix("/media/"),self.tenants["A"].id))
        self.assertTrue(media_owned_by(self.db,self.root,self.root/b["import_record"].storage_path,self.tenants["B"].id))
        self.add(ContentItem,client_id=self.tenants["B"].id,name="ambiguous",type=ContentType.VIDEO,file_path=a["content"].file_path);self.db.commit()
        self.assertFalse(media_owned_by(self.db,self.root,self.root/a["content"].file_path.removeprefix("/media/"),self.tenants["A"].id))
        for path in (self.root/"unknown.mp4",self.root/self.tenants["A"].id/"unindexed.mp4"):
            path.write_bytes(b"unknown")
            self.assertFalse(media_owned_by(self.db,self.root,path,self.tenants["A"].id))

    def test_upload_and_deletion_remain_usable(self):
        r=self.request("POST","/api/contents/upload",data={"name":"fresh"},files={"file":("fixture.mp4",b"fresh","video/mp4")})
        self.assertEqual(r.status_code,201,r.text)
        path=self.root/r.json()["file_path"].removeprefix("/media/")
        self.assertEqual(self.request("DELETE","/api/contents/"+r.json()["id"]).status_code,204)
        self.assertFalse(path.exists())


from test_tenant_isolation import HistoricalRelationFixture, LocalPostgresFixture
from unittest import skipUnless
import os


class HistoricalContentDeleteTests(HistoricalRelationFixture):
    def foreign_reference(self, kind):
        from app.models.entities import CampaignSequenceItem, CampaignPlaylistItem, AudioPlaylistItem
        model, key, parent = {
            "sequence": (CampaignSequenceItem, "campaign_id", "campaign"),
            "legacy": (CampaignPlaylistItem, "campaign_id", "campaign"),
            "audio": (AudioPlaylistItem, "playlist_id", "playlist"),
        }[kind]
        values = {key: self.res["B"][parent].id, "content_id": self.res["A"]["content"].id, "sort_order": 9}
        if kind == "sequence":
            values["item_type"] = "content"
        row = self.add(model, **values)
        self.db.commit()
        return row

    def test_foreign_sequence_prevents_content_delete(self):
        self.foreign_reference("sequence")
        self.reject("DELETE", "/api/contents/" + self.res["A"]["content"].id)

    def test_foreign_legacy_prevents_content_delete(self):
        self.foreign_reference("legacy")
        self.reject("DELETE", "/api/contents/" + self.res["A"]["content"].id)

    def test_foreign_audio_item_prevents_content_delete(self):
        self.foreign_reference("audio")
        self.reject("DELETE", "/api/contents/" + self.res["A"]["content"].id)

    def test_mixed_references_block_all_roles_and_folder_cascade(self):
        for kind in ("sequence", "legacy", "audio"):
            self.foreign_reference(kind)
        a = self.res["A"]
        for who in ("A_client_admin", "A_super_admin", "A_staff_admin", "A_staff_operator"):
            self.reject("DELETE", "/api/contents/" + a["content"].id, who=who)
        self.reject("DELETE", "/api/content-folders/" + a["folder"].id + "?cascade=true")

    def test_other_foreign_set_null_dependents_are_preserved(self):
        from datetime import datetime, UTC
        from app.models.entities import AudioPlaybackEvent, PlayerHeartbeat, PlayerPlaybackEvent
        a, b = self.res["A"], self.res["B"]
        for model in (AudioPlaybackEvent, PlayerHeartbeat, PlayerPlaybackEvent):
            values = dict(client_id=self.tenants["B"].id, branch_id=b["branch"].id,
                          channel_id=b["channel"].id)
            values["current_content_id" if model is PlayerHeartbeat else "content_id"] = a["content"].id
            if model is AudioPlaybackEvent:
                values.update(entry_kind="music", played_at=datetime.now(UTC))
            else:
                values["player_device_id"] = b["player"].id
                if model is PlayerPlaybackEvent:
                    values["started_at"] = datetime.now(UTC)
            row = self.add(model, **values); self.db.commit()
            self.reject("DELETE", "/api/contents/" + a["content"].id)
            self.db.delete(row); self.db.commit()

    def test_missing_parent_fails_closed_with_constraints_restored(self):
        from sqlalchemy import update
        from app.models.entities import CampaignSequenceItem, CampaignPlaylistItem, AudioPlaylistItem
        import uuid
        for kind in ("sequence", "legacy", "audio"):
            row = self.foreign_reference(kind)
            model = type(row)
            parent_key = "playlist_id" if model is AudioPlaylistItem else "campaign_id"
            # Fixture-only corruption; all enforcement is restored before the HTTP request.
            with self.engine.connect() as connection:
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                else:
                    connection.exec_driver_sql("SET LOCAL session_replication_role=replica")
                connection.execute(update(model).where(model.id == row.id).values(**{parent_key: str(uuid.uuid4())}))
                connection.commit()
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
                else:
                    self.assertEqual(connection.exec_driver_sql("SHOW session_replication_role").scalar(), "origin")
            self.db.expire_all()
            self.reject("DELETE", "/api/contents/" + self.res["A"]["content"].id)
            self.db.delete(row); self.db.commit()

    def test_complete_batch_preflight_leaves_session_clean(self):
        from fastapi import HTTPException
        from app.services.content_cleanup import delete_content_items
        from app.services.media_references import media_reference_operation
        self.foreign_reference("sequence")
        a = self.res["A"]
        def action():
            with self.assertRaises(HTTPException) as raised:
                with media_reference_operation(self.db):
                    delete_content_items([a["audio"], a["content"]], self.db)
            self.assertEqual(raised.exception.detail, "TENANT_RELATION_CONFLICT")
        self.observe_unchanged(action)

    def test_own_relations_and_file_are_deleted_normally(self):
        from app.models.entities import AudioPlaylistItem, CampaignSequenceItem, CampaignPlaylistItem
        a = self.res["A"]
        self.add(AudioPlaylistItem, playlist_id=a["playlist"].id, content_id=a["content"].id, sort_order=9)
        self.db.commit()
        content_id = a["content"].id
        path = self.root / a["content"].file_path.removeprefix("/media/")
        r = self.request("DELETE", "/api/contents/" + content_id)
        self.assertEqual(r.status_code, 204, r.text)
        self.db.expire_all()
        self.assertIsNone(self.db.get(ContentItem, content_id))
        for model in (CampaignSequenceItem, CampaignPlaylistItem, AudioPlaylistItem):
            self.assertIsNone(self.db.scalar(select(model.id).where(model.content_id == content_id)))
        self.assertFalse(path.exists())


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLHistoricalContentDeleteTests(LocalPostgresFixture, HistoricalContentDeleteTests):
    pass
