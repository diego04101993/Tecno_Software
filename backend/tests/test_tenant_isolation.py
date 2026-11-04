"""Isolated real HTTP/JWT tenant fixtures; never import app.main or use external DB."""
import os
import json
import hashlib
import uuid
from pathlib import Path
from datetime import datetime, UTC
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Run with the isolated backend test harness")

from sqlalchemy import create_engine,event,select,func
from sqlalchemy.orm import sessionmaker,Session
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.models.base import Base
from app.models.entities import *
from app.api.router import api_router
from app.api.routes import media,realtime
from app.api.deps import get_db
from app.core.security import create_access_token

from app.core.config import get_settings


class TenantFixture(TestCase):
    def make_engine(self):
        return create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    def setUp(self):
        root_tmp = self.enterContext(TemporaryDirectory(prefix="tenant-phase-a-"))
        root_tmp = SimpleNamespace(name=root_tmp)
        self.enterContext(patch.object(get_settings(), "MEDIA_ROOT", Path(root_tmp.name)))
        engine=self.make_engine()
        @event.listens_for(engine,"connect")
        def fk_on(conn,_):
            if engine.dialect.name == "sqlite":
                conn.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
        Sessions=sessionmaker(engine,expire_on_commit=False)
        app=FastAPI()
        app.include_router(api_router,prefix="/api")
        app.include_router(media.router)
        def isolated_db():
            with Sessions() as db:
                yield db
        app.dependency_overrides[get_db]=isolated_db
        self.enterContext(patch.object(realtime, 'SessionLocal', Sessions))
        http=TestClient(app,raise_server_exceptions=True)
        db=Sessions()
        def add(cls,**kw):
            obj=cls(**kw);db.add(obj);db.flush();return obj
        tenants={};res={};users={};tokens={};ptokens={}
        for side in ["A","B"]:
            tenant=add(Client,name="Tenant "+side,slug="tenant-"+side.lower());tenants[side]=tenant
            branch=add(Branch,client_id=tenant.id,name="Branch "+side,code=side)
            channel=add(Channel,client_id=tenant.id,branch_id=branch.id,name="Channel "+side)
            layout=add(Layout,client_id=tenant.id,name="SECRET_LAYOUT_"+side)
            campaign=add(Campaign,client_id=tenant.id,name="SECRET_CAMPAIGN_"+side,layout_id=layout.id)
            folder=add(ContentFolder,client_id=tenant.id,name="Folder "+side)
            mp=Path(root_tmp.name)/tenant.id/"synthetic.mp4";mp.parent.mkdir();mp.write_bytes(("MEDIA_SECRET_"+side).encode())
            content=add(ContentItem,client_id=tenant.id,name="SECRET_CONTENT_"+side,type=ContentType.VIDEO,file_path=f"/media/{tenant.id}/synthetic.mp4",folder_id=folder.id)
            audio=add(ContentItem,client_id=tenant.id,name="SECRET_AUDIO_"+side,type=ContentType.AUDIO)
            playlist=add(AudioPlaylist,client_id=tenant.id,name="SECRET_PLAYLIST_"+side,kind=AudioPlaylistKind.MUSIC)
            audioitem=add(AudioPlaylistItem,playlist_id=playlist.id,content_id=audio.id,sort_order=1)
            seq=add(CampaignSequenceItem,campaign_id=campaign.id,content_id=content.id,item_type=CampaignSequenceItemType.CONTENT,sort_order=1)
            legacy=add(CampaignPlaylistItem,campaign_id=campaign.id,content_id=content.id,sort_order=1)
            assignment=add(CampaignChannelAssignment,campaign_id=campaign.id,channel_id=channel.id)
            sched=add(Schedule,client_id=tenant.id,campaign_id=campaign.id,channel_id=channel.id,branch_id=branch.id,title="Schedule "+side)
            ds=add(DataSource,client_id=tenant.id,name="Source "+side,source_type=DataSourceType.FILE_UPLOAD)
            dataset=add(Dataset,client_id=tenant.id,data_source_id=ds.id,name="Dataset "+side,slug="dataset-"+side.lower())
            csv=Path(root_tmp.name)/"datasets"/tenant.id/dataset.id/"fixture.csv";csv.parent.mkdir(parents=True);csv.write_bytes(b"value\nsynthetic\n")
            imp=add(DatasetImport,dataset_id=dataset.id,source_filename="test.csv",storage_path=csv.relative_to(Path(root_tmp.name)).as_posix())
            dataset.current_import_id=imp.id
            row=add(DatasetRow,dataset_id=dataset.id,import_id=imp.id,row_index=0,row_data_json={"secret":"DATA_"+side})
            binding=add(LayoutDataBinding,layout_id=layout.id,dataset_id=dataset.id,name="Binding "+side,preset_key=list(LayoutBindingPreset)[0])
            experience=add(TouchExperience,client_id=tenant.id,name="Experience "+side,slug="exp-"+side.lower())
            screen=add(KioskScreen,client_id=tenant.id,experience_id=experience.id,name="Screen "+side,slug="screen-"+side.lower())
            location=add(TouchLocation,experience_id=experience.id,name="Location "+side)
            wall=add(Videowall,client_id=tenant.id,name="Wall "+side)
            ptoken="tpw_synthetic_audit_"+side
            player=add(PlayerDevice,client_id=tenant.id,branch_id=branch.id,channel_id=channel.id,hardware_id="synthetic-"+side,device_name="Player "+side,app_version="audit",player_token_hash=hashlib.sha256(ptoken.encode()).hexdigest(),is_active=True)
            ptokens[side]=ptoken
            res[side]=dict(branch=branch,channel=channel,layout=layout,campaign=campaign,folder=folder,content=content,audio=audio,playlist=playlist,audioitem=audioitem,sequence=seq,legacy=legacy,schedule=sched,source=ds,dataset=dataset,import_record=imp,binding=binding,experience=experience,screen=screen,location=location,wall=wall,player=player)
            for role in UserRole:
                if side=="B" and role in {UserRole.SUPER_ADMIN,UserRole.STAFF_ADMIN,UserRole.STAFF_OPERATOR}:continue
                global_role=role in {UserRole.SUPER_ADMIN,UserRole.STAFF_ADMIN,UserRole.STAFF_OPERATOR}
                u=add(User,email=f"{side.lower()}-{role.value}@example.com",full_name=f"{side} {role.value}",password_hash="synthetic-unusable",role=role,client_id=None if global_role else tenant.id,branch_id=branch.id if role in {UserRole.OPERATOR,UserRole.BRANCH_MANAGER} else None)
                key=side+"_"+role.value;users[key]=u;tokens[key]=create_access_token(u.id)
        db.commit()
        A=res["A"];B=res["B"];ca=tenants["A"].id;cb=tenants["B"].id
        self.db, self.engine, self.http = db, engine, http
        self.root = Path(root_tmp.name)
        self.res, self.tenants, self.users = res, tenants, users
        self.tokens, self.ptokens, self.add = tokens, ptokens, add
        self.app = app
        self.addCleanup(engine.dispose)
        self.addCleanup(db.close)
        self.addCleanup(http.close)

    def request(self, method, path, *, who="A_client_admin", body=None, **kw):
        token = self.ptokens[who[-1]] if who.startswith("player_") else self.tokens[who]
        response = self.http.request(method, path, headers={"Authorization": "Bearer " + token},
                                     **({"json": body} if body is not None else {}), **kw)
        return response


