"""SECURITY-1A: only disposable files and a verified in-memory database."""
from datetime import UTC, datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use tests/run_screen_rotation_suite.py (isolated memory DB and temporary MEDIA_ROOT)")

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.routes import contents, content_folders, data_sources, media, player
from app.core.config import get_settings
from app.models.base import Base
from app.models.entities import Branch, Campaign, Channel, Client, ContentFolder, ContentItem, ContentType, DataSource, DataSourceType, Dataset, DatasetImport, UserRole
from app.services.content_cleanup import cleanup_files, delete_content_items
from app.services.player_runtime import serialize_content


class MediaPathSafetyTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="tecno-sec1a-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "media"
        self.root.mkdir()
        self.settings_patch = patch.object(get_settings(), "MEDIA_ROOT", self.root)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        self.assertIn(self.engine.url.database, (None, "", ":memory:"))
        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, autoflush=False)
        self.addCleanup(self.db.close)
        self.tenant = Client(name="Temporary A", slug="sec-a")
        self.other = Client(name="Temporary B", slug="sec-b")
        self.db.add_all([self.tenant, self.other])
        self.db.commit()
        self.user = SimpleNamespace(role=UserRole.CLIENT_ADMIN, client_id=self.tenant.id, branch_id=None)
        app = FastAPI()  # No app.main, startup, seed, network or production settings.
        app.include_router(contents.router, prefix="/api/contents")
        app.include_router(content_folders.router, prefix="/api")
        app.include_router(media.router)
        app.include_router(data_sources.router, prefix="/api/data-sources")
        app.include_router(player.router, prefix="/api/player")

        def isolated_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def upload(self, filename="clip.mp4", data=b"complete media bytes"):
        return self.http.post("/api/contents/upload", data={"name": "Visible name"},
                              files={"file": (filename, data, "video/mp4")})

    def add_content(self, path, *, tenant=None, folder=None):
        item = ContentItem(client_id=(tenant or self.tenant).id, name="Fixture", type=ContentType.VIDEO,
                           file_path=path, folder_id=folder.id if folder else None)
        self.db.add(item)
        self.db.commit()
        return item

    def make_file(self, name="clip.mp4", data=b"0123456789"):
        path = self.root / self.tenant.id / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path, f"/media/{self.tenant.id}/{name}"

    def assert_content_exists(self, content_id):
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(ContentItem, content_id))

    def test_upload_normal_unique_physical_name_and_visible_metadata(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        path = self.root / body["file_path"].removeprefix("/media/")
        self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
        self.assertEqual(path.read_bytes(), b"complete media bytes")
        self.assertNotEqual(path.name, "clip.mp4")
        self.assertEqual(path.suffix, ".mp4")
        self.assertEqual(body["metadata_json"]["filename"], "clip.mp4")
        self.assertEqual(body["name"], "Visible name")

    def test_upload_rejects_unsafe_names_without_files_or_rows(self):
        # Direct UploadFile also tests Windows absolute names that HTTP clients may strip.
        absolute = Path(self.tmp.name) / "absolute.mp4"
        for name in ("../archivo.mp4", "..\\archivo.mp4", str(absolute), absolute.as_posix().split(":")[-1],
                     "%2e%2e%2farchivo.mp4", "%252e%252e%255carchivo.mp4", "", " ", "CON.mp4",
                     "clip.mp4:stream", "folder/clip.mp4", "clip.mp4.", "clip\x00.mp4"):
            with self.subTest(name=name):
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as raised:
                    contents.upload_content(file=UploadFile(BytesIO(b"fake"), filename=name), name="Fixture",
                        client_id=None, folder_id=None, duration_seconds=None, audio_kind=None,
                        normalization_status=None, target_lufs=None, db=self.db, current_user=self.user)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(raised.exception.detail, "INVALID_FILENAME")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_same_original_name_never_overwrites(self):
        first, second = self.upload(data=b"first"), self.upload(data=b"second")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        a, b = first.json()["file_path"], second.json()["file_path"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.http.get(a).content, b"first")
        self.assertEqual(self.http.get(b).content, b"second")

    def test_interrupted_upload_cleans_own_temp_only_and_creates_no_row(self):
        from fastapi import HTTPException
        class BrokenStream(BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("simulated write/input failure")
                return super().read(3)
        sentinel, _ = self.make_file("keep.mp4", b"keep")
        with self.assertRaises(HTTPException) as raised:
            contents.upload_content(file=UploadFile(BrokenStream(b"abcdef"), filename="clip.mp4"), name="Fixture",
                client_id=None, folder_id=None, duration_seconds=None, audio_kind=None,
                normalization_status=None, target_lufs=None, db=self.db, current_user=self.user)
        self.assertEqual(raised.exception.detail, "UPLOAD_WRITE_FAILED")
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [sentinel])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)

    def test_empty_upload_is_rejected(self):
        self.assertEqual(self.upload(data=b"").status_code, 400)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
        self.assertFalse(any(p.is_file() for p in self.root.rglob("*")))

    def test_delete_invalid_stored_paths_preserves_row_and_outside_file(self):
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        for stored in ("/media/../outside.mp4", "/media/%2e%2e/outside.mp4", "/media/..\\outside.mp4",
                       str(outside), "C:\\outside.mp4", "/media/C:/outside.mp4", "", "/media/"):
            with self.subTest(stored=stored):
                item = self.add_content(stored)
                response = self.http.delete(f"/api/contents/{item.id}")
                self.assertEqual(response.status_code, 409, response.text)
                self.assert_content_exists(item.id)
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_missing_file_fails_closed(self):
        item = self.add_content(f"/media/{self.tenant.id}/missing.mp4")
        response = self.http.delete(f"/api/contents/{item.id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "FILE_NOT_FOUND")
        self.assert_content_exists(item.id)

    def test_directory_is_not_a_media_file(self):
        path = self.root / self.tenant.id
        path.mkdir()
        item = self.add_content(f"/media/{self.tenant.id}")
        response = self.http.delete(f"/api/contents/{item.id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "PATH_IS_DIRECTORY")
        self.assertTrue(path.is_dir())
        self.assert_content_exists(item.id)

    def make_symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows requires symlink privilege / Developer Mode (1314)")
            raise

    def test_symlink_outside_root_is_never_read_or_deleted(self):
        outside = Path(self.tmp.name) / "external.mp4"
        outside.write_bytes(b"external")
        link = self.root / "escape.mp4"
        self.make_symlink(link, outside)
        item = self.add_content("/media/escape.mp4")
        self.assertEqual(self.http.get(item.file_path).status_code, 404)
        self.assertEqual(self.http.delete(f"/api/contents/{item.id}").status_code, 409)
        self.assertEqual(outside.read_bytes(), b"external")
        self.assert_content_exists(item.id)

    def test_upload_directory_symlink_cannot_escape_root(self):
        outside = Path(self.tmp.name) / "external"
        outside.mkdir()
        self.make_symlink(self.root / self.tenant.id, outside, directory=True)
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)

    def test_shared_leading_and_nonleading_media_path_survives_until_last_reference(self):
        path, url = self.make_file("shared clip.mp4")
        first = self.add_content(url)
        second = self.add_content(url.removeprefix("/"), tenant=self.other)
        self.assertEqual(self.http.delete(f"/api/contents/{first.id}").status_code, 204)
        self.assertTrue(path.exists())
        self.assert_content_exists(second.id)
        self.user.client_id = self.other.id
        self.assertEqual(self.http.delete(f"/api/contents/{second.id}").status_code, 204)
        self.assertFalse(path.exists())

    def test_delete_does_not_guess_related_files_by_stem(self):
        path, url = self.make_file()
        unrelated, _ = self.make_file("clip.jpg", b"unrelated")
        preview = self.root / "previews" / self.other.id / "clip.jpg"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"other tenant")
        item = self.add_content(url)
        self.assertEqual(self.http.delete(f"/api/contents/{item.id}").status_code, 204)
        self.assertFalse(path.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        self.assertEqual(preview.read_bytes(), b"other tenant")

    def test_folder_preflights_all_paths_before_deleting_anything(self):
        folder = ContentFolder(client_id=self.tenant.id, name="Fixtures")
        self.db.add(folder)
        self.db.commit()
        path, url = self.make_file()
        good = self.add_content(url, folder=folder)
        bad = self.add_content("/media/../escape.mp4", folder=folder)
        response = self.http.delete(f"/api/content-folders/{folder.id}?cascade=true")
        self.assertEqual(response.status_code, 409)
        self.assert_content_exists(good.id)
        self.assert_content_exists(bad.id)
        self.assertIsNotNone(self.db.get(ContentFolder, folder.id))
        self.assertTrue(path.exists())

    def test_folder_delete_preserves_shared_file_outside_folder(self):
        folder = ContentFolder(client_id=self.tenant.id, name="Fixtures")
        self.db.add(folder)
        self.db.commit()
        path, url = self.make_file()
        self.add_content(url, folder=folder)
        survivor = self.add_content(url)
        self.assertEqual(self.http.delete(f"/api/content-folders/{folder.id}?cascade=true").status_code, 204)
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor.id)

    def test_valid_media_get_head_range_and_headers_unchanged(self):
        _, url = self.make_file()
        get, head = self.http.get(url), self.http.head(url)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.content, b"0123456789")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        for key in ("etag", "last-modified", "content-type", "content-disposition", "content-length", "accept-ranges"):
            self.assertEqual(get.headers[key], head.headers[key])
        self.assertEqual(get.headers["content-type"], "video/mp4")
        self.assertTrue(get.headers["content-disposition"].startswith("inline;"))
        for value, expected in (("bytes=2-5", b"2345"), ("bytes=-3", b"789"), ("bytes=8-", b"89")):
            with self.subTest(range=value):
                result = self.http.get(url, headers={"Range": value})
                self.assertEqual(result.status_code, 206)
                self.assertEqual(result.content, expected)
                self.assertEqual(result.headers["etag"], get.headers["etag"])
                ranged_head = self.http.head(url, headers={"Range": value})
                self.assertEqual(ranged_head.status_code, 206)
                self.assertEqual(ranged_head.content, b"")
        for value in ("bytes=99-", "bytes=5-2", "bytes=0-1,3-4", "nonsense"):
            result = self.http.get(url, headers={"Range": value})
            self.assertEqual(result.status_code, 416)
            self.assertEqual(result.headers["content-range"], "bytes */10")

    def test_runtime_download_url_and_content_list_unchanged(self):
        path, url = self.make_file()
        item = self.add_content(url)
        payload = serialize_content(item, "https://api.example.test/")
        self.assertEqual(payload["file_path"], url)
        self.assertEqual(payload["download_url"], "https://api.example.test" + url)
        self.assertEqual(payload["size"], path.stat().st_size)
        listing = self.http.get("/api/contents/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["file_path"], url)

    def test_non_file_content_still_deletes(self):
        item = self.add_content(None)
        self.assertEqual(self.http.delete(f"/api/contents/{item.id}").status_code, 204)

    def test_foreign_tenant_delete_is_denied(self):
        path, url = self.make_file()
        item = self.add_content(url, tenant=self.other)
        self.assertIn(self.http.delete(f"/api/contents/{item.id}").status_code, (403, 404))
        self.assertTrue(path.exists())

    def test_admin_cleanup_invalid_path_aborts_without_partial_commits(self):
        from app.services.cleanup_retention import run_retention_cleanup
        old = datetime.now(UTC) - timedelta(days=40)
        item = self.add_content("/media/../escape.mp4")
        item.created_at = old
        campaign = Campaign(client_id=self.tenant.id, name="Unreferenced fixture", created_at=old)
        self.db.add(campaign)
        self.db.commit()
        campaign_id, item_id = campaign.id, item.id
        result = run_retention_cleanup(self.db)
        self.assertTrue(result["errors"])
        self.assertEqual(result["contents_deleted"], 0)
        self.assertEqual(result["campaigns_deleted"], 0)
        self.assert_content_exists(item_id)
        self.assertIsNotNone(self.db.get(Campaign, campaign_id))

    def test_client_delete_preflights_paths_before_cascade(self):
        from fastapi import HTTPException
        from app.services.client_cleanup import delete_client_cascade
        item = self.add_content("/media/../escape.mp4")
        item_id, tenant_id = item.id, self.tenant.id
        with self.assertRaises(HTTPException) as raised:
            delete_client_cascade(self.tenant, self.db)
        self.assertEqual(raised.exception.status_code, 409)
        self.assert_content_exists(item_id)
        self.assertIsNotNone(self.db.get(Client, tenant_id))

    def test_client_delete_preserves_file_referenced_by_other_client(self):
        from app.services.client_cleanup import delete_client_cascade
        path, url = self.make_file()
        self.add_content(url)
        survivor = self.add_content(url, tenant=self.other)
        survivor_id = survivor.id
        delete_client_cascade(self.tenant, self.db)
        self.assertEqual(path.read_bytes(), b"0123456789")
        self.assert_content_exists(survivor_id)

    def test_collision_does_not_overwrite_even_if_identifier_generator_repeats(self):
        from app.services.media_storage import write_media_upload
        existing, _ = self.make_file("repeated.mp4", b"previous")
        identifiers = [SimpleNamespace(hex="repeated"), SimpleNamespace(hex="new")]
        with patch("app.services.media_storage.uuid4", side_effect=identifiers):
            uploaded = write_media_upload(self.root, (self.tenant.id,), "clip.mp4", "video/mp4", BytesIO(b"new bytes"))
        self.assertEqual(existing.read_bytes(), b"previous")
        self.assertEqual(uploaded.physical_path.read_bytes(), b"new bytes")
        self.assertEqual(uploaded.physical_path.name, "new.mp4")
        self.assertFalse(list(self.root.rglob(".upload-*")))

    def test_atomic_promotion_failure_never_falls_back_to_overwrite(self):
        sentinel, _ = self.make_file("clip.mp4", b"keep")
        with patch("app.services.media_storage.os.link", side_effect=OSError("fixture failure")):
            response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "UPLOAD_WRITE_FAILED")
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
        self.assertFalse(list(self.root.rglob(".upload-*")))

    def test_db_failure_does_not_leave_valid_row_or_delete_complete_media(self):
        from fastapi import HTTPException
        from sqlalchemy.exc import SQLAlchemyError
        with patch.object(self.db, "commit", side_effect=SQLAlchemyError("fixture failure")):
            with self.assertRaises(HTTPException) as raised:
                contents.upload_content(file=UploadFile(BytesIO(b"complete"), filename="clip.mp4"), name="Fixture",
                    client_id=None, folder_id=None, duration_seconds=None, audio_kind=None,
                    normalization_status=None, target_lufs=None, db=self.db, current_user=self.user)
        self.assertEqual(raised.exception.detail, "UPLOAD_WRITE_FAILED")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
        files = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"complete")  # Complete orphan, never a partial referenced file.

    def test_physical_cleanup_preflights_whole_batch(self):
        from app.services.media_storage import MediaPathError
        good, _ = self.make_file()
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        with self.assertRaises(MediaPathError) as raised:
            cleanup_files([good, outside], self.db)
        self.assertEqual(raised.exception.code, "PATH_OUTSIDE_MEDIA_ROOT")
        self.assertTrue(good.exists())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_same_root_symlink_alias_is_counted_as_shared_reference(self):
        path, url = self.make_file()
        alias = self.root / "alias.mp4"
        self.make_symlink(alias, path)
        first = self.add_content(url)
        second = self.add_content("/media/alias.mp4", tenant=self.other)
        self.assertEqual(self.http.delete(f"/api/contents/{first.id}").status_code, 204)
        self.assertTrue(path.exists())
        self.assert_content_exists(second.id)

    def test_upload_symlink_to_other_tenant_inside_root_is_rejected(self):
        other_dir = self.root / self.other.id
        other_dir.mkdir()
        self.make_symlink(self.root / self.tenant.id, other_dir, directory=True)
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(list(other_dir.iterdir()), [])

    def test_client_tree_symlink_aborts_cascade_and_preserves_all_files(self):
        from fastapi import HTTPException
        from app.services.client_cleanup import delete_client_cascade
        path, url = self.make_file()
        item = self.add_content(url)
        item_id = item.id
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "keep.mp4"
        sentinel.write_bytes(b"keep")
        self.make_symlink(path.parent / "redirect", outside, directory=True)
        with self.assertRaises(HTTPException):
            delete_client_cascade(self.tenant, self.db)
        self.assertTrue(path.exists())
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assert_content_exists(item_id)

    def test_admin_cleanup_preserves_shared_file_then_removes_last_reference(self):
        from app.services.cleanup_retention import run_retention_cleanup
        path, url = self.make_file()
        first = self.add_content(url)
        first.created_at = datetime.now(UTC) - timedelta(days=40)
        survivor = self.add_content(url, tenant=self.other)
        result = run_retention_cleanup(self.db)
        self.assertEqual(result["contents_deleted"], 1)
        self.assertEqual(result["files_deleted"], 0)
        self.assertEqual(result["errors"], [])
        self.assertTrue(path.exists())
        survivor.created_at = datetime.now(UTC) - timedelta(days=40)
        self.db.commit()
        result = run_retention_cleanup(self.db)
        self.assertEqual(result["contents_deleted"], 1)
        self.assertEqual(result["files_deleted"], 1)
        self.assertEqual(result["errors"], [])
        self.assertFalse(path.exists())

    def test_dataset_upload_uses_same_safe_storage_without_changing_import(self):
        source = DataSource(client_id=self.tenant.id, name="Fixture", source_type=DataSourceType.FILE_UPLOAD)
        self.db.add(source)
        self.db.flush()
        dataset = Dataset(client_id=self.tenant.id, data_source_id=source.id, name="Fixture", slug="fixture")
        self.db.add(dataset)
        self.db.commit()
        response = self.http.post(f"/api/data-sources/datasets/{dataset.id}/imports/upload",
                                  files={"file": ("data.csv", b"name,price\nTest,10\n", "text/csv")})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["source_filename"], "data.csv")
        self.assertEqual(body["row_count"], 1)
        self.assertEqual(body["import_status"], "completed")
        path = self.root / body["storage_path"]
        self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
        self.assertNotEqual(path.name, "data.csv")
        self.assertEqual(path.read_bytes(), b"name,price\nTest,10\n")
        before = self.db.scalar(select(func.count()).select_from(DatasetImport))
        bad = self.http.post(f"/api/data-sources/datasets/{dataset.id}/imports/upload",
                             files={"file": ("../escape.csv", b"name\nTest\n", "text/csv")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(DatasetImport)), before)

    def test_player_activation_runtime_heartbeat_and_events_contract_smoke(self):
        branch = Branch(client_id=self.tenant.id, name="Fixture", code="FIX")
        self.db.add(branch)
        self.db.flush()
        channel = Channel(client_id=self.tenant.id, branch_id=branch.id, name="Fixture")
        self.db.add(channel)
        self.db.commit()
        activated = self.http.post("/api/player/activate", json={"channel_code": channel.channel_code,
            "hardware_id": "fixture-hardware", "device_name": "Fixture", "app_version": "fixture"})
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(set(activated.json()), {"player_token", "channel_id", "client_id", "branch_id", "activation_status"})
        headers = {"Authorization": "Bearer " + activated.json()["player_token"]}
        runtime = self.http.get("/api/player/runtime-config", headers=headers)
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["runtime_version"], 3)
        self.assertEqual(runtime.json()["media_retention"]["schema_version"], 1)
        heartbeat = self.http.post("/api/player/heartbeat", headers=headers, json={"status": "online"})
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        self.assertEqual(set(heartbeat.json()), {"ok", "received_at", "channel_id"})
        playback = self.http.post("/api/player/playback-events", headers=headers,
                                   json={"started_at": datetime.now(UTC).isoformat()})
        self.assertEqual(playback.status_code, 201, playback.text)
        self.assertEqual(set(playback.json()), {"event_id", "recorded_at", "channel_id"})


    def asgi_media_request(self, raw_path, method="GET"):
        # Starlette's pinned TestClient double-decodes httpx.URL.path. Use the
        # ASGI transport (one decode, like Uvicorn), never compensate in app code.
        import asyncio
        import httpx
        async def request():
            transport = httpx.ASGITransport(app=self.http.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, raw_path)
        return asyncio.run(request())

    def test_stored_percent_paths_are_literal_and_delete_only_exact_file(self):
        from app.services.media_storage import resolve_stored_media_path
        space, _ = self.make_file("video final.mp4", b"space")
        percent, url = self.make_file("video%20final.mp4", b"percent")
        nested, nested_url = self.make_file("video%2520final.mp4", b"nested")
        self.assertEqual(resolve_stored_media_path(self.root, url), percent.resolve())
        self.assertEqual(resolve_stored_media_path(self.root, nested_url), nested.resolve())
        item = self.add_content(url)
        self.assertEqual(self.http.delete(f"/api/contents/{item.id}").status_code, 204)
        self.assertFalse(percent.exists())
        self.assertEqual(space.read_bytes(), b"space")
        self.assertEqual(nested.read_bytes(), b"nested")

    def test_http_percent_space_and_nested_percent_coexist_get_head(self):
        from urllib.parse import quote
        for name, data in (("video final.mp4", b"space"), ("video%20final.mp4", b"percent"),
                           ("video%2520final.mp4", b"nested")):
            _, url = self.make_file(name, data)
            encoded = quote(url, safe="/")
            for method in ("GET", "HEAD"):
                with self.subTest(name=name, method=method):
                    response = self.asgi_media_request(encoded, method)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, data if method == "GET" else b"")
                    self.assertEqual(response.headers["content-length"], str(len(data)))

    def test_http_encoded_traversal_returns_404(self):
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        for path in ("/media/%2e%2e/outside.mp4", "/media/..%2foutside.mp4",
                     "/media/%2e%2e%5coutside.mp4", "/media/%2Foutside.mp4"):
            for method in ("GET", "HEAD"):
                self.assertEqual(self.asgi_media_request(path, method).status_code, 404)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_http_direct_traversal_reaches_route_and_returns_404(self):
        import asyncio
        import httpx
        # HTTP libraries normalize ../ before transport; construct the ASGI
        # scope a server would receive for an unnormalized raw HTTP target.
        async def app(scope, receive, send):
            scope = dict(scope, path="/media/../outside.mp4", raw_path=b"/media/../outside.mp4")
            await self.http.app(scope, receive, send)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.get("/")
        with patch("app.api.routes.media.resolve_media_path", wraps=media.resolve_media_path) as resolver:
            self.assertEqual(asyncio.run(request()).status_code, 404)
            resolver.assert_called_once_with(self.root, "../outside.mp4")

    def test_stored_unsupported_path_formats_stay_rejected(self):
        from app.services.media_storage import MediaPathError, resolve_stored_media_path
        path, url = self.make_file()
        self.assertEqual(resolve_stored_media_path(self.root, url[1:]), path.resolve())
        for value in (url.removeprefix("/media/"), str(path), path.as_posix()):
            with self.assertRaises(MediaPathError):
                resolve_stored_media_path(self.root, value)

    def test_upload_final_suffix_preserves_mime_for_supported_formats(self):
        import mimetypes
        for suffix in ("mp4", "mov", "webm", "wmv", "m2v", "jpg", "jpeg", "png", "webp", "safeunknown"):
            with self.subTest(suffix=suffix):
                expected = mimetypes.guess_type("fixture." + suffix)[0] or "application/octet-stream"
                response = self.upload("original.part." + suffix.upper())
                self.assertEqual(response.status_code, 201, response.text)
                url = response.json()["file_path"]
                self.assertTrue(url.endswith("." + suffix), url)
                streamed = self.http.get(url)
                self.assertEqual(streamed.headers["content-type"], expected)
                if suffix in ("wmv", "m2v") and mimetypes.guess_type("fixture." + suffix)[0]:
                    self.assertNotEqual(streamed.headers["content-type"], "application/octet-stream")
                # Runtime metadata is still the sender's MIME, not guessed bytes.
                item = self.db.get(ContentItem, response.json()["id"])
                self.assertEqual(serialize_content(item, "http://testserver")["mime_type"], "video/mp4")

    def test_upload_unsafe_or_long_suffix_rejected(self):
        from app.services.media_storage import MediaPathError, write_media_upload
        for filename in ("clip." + "x" * 17, "clip.bad%20ext", "clip.bad/ext", "clip.bad\\ext"):
            with self.assertRaises(MediaPathError):
                write_media_upload(self.root, (self.tenant.id,), filename, "video/mp4", BytesIO(b"fixture"))
        self.assertFalse(any(p.is_file() for p in self.root.rglob("*")))

    def test_create_reference_checks_file_and_commits_under_lock(self):
        path, url = self.make_file()
        response = self.http.post("/api/contents/", json={"name": "Reference", "type": "video", "file_path": url})
        self.assertEqual(response.status_code, 201, response.text)
        path.unlink()  # Fixture only: simulate a stale declarative reference.
        failed = self.http.post("/api/contents/", json={"name": "Missing", "type": "video", "file_path": url})
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 1)

    def test_file_path_update_uses_same_protocol_and_preserves_old_on_failure(self):
        from app.services.media_references import save_content_reference
        from app.services.media_storage import MediaPathError
        old, url = self.make_file()
        new, new_url = self.make_file("second.mp4")
        item = self.add_content(url)
        item.file_path = new_url
        save_content_reference(item, self.db)
        self.db.refresh(item)
        self.assertEqual(item.file_path, new_url)
        self.assertTrue(old.exists())
        item.file_path = new_url + "missing"
        with self.assertRaises(MediaPathError):
            save_content_reference(item, self.db)
        self.db.refresh(item)
        self.assertEqual(item.file_path, new_url)
        self.assertTrue(new.exists())

    def test_delete_db_failure_never_performs_provisional_filesystem_action(self):
        from sqlalchemy.exc import SQLAlchemyError
        path, url = self.make_file()
        item = self.add_content(url)
        item_id = item.id
        with patch.object(self.db, "commit", side_effect=SQLAlchemyError("fixture failure")), \
             patch.object(Path, "unlink") as unlink, patch.object(Path, "rename") as rename:
            with self.assertRaises(SQLAlchemyError):
                contents.delete_content(item_id, db=self.db, current_user=self.user)
            unlink.assert_not_called()
            rename.assert_not_called()
        self.assert_content_exists(item_id)
        self.assertEqual(path.read_bytes(), b"0123456789")

    def test_postcommit_unlink_failure_keeps_complete_orphan_without_live_row(self):
        path, url = self.make_file()
        item = self.add_content(url)
        item_id = item.id
        with patch.object(Path, "unlink", side_effect=OSError("fixture failure")):
            response = self.http.delete(f"/api/contents/{item_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "MEDIA_DELETE_FAILED")
        self.db.expire_all()
        self.assertIsNone(self.db.get(ContentItem, item_id))
        self.assertEqual(path.read_bytes(), b"0123456789")

    def test_flushed_delete_cannot_unlink_before_confirmed_commit(self):
        from app.services.media_references import media_reference_operation
        from app.services.media_storage import MediaPathError
        path, url = self.make_file()
        item = self.add_content(url)
        item_id = item.id
        with self.assertRaises(MediaPathError) as caught:
            with media_reference_operation(self.db):
                targets = delete_content_items([item], self.db)
                # The SQL DELETE is flushed, but the transaction can still fail.
                cleanup_files(targets, self.db)
        self.assertEqual(caught.exception.code, "MEDIA_CLEANUP_REQUIRES_COMMIT")
        self.assert_content_exists(item_id)
        self.assertTrue(path.exists())

    def test_cli_reports_actual_deleted_files_when_shared_file_kept(self):
        from contextlib import nullcontext, redirect_stdout
        from io import StringIO
        from app.scripts import cleanup_unused_assets as cli
        path, url = self.make_file()
        old = self.add_content(url)
        old.created_at = datetime.now(UTC) - timedelta(days=40)
        survivor = self.add_content(url, tenant=self.other)
        output = StringIO()
        with patch.object(cli, "SessionLocal", return_value=nullcontext(self.db)), \
             patch.object(cli, "parse_args", return_value=SimpleNamespace(apply=True, days=5)), redirect_stdout(output):
            cli.main()  # Injected memory session and temporary files only.
        self.assertIn("1 content item(s) and 0 file(s)", output.getvalue())
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor.id)

    def test_literal_paths_reject_dangerous_raw_components(self):
        from app.services.media_storage import MediaPathError, resolve_relative_media_path, resolve_stored_media_path
        for value in ("", ".", "..", "folder//clip.mp4", "folder/./clip.mp4", "folder/../clip.mp4",
                      "folder/", "/clip.mp4", "C:clip.mp4", "C:/clip.mp4", "folder/C:clip.mp4",
                      "//server/share/clip.mp4", "folder\\clip.mp4", "clip\x00.mp4"):
            for resolver, path in ((resolve_relative_media_path, value),
                                   (resolve_stored_media_path, "/media/" + value)):
                with self.subTest(path=value, resolver=resolver.__name__), self.assertRaises(MediaPathError):
                    resolver(self.root, path, must_exist=False)

    def test_windows_stored_names_cannot_select_ads_or_filename_aliases(self):
        if os.name != "nt":
            self.skipTest("Windows-specific filename restrictions")
        from app.services.media_storage import MediaPathError, resolve_stored_media_path
        for value in ("video:final.mp4", "clip.mp4:stream", "CON.mp4", "video.mp4.", "video.mp4 "):
            with self.subTest(value=value), self.assertRaises(MediaPathError):
                resolve_stored_media_path(self.root, "/media/" + value, must_exist=False)

    def unresolved_survivor_fixture(self, *, folder=None, old=False):
        path, url = self.make_file()
        candidate = self.add_content(url, folder=folder)
        if old:
            candidate.created_at = datetime.now(UTC) - timedelta(days=40)
        # Deliberately corrupt legacy data bypasses the protected reference writer.
        survivor = self.add_content("/media/../sensitive-fixture.mp4", tenant=self.other)
        return path, candidate.id, survivor.id

    def test_unresolved_reference_preserves_whole_batch_and_logs_codes_only(self):
        from app.services.media_references import media_reference_operation
        from app.services.media_storage import MediaPathError
        path, candidate_id, survivor_id = self.unresolved_survivor_fixture()
        second, _ = self.make_file("second.mp4")
        with self.assertLogs("app.services.content_cleanup", level="WARNING") as logs:
            with self.assertRaises(MediaPathError) as raised:
                with media_reference_operation(self.db):
                    self.db.delete(self.db.get(ContentItem, candidate_id))
                    self.db.commit()
                    cleanup_files([path, second], self.db)
        self.assertEqual(raised.exception.code, "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertIn("cause=PATH_OUTSIDE_MEDIA_ROOT", " ".join(logs.output))
        self.assertNotIn("sensitive-fixture", " ".join(logs.output))
        self.assertNotIn(str(self.root), " ".join(logs.output))
        self.assertTrue(path.exists())
        self.assertTrue(second.exists())
        self.assert_content_exists(survivor_id)

    def test_content_delete_unresolved_survivor_fails_closed(self):
        path, candidate_id, survivor_id = self.unresolved_survivor_fixture()
        response = self.http.delete(f"/api/contents/{candidate_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor_id)
        self.assertIsNone(self.db.get(ContentItem, candidate_id))  # SQL already committed; orphan is preserved.

    def test_folder_delete_unresolved_survivor_fails_closed(self):
        folder = ContentFolder(client_id=self.tenant.id, name="Fixture folder")
        self.db.add(folder)
        self.db.commit()
        path, _, survivor_id = self.unresolved_survivor_fixture(folder=folder)
        response = self.http.delete(f"/api/content-folders/{folder.id}?cascade=true")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor_id)

    def test_client_delete_unresolved_survivor_fails_closed(self):
        from fastapi import HTTPException
        from app.services.client_cleanup import delete_client_cascade
        path, candidate_id, survivor_id = self.unresolved_survivor_fixture()
        tenant_id, other_id = self.tenant.id, self.other.id
        with self.assertRaises(HTTPException) as raised:
            delete_client_cascade(self.tenant, self.db)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor_id)
        self.assertIsNotNone(self.db.get(Client, other_id))
        self.assertIsNone(self.db.get(Client, tenant_id))
        self.assertIsNone(self.db.get(ContentItem, candidate_id))

    def test_admin_cleanup_unresolved_survivor_fails_closed(self):
        from app.services.cleanup_retention import run_retention_cleanup
        path, _, survivor_id = self.unresolved_survivor_fixture(old=True)
        result = run_retention_cleanup(self.db)
        self.assertEqual(result["files_deleted"], 0)
        self.assertIn("UNRESOLVED_SURVIVING_REFERENCE", str(result["errors"]))
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor_id)

    def test_cli_cleanup_unresolved_survivor_fails_closed(self):
        from contextlib import nullcontext, redirect_stdout
        from io import StringIO
        from app.scripts import cleanup_unused_assets as cli
        from app.services.media_storage import MediaPathError
        path, _, survivor_id = self.unresolved_survivor_fixture(old=True)
        with patch.object(cli, "SessionLocal", return_value=nullcontext(self.db)), \
             patch.object(cli, "parse_args", return_value=SimpleNamespace(apply=True, days=5)), \
             redirect_stdout(StringIO()), self.assertRaises(MediaPathError) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertTrue(path.exists())
        self.assert_content_exists(survivor_id)