class TenantRelationsTests(TenantFixture):
    def test_foreign_relationship_writes_rejected_for_all_writer_roles(self):
        a, b = self.res["A"], self.res["B"]
        cases = [
            ("/api/campaigns/", {"client_id": self.tenants["A"].id, "name": "test", "layout_id": b["layout"].id}),
            ("/api/kiosk/screens", {"client_id": self.tenants["A"].id, "name": "test", "slug": "new", "experience_id": b["experience"].id}),
            ("/api/schedules/", {"client_id": self.tenants["A"].id, "campaign_id": a["campaign"].id, "branch_id": b["branch"].id, "title": "test"}),
            ("/api/audio/events", {"client_id": self.tenants["A"].id, "channel_id": a["channel"].id, "content_id": b["audio"].id, "entry_kind": "music"}),
            ("/api/audio/events", {"client_id": self.tenants["A"].id, "channel_id": a["channel"].id, "playlist_id": b["playlist"].id, "entry_kind": "music"}),
        ]
        for role in UserRole:
            for path, body in cases:
                with self.subTest(role=role.value, path=path, keys=list(body)):
                    response = self.request("POST", path, who="A_" + role.value, body=body)
                    self.assertIn(response.status_code, (403, 404), response.text)

    def test_own_writes_preserve_all_eight_role_contracts(self):
        for role in UserRole:
            response = self.request("POST", "/api/campaigns/", who="A_" + role.value,
                body={"client_id": self.tenants["A"].id, "layout_id": self.res["A"]["layout"].id, "name": "own"})
            expected = 403 if role in {UserRole.OPERATOR, UserRole.BRANCH_MANAGER} else 201
            self.assertEqual(response.status_code, expected, response.text)
        for role, expected in (("branch_manager", 200), ("operator", 403), ("client_operator", 200)):
            response = self.request("PATCH", f"/api/channels/{self.res['A']['channel'].id}",
                                    who="A_" + role, body={"name": "own"})
            self.assertEqual(response.status_code, expected, response.text)

    def test_touch_and_legacy_targets_same_tenant_and_experience(self):
        a, b = self.res["A"], self.res["B"]
        other_experience = self.add(TouchExperience, client_id=self.tenants["A"].id, name="other", slug="other")
        other_screen = self.add(KioskScreen, client_id=self.tenants["A"].id, experience_id=other_experience.id, name="other", slug="other")
        button = self.add(KioskButton, screen_id=a["screen"].id, label="ok", x=0, y=0, width=10, height=10, action_type=KioskActionType.NAVIGATE_MENU)
        self.db.commit()
        base = f"/api/touch/experiences/{a['experience'].id}/screens/{a['screen'].id}/buttons"
        for target in (b["screen"], other_screen, a["screen"]):
            body = dict(label="test", x=0, y=0, width=10, height=10, action_type="navigate_menu", target_screen_id=target.id)
            for method, path in (("POST", base), ("PATCH", base + "/" + button.id), ("POST", f"/api/kiosk/screens/{a['screen'].id}/buttons")):
                with self.subTest(target=target.id, path=path):
                    response = self.request(method, path, body=body)
                    self.assertIn(response.status_code, (200, 201) if target is a["screen"] else (404,), response.text)

    def test_orphan_and_corrupt_branch_identity_denied_before_reads(self):
        for role in ("client", "client_admin", "client_operator", "branch_manager", "operator"):
            user = self.users["A_" + role]
            original = user.client_id
            user.client_id = None
            self.db.commit()
            for path in ("/api/users/", "/api/channels/", "/api/contents/"):
                self.assertEqual(self.request("GET", path, who="A_" + role).status_code, 403)
            user.client_id = original
            self.db.commit()
        for role in ("branch_manager", "operator"):
            user = self.users["A_" + role]
            for branch in (None, self.res["B"]["branch"].id):
                user.branch_id = branch
                self.db.commit()
                self.assertEqual(self.request("GET", "/api/channels/", who="A_" + role).status_code, 403)

    def test_operator_layout_gets_never_persist(self):
        layout = self.res["A"]["layout"]
        for existing in (False, True):
            if existing:
                self.add(LayoutRevision, layout_id=layout.id, revision_number=1, name="existing", status="draft", is_current_draft=True)
                self.db.commit()
            before = list(self.db.execute(select(LayoutRevision.id, LayoutRevision.updated_at, LayoutRevision.preview_state_json)))
            for suffix in ("editor-state", "preview"):
                response = self.request("GET", f"/api/layouts/{layout.id}/{suffix}", who="A_operator")
                self.assertEqual(response.status_code, 200, response.text)
            self.db.expire_all()
            after = list(self.db.execute(select(LayoutRevision.id, LayoutRevision.updated_at, LayoutRevision.preview_state_json)))
            self.assertEqual(before, after)

class HistoricalRelationFixture(TenantFixture):
    def snapshot(self):
        from hashlib import sha256
        self.db.expire_all()
        rows = {
            table.name: sorted(json.dumps(dict(row._mapping), sort_keys=True, default=str)
                               for row in self.db.execute(select(table)))
            for table in Base.metadata.sorted_tables
        }
        files = {str(path.relative_to(self.root)): sha256(path.read_bytes()).hexdigest()
                 for path in self.root.rglob("*") if path.is_file()}
        return rows, files

    def observe_unchanged(self, action):
        from contextlib import ExitStack
        from sqlalchemy import inspect
        before = self.snapshot()
        counters = dict(INSERT=0, UPDATE=0, DELETE=0, flush=0, commit=0, unlink=0,
                        orm_set=0, orm_add=0, orm_delete=0)
        def sql(conn, cursor, statement, params, context, many):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in ("INSERT", "UPDATE", "DELETE"):
                counters[verb] += 1
        def flushing(*args):
            counters["flush"] += 1
        def committing(*args):
            counters["commit"] += 1
        def setting(target, value, old, initiator):
            if inspect(target).persistent and value != old:
                counters["orm_set"] += 1
        listeners = [(self.engine, "before_cursor_execute", sql),
                     (Session, "before_flush", flushing), (self.engine, "commit", committing)]
        listeners.extend((getattr(mapper.class_, attr.key), "set", setting)
                         for mapper in Base.registry.mappers for attr in mapper.column_attrs)
        for target, name, fn in listeners:
            event.listen(target, name, fn)
        original_add, original_delete, original_unlink = Session.add, Session.delete, Path.unlink
        def add(session, *args, **kwargs):
            counters["orm_add"] += 1
            return original_add(session, *args, **kwargs)
        def delete(session, *args, **kwargs):
            counters["orm_delete"] += 1
            return original_delete(session, *args, **kwargs)
        def unlink(path, *args, **kwargs):
            counters["unlink"] += 1
            return original_unlink(path, *args, **kwargs)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(Session, "add", add))
                stack.enter_context(patch.object(Session, "delete", delete))
                stack.enter_context(patch.object(Path, "unlink", unlink))
                result = action()
        finally:
            for target, name, fn in reversed(listeners):
                event.remove(target, name, fn)
        self.assertEqual(counters, dict.fromkeys(counters, 0), counters)
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        self.assertEqual(self.snapshot(), before)
        return result

    def reject(self, method, path, **kwargs):
        response = self.observe_unchanged(lambda: self.request(method, path, **kwargs))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": "TENANT_RELATION_CONFLICT"})
        return response

    def foreign_touch_assignment(self):
        a, b = self.res["A"], self.res["B"]
        row = self.add(TouchExperienceAssignment, client_id=self.tenants["B"].id,
                       experience_id=a["experience"].id, channel_id=b["channel"].id,
                       metadata_json={"synthetic": "FOREIGN_ASSIGNMENT_METADATA"})
        self.db.commit()
        return row

    def foreign_audio_assignment(self, branch_only=False):
        a, b = self.res["A"], self.res["B"]
        row = self.add(AudioAssignment, client_id=self.tenants["B"].id,
                       channel_id=None if branch_only else a["channel"].id,
                       branch_id=a["branch"].id if branch_only else b["branch"].id,
                       music_playlist_id=b["playlist"].id)
        self.db.commit()
        return row


class HistoricalRelationshipTests(HistoricalRelationFixture):
    def test_touch_list_omits_foreign_assignment(self):
        row = self.foreign_touch_assignment()
        # Ownership must be checked independently of the referenced targets.
        other = self.add(TouchExperienceAssignment, client_id=self.tenants["B"].id,
                         experience_id=self.res["A"]["experience"].id,
                         channel_id=self.res["A"]["channel"].id)
        own = self.add(TouchExperienceAssignment, client_id=self.tenants["A"].id,
                       experience_id=self.res["A"]["experience"].id)
        self.db.commit()
        path = "/api/touch/experiences/" + self.res["A"]["experience"].id + "/assignments"
        for who in ("A_client_admin", "A_super_admin", "A_staff_admin", "A_staff_operator"):
            response = self.observe_unchanged(lambda: self.request("GET", path, who=who))
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r["id"] for r in response.json()], [own.id])
            self.assertNotIn(row.id, response.text)
            self.assertNotIn(other.id, response.text)
            self.assertNotIn("FOREIGN_ASSIGNMENT_METADATA", response.text)

    def test_touch_patch_rejects_foreign_assignment(self):
        row = self.foreign_touch_assignment()
        path = "/api/touch/experiences/" + row.experience_id + "/assignments/" + row.id
        for who in ("A_client_admin", "A_super_admin", "A_staff_admin", "A_staff_operator"):
            self.reject("PATCH", path, who=who, body={"sort_order": 777})

    def test_touch_delete_rejects_foreign_assignment(self):
        row = self.foreign_touch_assignment()
        path = "/api/touch/experiences/" + row.experience_id + "/assignments/" + row.id
        for who in ("A_client_admin", "A_super_admin", "A_staff_admin", "A_staff_operator"):
            self.reject("DELETE", path, who=who)

    def test_audio_upsert_rejects_foreign_assignment(self):
        self.foreign_audio_assignment()
        a = self.res["A"]
        for who in ("A_client_admin", "A_super_admin", "A_staff_admin", "A_staff_operator"):
            self.reject("POST", "/api/audio/assignments", who=who,
                        body={"channel_id": a["channel"].id, "music_playlist_id": a["playlist"].id})

    def test_audio_branch_upsert_rejects_all_corrupt_candidates(self):
        a = self.res["A"]
        self.add(AudioAssignment, client_id=self.tenants["A"].id, branch_id=a["branch"].id)
        self.foreign_audio_assignment(branch_only=True)
        self.reject("POST", "/api/audio/assignments",
                    body={"branch_id": a["branch"].id, "music_playlist_id": a["playlist"].id})

    def test_audio_context_reads_do_not_expose_foreign_assignment_or_event(self):
        row = self.foreign_audio_assignment()
        a, b = self.res["A"], self.res["B"]
        ev = self.add(AudioPlaybackEvent, client_id=self.tenants["B"].id, channel_id=a["channel"].id,
                      branch_id=a["branch"].id, playlist_id=b["playlist"].id, content_id=b["audio"].id,
                      entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
        self.db.commit()
        for who in ("A_client_admin", "A_super_admin"):
            for path, params in (("/api/audio/assignments", {"channel_id": a["channel"].id}),
                                 ("/api/audio/assignments", {"branch_id": a["branch"].id}),
                                 ("/api/audio/reports/summary", {"channel_id": a["channel"].id}),
                                 ("/api/audio/reports/summary", {"branch_id": a["branch"].id}),
                                 ("/api/audio/runtime-config", {"channel_id": a["channel"].id})):
                with self.subTest(path=path, who=who, params=params):
                    response = self.observe_unchanged(lambda: self.request("GET", path, who=who, params=params))
                    self.assertEqual(response.status_code, 200, response.text)
                    for forbidden in (row.id, ev.id, b["playlist"].id, b["playlist"].name, b["audio"].name):
                        self.assertNotIn(forbidden, response.text)

    def test_experience_delete_preserves_foreign_screen(self):
        a, b = self.res["A"], self.res["B"]
        b["screen"].experience_id = a["experience"].id
        self.db.commit()
        for who in ("A_client_admin", "A_super_admin"):
            self.reject("DELETE", "/api/touch/experiences/" + a["experience"].id, who=who)

    def test_experience_delete_preserves_foreign_assignment(self):
        row = self.foreign_touch_assignment()
        self.reject("DELETE", "/api/touch/experiences/" + row.experience_id)

    def test_experience_delete_checks_incoming_screen_links(self):
        a, b = self.res["A"], self.res["B"]
        button = self.add(KioskButton, screen_id=b["screen"].id, label="synthetic",
                          target_screen_id=a["screen"].id, action_type=KioskActionType.NAVIGATE_MENU)
        self.db.commit()
        self.reject("DELETE", "/api/touch/experiences/" + a["experience"].id)
        self.db.delete(button); self.db.commit()
        for field in ("home_screen_id", "attract_screen_id"):
            setattr(b["experience"], field, a["screen"].id)
            self.db.commit()
            self.reject("DELETE", "/api/touch/experiences/" + a["experience"].id)
            setattr(b["experience"], field, None)
            self.db.commit()

    def test_channel_delete_preserves_foreign_audio(self):
        self.foreign_audio_assignment()
        for who in ("A_client_admin", "A_super_admin"):
            self.reject("DELETE", "/api/channels/" + self.res["A"]["channel"].id, who=who)

    def test_channel_delete_checks_all_affected_dependencies(self):
        a, b = self.res["A"], self.res["B"]
        client_b = self.tenants["B"].id
        candidates = [
            (Schedule, dict(client_id=client_b, campaign_id=b["campaign"].id, channel_id=a["channel"].id, title="synthetic")),
            (TouchExperienceAssignment, dict(client_id=client_b, experience_id=b["experience"].id, channel_id=a["channel"].id)),
            (AudioPlaybackEvent, dict(client_id=client_b, channel_id=a["channel"].id, entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))),
            (CampaignChannelAssignment, dict(campaign_id=b["campaign"].id, channel_id=a["channel"].id)),
            (DeviceOutput, dict(name="foreign root", root_channel_id=b["channel"].id, channel_id=a["channel"].id)),
            (DeviceOutput, dict(name="foreign target", root_channel_id=a["channel"].id, channel_id=b["channel"].id)),
        ]
        for model in (PlayerHeartbeat, PlayerPlaybackEvent):
            for transitive in (False, True):
                values = dict(client_id=client_b, branch_id=b["branch"].id,
                              channel_id=b["channel"].id if transitive else a["channel"].id,
                              player_device_id=a["player"].id if transitive else b["player"].id)
                if model is PlayerPlaybackEvent:
                    values["started_at"] = datetime.now(UTC)
                candidates.append((model, values))
        for model, values in candidates:
            with self.subTest(model=model.__name__, values=list(values)):
                row = self.add(model, **values); self.db.commit()
                self.reject("DELETE", "/api/channels/" + a["channel"].id)
                self.db.delete(row); self.db.commit()
        self.db.delete(a["player"]); self.db.commit()
        b["player"].channel_id = a["channel"].id; self.db.commit()
        self.reject("DELETE", "/api/channels/" + a["channel"].id)

    def test_same_tenant_operations_still_work(self):
        a = self.res["A"]
        path = "/api/touch/experiences/" + a["experience"].id + "/assignments"
        created = self.request("POST", path, body={"channel_id": a["channel"].id})
        self.assertEqual(created.status_code, 201, created.text)
        rid = created.json()["id"]
        self.assertEqual(self.request("PATCH", path + "/" + rid, body={"sort_order": 3}).status_code, 200)
        self.assertEqual(self.request("DELETE", path + "/" + rid).status_code, 204)
        body = {"channel_id": a["channel"].id, "music_playlist_id": a["playlist"].id}
        first = self.request("POST", "/api/audio/assignments", body=body)
        self.assertEqual(first.status_code, 201, first.text)
        second = self.request("POST", "/api/audio/assignments", body={**body, "songs_between_spots": 7})
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.json()["songs_between_spots"], 7)
        self.assertEqual(self.request("DELETE", "/api/touch/experiences/" + a["experience"].id).status_code, 204)
        self.assertEqual(self.request("DELETE", "/api/channels/" + a["channel"].id).status_code, 204)

    def test_conflict_logs_have_no_sensitive_values(self):
        row = self.foreign_touch_assignment()
        with self.assertLogs("app.services.tenancy", level="WARNING") as logs:
            self.reject("DELETE", "/api/touch/experiences/" + row.experience_id + "/assignments/" + row.id)
        self.assertEqual([entry.getMessage() for entry in logs.records], ["TENANT_RELATION_CONFLICT"])


class LocalPostgresFixture:
    """Run the same HTTP assertions in a fresh schema on a verified disposable DB."""
    def make_engine(self):
        from sqlalchemy.engine import make_url
        from sqlalchemy.schema import CreateSchema, DropSchema
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1"}
                or url.database != "tecno_control_retention_test"):
            raise RuntimeError("Refusing nonlocal or non-test database")
        schema = "tenant_ar3_" + uuid.uuid4().hex
        control = create_engine(url)
        with control.begin() as connection:
            connection.execute(CreateSchema(schema))
        def cleanup():
            try:
                with control.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
            finally:
                control.dispose()
        self.addCleanup(cleanup)
        return create_engine(url, connect_args={"options": "-csearch_path=" + schema})


from unittest import skipUnless

@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLHistoricalRelationshipTests(LocalPostgresFixture, HistoricalRelationshipTests):
    pass

R4_ROLES = ("A_client_admin", "A_staff_operator", "A_staff_admin", "A_super_admin")


class AlternateMutationTests(HistoricalRelationFixture):
    def test_touch_screen_incoming_references(self):
        a, b = self.res["A"], self.res["B"]
        self.add(KioskButton, screen_id=b["screen"].id, label="foreign synthetic",
                 target_screen_id=a["screen"].id, action_type=KioskActionType.NAVIGATE_MENU)
        b["experience"].home_screen_id = a["screen"].id
        b["experience"].attract_screen_id = a["screen"].id
        a["experience"].home_screen_id = a["screen"].id
        self.db.commit()
        for who in R4_ROLES:
            self.reject("DELETE", f"/api/touch/experiences/{a['experience'].id}/screens/{a['screen'].id}", who=who)

    def test_campaign_delete_foreign_schedule(self):
        a, b = self.res["A"], self.res["B"]
        b["schedule"].campaign_id = a["campaign"].id
        self.db.commit()
        for who in R4_ROLES:
            self.reject("DELETE", "/api/campaigns/" + a["campaign"].id, who=who)

    def test_channel_campaign_replace_foreign_assignment(self):
        a, b = self.res["A"], self.res["B"]
        self.add(CampaignChannelAssignment, campaign_id=b["campaign"].id, channel_id=a["channel"].id)
        self.db.commit()
        for who in R4_ROLES:
            self.reject("PUT", f"/api/channels/{a['channel'].id}/campaign-assignment",
                        body={"campaign_id": a["campaign"].id}, who=who)

    def test_videowall_conflict_is_generic(self):
        a, b = self.res["A"], self.res["B"]
        for kind in ("primary", "node"):
            if kind == "primary":
                b["wall"].primary_channel_id = a["channel"].id
            else:
                self.add(VideowallNode, videowall_id=b["wall"].id, channel_id=a["channel"].id,
                         position_index=1, row_index=0, column_index=0, x=0, y=0, width=1920, height=1080)
            self.db.commit()
            for who in R4_ROLES:
                with self.assertLogs("app.services.tenancy", level="WARNING") as logs:
                    self.reject("DELETE", "/api/channels/" + a["channel"].id, who=who)
                self.assertEqual([row.getMessage() for row in logs.records], ["TENANT_RELATION_CONFLICT"])
            b["wall"].primary_channel_id = None
            self.db.commit()



    def test_screen_incoming_owners_checked_independently(self):
        a, b = self.res["A"], self.res["B"]
        path = f"/api/touch/experiences/{a['experience'].id}/screens/{a['screen'].id}"
        for kind in ("home", "attract", "button", "button_experience"):
            with self.subTest(kind=kind):
                button = None
                if kind in ("home", "attract"):
                    setattr(b["experience"], kind + "_screen_id", a["screen"].id)
                else:
                    if kind == "button_experience":
                        b["screen"].client_id = self.tenants["A"].id
                    button = self.add(KioskButton, screen_id=b["screen"].id, label="synthetic",
                                      target_screen_id=a["screen"].id, action_type=KioskActionType.NAVIGATE_MENU)
                self.db.commit()
                for who in R4_ROLES:
                    self.reject("DELETE", path, who=who)
                if button:
                    self.db.delete(button)
                b["experience"].home_screen_id = None
                b["experience"].attract_screen_id = None
                b["screen"].client_id = self.tenants["B"].id
                self.db.commit()

    def test_campaign_other_independent_dependents(self):
        a, b = self.res["A"], self.res["B"]
        for kind in ("assignment", "heartbeat", "playback"):
            with self.subTest(kind=kind):
                if kind == "assignment":
                    row = self.add(CampaignChannelAssignment, campaign_id=a["campaign"].id, channel_id=b["channel"].id)
                else:
                    fields = dict(client_id=self.tenants["B"].id, branch_id=b["branch"].id,
                                  channel_id=b["channel"].id, player_device_id=b["player"].id)
                    if kind == "heartbeat":
                        row = self.add(PlayerHeartbeat, current_campaign_id=a["campaign"].id, **fields)
                    else:
                        row = self.add(PlayerPlaybackEvent, campaign_id=a["campaign"].id, started_at=datetime.now(UTC), **fields)
                self.db.commit()
                for who in R4_ROLES:
                    self.reject("DELETE", "/api/campaigns/" + a["campaign"].id, who=who)
                self.db.delete(row)
                self.db.commit()

    def test_same_tenant_mutations_remain_allowed(self):
        a = self.res["A"]
        for index, who in enumerate(R4_ROLES):
            with self.subTest(who=who):
                exp = self.add(TouchExperience, client_id=self.tenants["A"].id, name="own", slug=f"own-{index}")
                screen = self.add(KioskScreen, client_id=exp.client_id, experience_id=exp.id, name="own", slug=f"own-{index}")
                button = self.add(KioskButton, screen_id=screen.id, label="own", target_screen_id=screen.id,
                                  action_type=KioskActionType.NAVIGATE_MENU)
                exp.home_screen_id = exp.attract_screen_id = screen.id
                campaign = self.add(Campaign, client_id=exp.client_id, name="own")
                schedule = self.add(Schedule, client_id=exp.client_id, campaign_id=campaign.id, channel_id=a["channel"].id, title="own")
                self.add(CampaignChannelAssignment, campaign_id=campaign.id, channel_id=a["channel"].id)
                self.db.commit()
                screen_id, campaign_id, schedule_id = screen.id, campaign.id, schedule.id
                response = self.request("DELETE", f"/api/touch/experiences/{exp.id}/screens/{screen.id}", who=who)
                self.assertEqual(response.status_code, 204, response.text)
                self.db.expire_all()
                self.assertIsNone(self.db.get(KioskScreen, screen_id))
                self.assertIsNone(exp.home_screen_id)
                self.assertIsNone(exp.attract_screen_id)
                response = self.request("PUT", f"/api/channels/{a['channel'].id}/campaign-assignment",
                                        body={"campaign_id": campaign.id}, who=who)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(list(self.db.scalars(select(CampaignChannelAssignment.campaign_id)
                    .where(CampaignChannelAssignment.channel_id == a["channel"].id))), [campaign.id])
                response = self.request("DELETE", "/api/campaigns/" + campaign.id, who=who)
                self.assertEqual(response.status_code, 204, response.text)
                self.db.expire_all()
                self.assertIsNone(self.db.get(Campaign, campaign_id))
                self.assertIsNone(self.db.get(Schedule, schedule_id))

    def test_same_tenant_videowall_keeps_existing_block(self):
        a = self.res["A"]
        a["wall"].primary_channel_id = a["channel"].id
        self.db.commit()
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("DELETE", "/api/channels/" + a["channel"].id, who=who))
            self.assertEqual(response.status_code, 409)
            self.assertNotEqual(response.json(), {"detail": "TENANT_RELATION_CONFLICT"})


class ScopedReadIntegrityTests(HistoricalRelationFixture):
    def seed_case(self, kind):
        a, b = self.res["A"], self.res["B"]
        ca = self.tenants["A"].id
        if kind == "contents":
            b["content"].folder_id = a["folder"].id
            own, corrupt = a["content"], b["content"]
            path = f"/api/contents/?folder_id={a['folder'].id}"
        elif kind == "schedules":
            b["schedule"].channel_id = a["channel"].id
            own, corrupt = a["schedule"], b["schedule"]
            path = f"/api/schedules/?channel_id={a['channel'].id}"
        elif kind == "channels":
            b["channel"].branch_id = a["branch"].id
            own, corrupt = a["channel"], b["channel"]
            path = f"/api/channels/?branch_id={a['branch'].id}"
        elif kind == "audio_items":
            own = a["audioitem"]
            corrupt = self.add(AudioPlaylistItem, playlist_id=a["playlist"].id, content_id=b["audio"].id, sort_order=9)
            path = f"/api/audio/playlists/{a['playlist'].id}/items"
        elif kind == "audio_report":
            own = self.add(AudioPlaybackEvent, client_id=ca, channel_id=a["channel"].id,
                           content_id=a["audio"].id, playlist_id=a["playlist"].id,
                           entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
            corrupt = self.add(AudioPlaybackEvent, client_id=ca, channel_id=a["channel"].id,
                               content_id=b["audio"].id, playlist_id=b["playlist"].id,
                               entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
            path = f"/api/audio/reports/summary?channel_id={a['channel'].id}"
        elif kind == "audio_assignments":
            own = self.add(AudioAssignment, client_id=ca, branch_id=a["branch"].id, music_playlist_id=a["playlist"].id)
            corrupt = self.add(AudioAssignment, client_id=ca, branch_id=a["branch"].id,
                               channel_id=a["channel"].id, music_playlist_id=b["playlist"].id)
            path = f"/api/audio/assignments?branch_id={a['branch'].id}"
        elif kind == "touch_assignments":
            own = self.add(TouchExperienceAssignment, client_id=ca, experience_id=a["experience"].id, channel_id=a["channel"].id)
            corrupt = self.add(TouchExperienceAssignment, client_id=ca, experience_id=a["experience"].id,
                               channel_id=b["channel"].id, branch_id=b["branch"].id,
                               metadata_json={"marker": "FOREIGN_REFERENCE_FIXTURE"})
            path = f"/api/touch/experiences/{a['experience'].id}/assignments"
        elif kind == "layout_bindings":
            own = a["binding"]
            corrupt = self.add(LayoutDataBinding, layout_id=a["layout"].id, dataset_id=b["dataset"].id,
                               name="FOREIGN_REFERENCE_FIXTURE", preset_key=list(LayoutBindingPreset)[0])
            self.add(LayoutBindingField, binding_id=corrupt.id, target_field="synthetic",
                     display_label="FOREIGN_REFERENCE_FIXTURE")
            path = f"/api/layouts/{a['layout'].id}/bindings"
        else:
            raise AssertionError(kind)
        self.db.commit()
        return path, own.id, corrupt.id

    def assert_safe_case(self, kind):
        path, own_id, corrupt_id = self.seed_case(kind)
        for who in R4_ROLES:
            with self.subTest(kind=kind, who=who):
                response = self.observe_unchanged(lambda: self.request("GET", path, who=who))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(corrupt_id, response.text)
                self.assertIn(own_id, response.text)
                for row in self.res["B"].values():
                    self.assertNotIn(row.id, response.text)
                for value in ("SECRET_AUDIO_B", "SECRET_PLAYLIST_B", "SECRET_CONTENT_B", "FOREIGN_REFERENCE_FIXTURE"):
                    self.assertNotIn(value, response.text)
                if kind == "audio_report":
                    self.assertEqual(sum(r["play_count"] for r in response.json()["music"]), 1)

    def test_contents_folder_context(self):
        self.assert_safe_case("contents")

    def test_schedules_channel_context(self):
        self.assert_safe_case("schedules")

    def test_channels_branch_context(self):
        self.assert_safe_case("channels")

    def test_audio_items_foreign_content(self):
        self.assert_safe_case("audio_items")

    def test_audio_report_foreign_resources(self):
        self.assert_safe_case("audio_report")

    def test_audio_assignments_foreign_playlist(self):
        self.assert_safe_case("audio_assignments")

    def test_touch_assignments_foreign_targets(self):
        self.assert_safe_case("touch_assignments")

    def test_layout_bindings_foreign_dataset(self):
        self.assert_safe_case("layout_bindings")

    def test_global_unfiltered_access_preserved(self):
        for who in R4_ROLES[1:]:
            for route, resource in (("contents", "content"), ("schedules", "schedule"), ("channels", "channel")):
                response = self.observe_unchanged(lambda: self.request("GET", "/api/" + route + "/", who=who))
                self.assertEqual(response.status_code, 200)
                ids = {row["id"] for row in response.json()}
                self.assertTrue({self.res[side][resource].id for side in ("A", "B")} <= ids)

    def test_schedule_campaign_context(self):
        a, b = self.res["A"], self.res["B"]
        b["schedule"].campaign_id = a["campaign"].id
        self.db.commit()
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET",
                "/api/schedules/?campaign_id=" + a["campaign"].id, who=who))
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row["id"] for row in response.json()], [a["schedule"].id])

    def test_sql_filter_precedes_counts_pagination_and_search(self):
        # These endpoints expose lists, not count/limit/offset/search parameters.
        # Exercise those operations on the actual SELECT emitted by each route.
        a = self.res["A"]
        for model, route, parent, fk, label in (
            (ContentItem, "contents", "folder", "folder_id", "name"),
            (Schedule, "schedules", "channel", "channel_id", "title"),
            (Channel, "channels", "branch", "branch_id", "name"),
        ):
            for side in ("A", "B"):
                for index in range(5):
                    values = dict(client_id=self.tenants[side].id, **{fk: a[parent].id, label: f"search-{side}-{index}"})
                    if model is ContentItem:
                        values["type"] = ContentType.IMAGE
                    if model is Schedule:
                        values["campaign_id"] = self.res[side]["campaign"].id
                    self.add(model, **values)
            self.db.commit()
            for who in R4_ROLES:
                captured = []
                def capture(state):
                    descriptions = getattr(state.statement, "column_descriptions", [])
                    # Capture the route's eligible ORM row query, not ownership
                    # column probes made by the response projection afterwards.
                    if descriptions and descriptions[0].get("expr") is model and not state.is_column_load:
                        captured.append(state.statement)
                event.listen(Session, "do_orm_execute", capture)
                try:
                    response = self.observe_unchanged(lambda: self.request("GET",
                        f"/api/{route}/?{fk}={a[parent].id}", who=who))
                finally:
                    event.remove(Session, "do_orm_execute", capture)
                self.assertEqual(response.status_code, 200)
                query = captured[-1].order_by(None).where(getattr(model, label).like("search-%"))
                self.assertEqual(self.db.scalar(select(func.count()).select_from(query.subquery())), 5)
                ids = []
                for offset in (0, 2, 4, 6):
                    page = list(self.db.scalars(query.order_by(getattr(model, label)).limit(2).offset(offset)))
                    self.assertTrue(all(row.client_id == self.tenants["A"].id for row in page))
                    self.assertEqual(len(page), {0: 2, 2: 2, 4: 1, 6: 0}[offset])
                    ids.extend(row.id for row in page)
                self.assertEqual(len(set(ids)), 5)
                self.assertEqual(list(self.db.scalars(query.where(getattr(model, label).like("search-B-%")))), [])

    def test_audio_runtime_omits_corrupt_assignment_and_uses_valid_branch(self):
        self.seed_case("audio_assignments")
        a, b = self.res["A"], self.res["B"]
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET",
                "/api/audio/runtime-config?channel_id=" + a["channel"].id, who=who))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["assignment_scope"], "branch")
            self.assertEqual(response.json()["music_playlist"]["id"], a["playlist"].id)
            self.assertNotIn(b["playlist"].id, response.text)

    def test_audio_event_foreign_fields_individually(self):
        a, b = self.res["A"], self.res["B"]
        own = self.add(AudioPlaybackEvent, client_id=self.tenants["A"].id, channel_id=a["channel"].id,
                       content_id=a["audio"].id, playlist_id=a["playlist"].id,
                       entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
        for field, key in (("content_id", "audio"), ("playlist_id", "playlist"), ("branch_id", "branch")):
            values = dict(client_id=self.tenants["A"].id, channel_id=a["channel"].id, content_id=a["audio"].id,
                          playlist_id=a["playlist"].id, entry_kind=AudioPlaybackEntryKind.MUSIC, played_at=datetime.now(UTC))
            values[field] = b[key].id
            self.add(AudioPlaybackEvent, **values)
        self.db.commit()
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET", "/api/audio/reports/summary?channel_id=" + a["channel"].id, who=who))
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r["id"] for r in response.json()["recent_events"]], [own.id])
            self.assertEqual(sum(r["play_count"] for r in response.json()["music"]), 1)

    def test_layout_binding_detail_fields_preview_and_runtime(self):
        path, own_id, bad_id = self.seed_case("layout_bindings")
        for who in R4_ROLES:
            for suffix in ("/" + bad_id, "/" + bad_id + "/fields", "/" + own_id, "/" + own_id + "/fields"):
                response = self.observe_unchanged(lambda: self.request("GET", path + suffix, who=who))
                self.assertEqual(response.status_code, 404 if bad_id in suffix else 200, response.text)
                self.assertNotIn(self.res["B"]["dataset"].id, response.text)
                self.assertNotIn("FOREIGN_REFERENCE_FIXTURE", response.text)
            for endpoint in ("data-preview", "runtime-data"):
                response = self.observe_unchanged(lambda: self.request("GET",
                    f"/api/layouts/{self.res['A']['layout'].id}/{endpoint}", who=who))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(bad_id, response.text)
                self.assertNotIn(self.res["B"]["dataset"].id, response.text)
                self.assertNotIn("FOREIGN_REFERENCE_FIXTURE", response.text)

    def test_campaign_scoped_item_reads(self):
        a, b = self.res["A"], self.res["B"]
        self.add(CampaignPlaylistItem, campaign_id=a["campaign"].id, content_id=b["content"].id, sort_order=9)
        self.add(CampaignSequenceItem, campaign_id=a["campaign"].id, content_id=b["content"].id,
                 item_type=CampaignSequenceItemType.CONTENT, sort_order=9)
        self.add(CampaignSequenceItem, campaign_id=a["campaign"].id, layout_id=b["layout"].id,
                 item_type=CampaignSequenceItemType.LAYOUT, sort_order=10)
        self.db.commit()
        for who in R4_ROLES:
            for suffix in ("playlist", "sequence"):
                response = self.observe_unchanged(lambda: self.request("GET", f"/api/campaigns/{a['campaign'].id}/{suffix}", who=who))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()), 1)
                self.assertEqual(response.json()[0]["content_id"], a["content"].id)
                self.assertNotIn(b["content"].id, response.text)
                self.assertNotIn(b["layout"].id, response.text)

    def test_touch_button_scoped_read(self):
        a, b = self.res["A"], self.res["B"]
        for target in (a["screen"], b["screen"]):
            self.add(KioskButton, screen_id=a["screen"].id, target_screen_id=target.id,
                     label="synthetic", action_type=KioskActionType.NAVIGATE_MENU)
        self.db.commit()
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET",
                f"/api/touch/experiences/{a['experience'].id}/screens/{a['screen'].id}/buttons", who=who))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()), 1)
            self.assertEqual(response.json()[0]["target_screen_id"], a["screen"].id)
            self.assertNotIn(b["screen"].id, response.text)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLAlternateMutationTests(LocalPostgresFixture, AlternateMutationTests):
    pass


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLScopedReadIntegrityTests(LocalPostgresFixture, ScopedReadIntegrityTests):
    pass


R5_READ_CASES = ("kiosk_target", "dataset_source", "dataset_import", "campaign_layout",
                 "schedule_campaign", "schedule_layout", "content_folder", "content_file",
                 "channel_branch", "experience_home", "experience_attract")


class ReadSideIntegrityTests(HistoricalRelationFixture):
    """Both directions of historical corruption, including global parent contexts."""

    def seed_read_case(self, kind):
        a, b = self.res["A"], self.res["B"]
        owner = self.tenants["A"].id
        strategy, field = "FOREIGN_FIELD_NEUTRALIZED", None
        if kind == "kiosk_target":
            good = self.add(KioskButton, screen_id=a["screen"].id, label="own",
                            target_screen_id=a["screen"].id, action_type=KioskActionType.NAVIGATE_MENU)
            bad = self.add(KioskButton, screen_id=a["screen"].id, label="bad relation",
                           target_screen_id=b["screen"].id, action_type=KioskActionType.NAVIGATE_MENU,
                           action_value=b["screen"].id, action_payload_json={"target": b["screen"].id})
            path = f"/api/kiosk/screens/{a['screen'].id}/buttons"
            field = "target_screen_id"
        elif kind.startswith("dataset"):
            good = a["dataset"]
            bad = self.add(Dataset, client_id=owner, data_source_id=a["source"].id,
                           name="bad relation", slug="r5-dataset")
            if kind == "dataset_source":
                bad.data_source_id = b["source"].id
                strategy = "ROW_OMITTED"
            else:
                bad.current_import_id = b["import_record"].id
                field = "current_import_id"
            path = "/api/datasets?client_id=" + owner
        elif kind == "campaign_layout":
            good = a["campaign"]
            bad = self.add(Campaign, client_id=owner, name="bad relation", layout_id=b["layout"].id)
            path, field = "/api/campaigns/?client_id=" + owner, "layout_id"
        elif kind.startswith("schedule"):
            good = a["schedule"]
            bad = self.add(Schedule, client_id=owner, channel_id=a["channel"].id,
                           campaign_id=a["campaign"].id, title="bad relation")
            setattr(bad, "campaign_id" if kind == "schedule_campaign" else "layout_id",
                    b["campaign" if kind == "schedule_campaign" else "layout"].id)
            path = "/api/schedules/?channel_id=" + a["channel"].id
            strategy = "ROW_OMITTED"
        elif kind.startswith("content"):
            good = a["audio"]  # Legitimate content with no file_path must remain.
            bad = self.add(ContentItem, client_id=owner, name="bad relation", type=ContentType.VIDEO)
            if kind == "content_folder":
                bad.folder_id, field = b["folder"].id, "folder_id"
            else:
                bad.file_path = b["content"].file_path
                bad.source_url = "http://testserver" + b["content"].file_path
                bad.metadata_json = {"derived_path": b["content"].file_path}
                strategy = "ROW_OMITTED"
            path = "/api/contents/?client_id=" + owner
        elif kind == "channel_branch":
            good = a["channel"]
            bad = self.add(Channel, client_id=owner, branch_id=b["branch"].id, name="bad relation")
            path, strategy = "/api/channels/?client_id=" + owner, "ROW_OMITTED"
        else:
            good = a["experience"]
            bad = self.add(TouchExperience, client_id=owner, name="bad relation", slug="r5-experience")
            field = "home_screen_id" if kind == "experience_home" else "attract_screen_id"
            setattr(bad, field, b["screen"].id)
            path = "/api/touch/experiences?client_id=" + owner
        self.db.commit()
        return dict(path=path, good=good, bad=bad, strategy=strategy, field=field)

    def assert_no_foreign_response(self, response):
        for row in self.res["B"].values():
            self.assertNotIn(row.id, response.text)
            if hasattr(row, "name"):
                self.assertNotIn(row.name, response.text)
        self.assertNotIn(self.res["B"]["content"].file_path, response.text)
        self.assertNotIn("DATA_B", response.text)

    def check_read_case(self, kind):
        case = self.seed_read_case(kind)
        for who in R4_ROLES:
            response = self.observe_unchanged(lambda: self.request("GET", case["path"], who=who))
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_no_foreign_response(response)
            by_id = {row["id"]: row for row in response.json()}
            self.assertIn(case["good"].id, by_id)
            if case["strategy"] == "ROW_OMITTED":
                self.assertNotIn(case["bad"].id, by_id)
            else:
                self.assertIsNone(by_id[case["bad"].id][case["field"]])
            if kind.startswith("dataset"):
                for suffix in ("", "/columns", "/rows", "/preview", "/imports"):
                    detail = self.observe_unchanged(lambda: self.request("GET",
                        "/api/datasets/" + case["bad"].id + suffix, who=who))
                    self.assertEqual(detail.status_code, 404 if kind == "dataset_source" else 200)
                    self.assert_no_foreign_response(detail)
            if kind.startswith("experience"):
                for suffix in ("", "/runtime-config"):
                    detail = self.observe_unchanged(lambda: self.request("GET",
                        "/api/touch/experiences/" + case["bad"].id + suffix, who=who))
                    self.assertEqual(detail.status_code, 200)
                    self.assert_no_foreign_response(detail)
            if kind.startswith("schedule"):
                global_read = self.observe_unchanged(lambda: self.request("GET", "/api/schedules/", who=who))
                self.assertNotIn(case["bad"].id, global_read.text)

    def test_kiosk_target(self): self.check_read_case("kiosk_target")
    def test_dataset_source(self): self.check_read_case("dataset_source")
    def test_dataset_import(self): self.check_read_case("dataset_import")
    def test_campaign_layout(self): self.check_read_case("campaign_layout")
    def test_schedule_campaign(self): self.check_read_case("schedule_campaign")
    def test_schedule_layout(self): self.check_read_case("schedule_layout")
    def test_content_folder(self): self.check_read_case("content_folder")
    def test_content_file(self): self.check_read_case("content_file")
    def test_channel_branch(self): self.check_read_case("channel_branch")
    def test_experience_home(self): self.check_read_case("experience_home")
    def test_experience_attract(self): self.check_read_case("experience_attract")

    def test_essential_relations_filter_before_pagination(self):
        for kind, model in (("dataset_source", Dataset), ("schedule_campaign", Schedule),
                            ("content_file", ContentItem), ("channel_branch", Channel)):
            case = self.seed_read_case(kind)
            for who in R4_ROLES:
                captured = []
                def capture(state):
                    descriptions = getattr(state.statement, "column_descriptions", [])
                    if descriptions and descriptions[0].get("expr") is model and not state.is_column_load:
                        captured.append(state.statement)
                event.listen(Session, "do_orm_execute", capture)
                try:
                    self.observe_unchanged(lambda: self.request("GET", case["path"], who=who))
                finally:
                    event.remove(Session, "do_orm_execute", capture)
                query = captured[-1].order_by(None).where(model.id.in_([case["good"].id, case["bad"].id]))
                self.assertEqual(self.db.scalar(select(func.count()).select_from(query.subquery())), 1)
                self.assertEqual([r.id for r in self.db.scalars(query.limit(1))], [case["good"].id])
                self.assertEqual(list(self.db.scalars(query.offset(1).limit(1))), [])
                self.assertEqual(list(self.db.scalars(query.where(model.id == case["bad"].id))), [])

    def test_branch_transitive_foreign_grant(self):
        a, b = self.res["A"], self.res["B"]
        layout = self.add(Layout, client_id=self.tenants["A"].id, name="unassigned")
        content = self.add(ContentItem, client_id=self.tenants["A"].id, name="unassigned", type=ContentType.TEXT)
        b["campaign"].layout_id = layout.id
        self.add(CampaignPlaylistItem, campaign_id=b["campaign"].id, content_id=content.id, sort_order=4)
        self.db.commit()
        def denied():
            for who in ("A_operator", "A_branch_manager"):
                response = self.observe_unchanged(lambda: self.request("GET", "/api/layouts/" + layout.id + "/preview", who=who))
                self.assertEqual(response.status_code, 404)
                response = self.observe_unchanged(lambda: self.request("GET", "/api/contents/", who=who))
                self.assertNotIn(content.id, response.text)
        denied()
        self.add(CampaignChannelAssignment, campaign_id=b["campaign"].id, channel_id=a["channel"].id)
        self.db.commit()
        denied()
        a["campaign"].layout_id = layout.id
        self.add(CampaignPlaylistItem, campaign_id=a["campaign"].id, content_id=content.id, sort_order=4)
        self.db.commit()
        for who in ("A_operator", "A_branch_manager"):
            response = self.observe_unchanged(lambda: self.request("GET", "/api/layouts/" + layout.id + "/preview", who=who))
            self.assertEqual(response.status_code, 200)
            response = self.observe_unchanged(lambda: self.request("GET", "/api/contents/", who=who))
            self.assertIn(content.id, response.text)

    def test_experience_patch_preflight(self):
        a, b = self.res["A"], self.res["B"]
        for field in ("home_screen_id", "attract_screen_id"):
            setattr(a["experience"], field, b["screen"].id)
            self.db.commit()
            for who in R4_ROLES:
                self.reject("PATCH", "/api/touch/experiences/" + a["experience"].id,
                            who=who, body={"name": "must not change"})
            setattr(a["experience"], field, None)
        self.db.commit()
        for who in R4_ROLES:
            response = self.request("PATCH", "/api/touch/experiences/" + a["experience"].id,
                                    who=who, body={"name": "legitimate", "home_screen_id": a["screen"].id})
            self.assertEqual(response.status_code, 200)

    def test_folder_conflict_code(self):
        a, b = self.res["A"], self.res["B"]
        b["content"].folder_id = a["folder"].id
        self.db.commit()
        for who in R4_ROLES:
            self.reject("DELETE", "/api/content-folders/" + a["folder"].id + "?cascade=true", who=who)

    def test_same_owner_wrong_parent_is_still_incoherent(self):
        a = self.res["A"]
        owner = self.tenants["A"].id
        other_exp = self.add(TouchExperience, client_id=owner, name="other", slug="r5-other")
        other_screen = self.add(KioskScreen, client_id=owner, experience_id=other_exp.id, name="other", slug="r5-other")
        a["experience"].home_screen_id = other_screen.id
        a["experience"].attract_screen_id = other_screen.id
        other_dataset = self.add(Dataset, client_id=owner, data_source_id=a["source"].id, name="other", slug="r5-other")
        other_import = self.add(DatasetImport, dataset_id=other_dataset.id, source_filename="synthetic.csv", storage_path="synthetic.csv")
        a["dataset"].current_import_id = other_import.id
        self.add(KioskButton, screen_id=a["screen"].id, label="same owner wrong parent",
                 target_screen_id=other_screen.id, action_type=KioskActionType.NAVIGATE_MENU)
        self.db.commit()
        for who in R4_ROLES:
            for path, forbidden in (("/api/touch/experiences/" + a["experience"].id, other_screen.id),
                                    ("/api/kiosk/screens/" + a["screen"].id + "/buttons", other_screen.id),
                                    ("/api/datasets/" + a["dataset"].id, other_import.id)):
                response = self.observe_unchanged(lambda: self.request("GET", path, who=who))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(forbidden, response.text)
            self.reject("PATCH", "/api/touch/experiences/" + a["experience"].id, who=who,
                        body={"name": "no implicit repair", "home_screen_id": None, "attract_screen_id": None})

    def test_transitive_schedule_and_channel_edges_cannot_grant_scope(self):
        a, b = self.res["A"], self.res["B"]
        layout = self.add(Layout, client_id=self.tenants["A"].id, name="unassigned")
        campaign = self.add(Campaign, client_id=self.tenants["A"].id, name="unassigned", layout_id=layout.id)
        # A schedule owned by B cannot grant a branch A access to an A resource.
        bad_schedule = self.add(Schedule, client_id=self.tenants["B"].id, campaign_id=campaign.id,
                                layout_id=layout.id, channel_id=a["channel"].id, branch_id=a["branch"].id, title="bad")
        # Neither can a Channel B falsely attached to Branch A.
        b["channel"].branch_id = a["branch"].id
        self.add(CampaignChannelAssignment, campaign_id=campaign.id, channel_id=b["channel"].id)
        other_branch = self.add(Branch, client_id=self.tenants["A"].id, name="other branch", code="r5-scope")
        other_channel = self.add(Channel, client_id=self.tenants["A"].id, branch_id=other_branch.id, name="other channel")
        self.add(Schedule, client_id=self.tenants["A"].id, campaign_id=campaign.id,
                 layout_id=layout.id, channel_id=other_channel.id, branch_id=a["branch"].id, title="incoherent branch")
        self.db.commit()
        for who in ("A_operator", "A_branch_manager"):
            response = self.observe_unchanged(lambda: self.request("GET", "/api/layouts/" + layout.id + "/preview", who=who))
            self.assertEqual(response.status_code, 404)
            response = self.observe_unchanged(lambda: self.request("GET", "/api/campaigns/", who=who))
            self.assertNotIn(campaign.id, response.text)
            response = self.observe_unchanged(lambda: self.request("GET", "/api/schedules/", who=who))
            self.assertNotIn(bad_schedule.id, response.text)
        self.add(Schedule, client_id=self.tenants["A"].id, campaign_id=campaign.id,
                 layout_id=layout.id, branch_id=a["branch"].id, title="legitimate branch schedule")
        self.db.commit()
        for who in ("A_operator", "A_branch_manager"):
            response = self.observe_unchanged(lambda: self.request("GET", "/api/layouts/" + layout.id + "/preview", who=who))
            self.assertEqual(response.status_code, 200)


@skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires disposable local PostgreSQL")
class PostgreSQLReadSideIntegrityTests(LocalPostgresFixture, ReadSideIntegrityTests):
    pass



class A9ReadHTTPTests(TenantFixture):
    def test_audio_library_omits_foreign_authoritative_media_before_serialization(self):
        a, b = self.res['A'], self.res['B']
        a['audio'].file_path = b['content'].file_path
        self.db.commit()
        response = self.request('GET', '/api/audio/library')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b['content'].file_path, response.text)
        self.assertNotIn(a['audio'].id, response.text)

    def test_user_read_neutralizes_foreign_branch_without_repair(self):
        user = self.users['A_branch_manager']
        user.branch_id = self.res['B']['branch'].id
        self.db.commit()
        response = self.request('GET', '/api/clients/' + self.tenants['A'].id + '/users')
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.json() if r['id'] == user.id)
        self.assertIsNone(row['branch_id'])
        self.assertNotIn(self.res['B']['branch'].id, response.text)
        self.db.expire_all()
        self.assertEqual(user.branch_id, self.res['B']['branch'].id)

    def test_dataset_foreign_import_neutralizes_derived_counts(self):
        dataset = self.res['A']['dataset']
        dataset.current_import_id = self.res['B']['import_record'].id
        dataset.row_count, dataset.column_count = 991, 772
        self.db.commit()
        response = self.request('GET', '/api/datasets/' + dataset.id)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['current_import_id'])
        self.assertEqual(response.json()['row_count'], 0)
        self.assertEqual(response.json()['column_count'], 0)
        self.db.expire_all()
        self.assertEqual(dataset.row_count, 991)


class A8WriteHTTPTests(HistoricalRelationFixture):
    """Real HTTP entrypoints with all mapped assignments measured, including INSERT construction."""
    def observe_unchanged(self, action):
        assignments = []
        def assignment(*args):
            assignments.append("ORM_ASSIGNMENT")
        targets = [getattr(mapper.class_, attr.key) for mapper in Base.registry.mappers for attr in mapper.column_attrs]
        for target in targets:
            event.listen(target, "set", assignment)
        try:
            return super().observe_unchanged(action)
        finally:
            for target in targets:
                event.remove(target, "set", assignment)
            self.assertEqual(assignments, [], "No ORM assignment may precede a rejected complete write plan")

    def assert_write_success(self, method, path, body=None, expected=200, **kwargs):
        response = self.request(method, path, body=body, **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def revision(self, *, corrupt=False):
        from app.services.layout_editor import build_initial_editor_state
        a, b = self.res["A"], self.res["B"]
        state = build_initial_editor_state(a["layout"])
        if corrupt:
            state["widgets"] = [{"id": "synthetic", "type": "image",
                                  "props_json": {"content_id": b["content"].id}}]
        revision = self.add(LayoutRevision, layout_id=a["layout"].id, revision_number=1,
                            name="QA revision", editor_state_json=state, preview_state_json={},
                            is_current_draft=True, status="draft",
                            created_by_user_id=self.users["A_super_admin"].id)
        self.db.commit()
        return revision

    def test_layout_create_edit_publish_and_binding_replacement_positive(self):
        a = self.res["A"]
        root = f"/api/layouts/{a['layout'].id}"
        first = self.assert_write_success("POST", root + "/revisions", {"name": "QA"}, 201)
        self.assert_write_success("PATCH", root + "/revisions/" + first["id"], {"name": "QA edit"})
        published = self.assert_write_success("POST", root + "/revisions/" + first["id"] + "/publish")
        self.assertTrue(published["is_current_published"])
        fields = self.assert_write_success("POST", root + "/bindings/" + a["binding"].id + "/fields",
                                          {"fields": [{"target_field": "name", "column_key": "value"}]})
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["column_key"], "value")
        second = self.assert_write_success("POST", root + "/revisions", {"clone_from_revision_id": first["id"]}, 201)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["created_by_user_id"], self.users["A_client_admin"].id)

    def test_layout_publish_historical_foreign_widget_before_flags(self):
        revision = self.revision(corrupt=True)
        path = f"/api/layouts/{self.res['A']['layout'].id}/revisions/{revision.id}/publish"
        for who in ("A_client_admin", "A_staff_operator", "A_staff_admin", "A_super_admin"):
            with self.subTest(who=who):
                self.reject("POST", path, who=who)

    def test_layout_create_revision_rejects_corrupt_clone_before_draft_flags(self):
        revision = self.revision(corrupt=True)
        self.reject("POST", f"/api/layouts/{self.res['A']['layout'].id}/revisions",
                    body={"clone_from_revision_id": revision.id, "name": "Clone"})

    def test_layout_patch_rejects_proposed_foreign_widget_before_name(self):
        revision = self.revision()
        state = dict(revision.editor_state_json, widgets=[
            {"id": "synthetic", "type": "image", "props_json": {"content_id": self.res["B"]["content"].id}}])
        self.reject("PATCH", f"/api/layouts/{self.res['A']['layout'].id}/revisions/{revision.id}",
                    body={"name": "Changed", "editor_state_json": state})

    def test_binding_collection_old_corruption_is_not_deleted_first(self):
        a, b = self.res["A"], self.res["B"]
        self.add(LayoutBindingField, binding_id=a["binding"].id, target_field="old",
                 options_json={"dataset_id": b["dataset"].id})
        self.db.commit()
        self.reject("POST", f"/api/layouts/{a['layout'].id}/bindings/{a['binding'].id}/fields",
                    body={"fields": [{"target_field": "new"}]})

    def test_binding_collection_new_corruption_is_not_inserted(self):
        a, b = self.res["A"], self.res["B"]
        self.add(LayoutBindingField, binding_id=a["binding"].id, target_field="old")
        self.db.commit()
        self.reject("POST", f"/api/layouts/{a['layout'].id}/bindings/{a['binding'].id}/fields",
                    body={"fields": [{"target_field": "new", "options_json": {"dataset_id": b["dataset"].id}}]})

    def test_dataset_import_current_foreign_source_precedes_file_and_rows(self):
        a, b = self.res["A"], self.res["B"]
        a["dataset"].data_source_id = b["source"].id
        self.db.commit()
        from app.api.routes import data_sources
        with patch.object(data_sources, "write_media_upload", side_effect=AssertionError("file created before approval")):
            self.reject("POST", f"/api/datasets/{a['dataset'].id}/imports/upload",
                        files={"file": ("synthetic.csv", b"name\nQA\n", "text/csv")})

    def test_dataset_reimport_positive_preserves_old_rows_and_sets_current(self):
        a = self.res["A"]
        old_import = a["import_record"].id
        result = self.assert_write_success("POST", f"/api/datasets/{a['dataset'].id}/imports/upload",
            expected=201, files={"file": ("synthetic.csv", b"name,count\nQA,2\n", "text/csv")})
        self.db.expire_all()
        self.assertEqual(a["dataset"].current_import_id, result["id"])
        self.assertIsNotNone(self.db.get(DatasetImport, old_import))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(DatasetRow).where(DatasetRow.import_id == result["id"])), 1)

    def test_upsert_foreign_existing_assignment_cannot_transfer_owner(self):
        self.foreign_audio_assignment()
        a = self.res["A"]
        self.reject("POST", "/api/audio/assignments",
                    body={"channel_id": a["channel"].id, "music_playlist_id": a["playlist"].id})

    def test_upsert_audio_positive_create_and_update_stable_identity(self):
        a = self.res["A"]
        payload = {"channel_id": a["channel"].id, "music_playlist_id": a["playlist"].id}
        first = self.assert_write_success("POST", "/api/audio/assignments", payload, 201)
        second = self.assert_write_success("POST", "/api/audio/assignments", dict(payload, songs_between_spots=5), 201)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["songs_between_spots"], 5)

    def test_channel_replace_old_foreign_collection_has_no_partial_delete(self):
        a, b = self.res["A"], self.res["B"]
        self.add(CampaignChannelAssignment, campaign_id=b["campaign"].id, channel_id=a["channel"].id)
        self.db.commit()
        self.reject("PUT", f"/api/channels/{a['channel'].id}/campaign-assignment",
                    body={"campaign_id": a["campaign"].id})

    def test_channel_rename_rejects_historical_foreign_output(self):
        a, b = self.res["A"], self.res["B"]
        self.add(DeviceOutput, root_channel_id=a["channel"].id, channel_id=b["channel"].id, name="QA", display_order=0)
        self.db.commit()
        self.reject("PATCH", f"/api/channels/{a['channel'].id}", body={"name": "Changed"})

    def test_campaign_assign_existing_path_checks_complete_historical_graph(self):
        a, b = self.res["A"], self.res["B"]
        a["sequence"].content_id = b["content"].id
        self.db.commit()
        self.reject("POST", f"/api/campaigns/{a['campaign'].id}/assignments", body={"channel_id": a["channel"].id})

    def test_player_heartbeat_rejects_current_foreign_device_graph_without_token_change(self):
        a, b = self.res["A"], self.res["B"]
        self.add(PlayerHeartbeat, player_device_id=a["player"].id, channel_id=a["channel"].id,
                 branch_id=a["branch"].id, client_id=self.tenants["A"].id,
                 status="online", current_content_id=b["content"].id)
        self.db.commit()
        self.reject("POST", "/api/player/heartbeat", who="player_A", body={"status": "online"})

    def test_touch_and_legacy_alternates_reject_corrupt_parent_before_insert(self):
        a, b = self.res["A"], self.res["B"]
        a["experience"].home_screen_id = b["screen"].id
        self.db.commit()
        for path in (f"/api/touch/experiences/{a['experience'].id}/screens/{a['screen'].id}/buttons",
                     f"/api/kiosk/screens/{a['screen'].id}/buttons"):
            with self.subTest(path=path):
                self.reject("POST", path, body={"label": "QA", "x": 0, "y": 0, "width": 10,
                                                "height": 10, "action_type": "navigate_menu"})

    def campaign_case(self, side, action, *, corrupt):
        a, b = self.res["A"], self.res["B"]
        item = a["legacy" if side == "playlist" else "sequence"]
        if corrupt:
            a["sequence"].content_id = b["content"].id
            self.db.commit()
        path = f"/api/campaigns/{a['campaign'].id}/{side}"
        method, payload, expected = "POST", None, 201
        if action == "add":
            # The legacy add API assigns the requested vacant slot; it does not
            # implement insertion into an occupied position.
            payload = {"content_id": a["content"].id, "duration_seconds": 9, "sort_order": 2}
        elif action == "update":
            method, path, payload, expected = "PATCH", path + "/" + item.id, {"duration_seconds": 9}, 200
        elif action == "reorder":
            path, payload, expected = path + "/reorder", {"ordered_item_ids": [item.id]}, 200
        else:
            path += "/" + item.id + "/duplicate"
        if corrupt:
            for role in ("client_admin", "staff_operator", "staff_admin", "super_admin"):
                self.reject(method, path, who="A_" + role, body=payload)
        else:
            result = self.assert_write_success(method, path, payload, expected)
            self.assertTrue(result)
            self.db.expire_all()
            for cls in (CampaignSequenceItem, CampaignPlaylistItem):
                rows = list(self.db.scalars(select(cls).where(cls.campaign_id == a["campaign"].id)))
                self.assertTrue(rows)
                self.assertTrue(all(row.content_id == a["content"].id for row in rows))


    def test_legacy_sequence_duplicate_keeps_survivor_id_and_new_creation_time(self):
        a = self.res["A"]
        legacy_id = a["legacy"].id
        old_time = datetime(2020, 1, 1, tzinfo=UTC)
        self.db.delete(a["sequence"])
        a["legacy"].created_at = old_time
        a["legacy"].updated_at = old_time
        self.db.commit()
        duplicate = self.assert_write_success("POST",
            f"/api/campaigns/{a['campaign'].id}/sequence/{legacy_id}/duplicate", expected=201)
        self.assertNotEqual(duplicate["id"], legacy_id)
        self.assertGreater(datetime.fromisoformat(duplicate["created_at"]).replace(tzinfo=UTC), old_time)
        original = self.db.get(CampaignSequenceItem, legacy_id)
        self.assertIsNotNone(original)
        self.assertEqual(original.created_at.replace(tzinfo=UTC), old_time)


for _a8_side in ("playlist", "sequence"):
    for _a8_action in ("add", "update", "reorder", "duplicate"):
        for _a8_bad in (False, True):
            def _a8_campaign_test(self, side=_a8_side, action=_a8_action, bad=_a8_bad):
                self.campaign_case(side, action, corrupt=bad)
            setattr(A8WriteHTTPTests, "test_campaign_" + _a8_side + "_" + _a8_action + ("_corrupt" if _a8_bad else "_positive"),
                    _a8_campaign_test)
del _a8_side, _a8_action, _a8_bad, _a8_campaign_test
