"""Linux regressions using temporary files, memory SQLite and the real media route."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
import os

from app.services.media_storage import MediaPathError, normalize_upload_filename, resolve_relative_media_path, resolve_stored_media_path, write_media_upload


@skipUnless(os.name == "posix", "Historical POSIX filenames must be tested on Linux")
class HistoricalPosixMediaTests(TestCase):
    def test_reserved_windows_name_is_valid_historical_posix_file(self):
        self.check_historical("CON.mp4")

    def test_trailing_dot_is_valid_historical_posix_file(self):
        self.check_historical("video.mp4.")

    def test_colon_is_valid_historical_posix_file(self):
        self.check_historical("video:final.mp4")

    def test_percent_and_space_are_distinct_literal_stored_paths(self):
        with TemporaryDirectory(prefix="tecno-media-posix-") as temporary:
            root = Path(temporary)
            names = ("video%20final.mp4", "video final.mp4", "video%2520final.mp4", "archivo%20literal.mp4")
            for name in names:
                (root / name).write_bytes(name.encode())
            for name in names:
                self.assertEqual(resolve_stored_media_path(root, "/media/" + name).read_bytes(), name.encode())
                self.assertEqual(resolve_relative_media_path(root, name).read_bytes(), name.encode())

    def check_historical(self, name):
        with TemporaryDirectory(prefix="tecno-media-posix-") as temporary:
            root = Path(temporary)
            path = root / name
            path.write_bytes(b"historical fixture")
            self.assertEqual(resolve_stored_media_path(root, "/media/" + name), path)
            self.assertEqual(resolve_relative_media_path(root, name), path)
            self.assertEqual(path.read_bytes(), b"historical fixture")
            with self.assertRaises(MediaPathError):
                normalize_upload_filename(name)

    def test_same_directory_hardlink_and_real_upload_promotion(self):
        with TemporaryDirectory(prefix="tecno-media-hardlink-") as temporary:
            root = Path(temporary)
            source, linked = root / "fixture.tmp", root / "fixture.link"
            source.write_bytes(b"hardlink fixture")
            os.link(source, linked)
            self.assertEqual(source.stat().st_ino, linked.stat().st_ino)
            self.assertEqual(source.stat().st_dev, linked.stat().st_dev)
            source.unlink()
            self.assertEqual(linked.read_bytes(), b"hardlink fixture")
            linked.unlink()
            upload = write_media_upload(root, ("fixture",), "original.WMV", "video/x-ms-wmv", BytesIO(b"upload fixture"))
            self.assertEqual(upload.physical_path.suffix, ".wmv")
            self.assertEqual(upload.physical_path.read_bytes(), b"upload fixture")
            self.assertEqual(list(upload.physical_path.parent.glob(".upload-*")), [])


@skipUnless(os.name == "posix", "Actual historical Linux paths require Linux")
class PosixMediaIntegrationTests(TestCase):
    def setUp(self):
        if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
            raise RuntimeError("Configure isolated memory DB and temporary MEDIA_ROOT before importing app modules")
        from unittest.mock import patch
        from fastapi import FastAPI
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool
        from app.core.config import get_settings
        from app.models.base import Base
        from app.models.entities import Client
        from app.api.routes import media

        temporary = TemporaryDirectory(prefix="tecno-posix-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "media"
        self.root.mkdir()
        settings_patch = patch.object(get_settings(), "MEDIA_ROOT", self.root)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
        Base.metadata.create_all(self.engine)  # Only this verified empty, memory fixture DB.
        self.db = Session(self.engine, autoflush=False)
        self.addCleanup(self.db.close)
        self.client_a, self.client_b = Client(name="Fixture A", slug="a"), Client(name="Fixture B", slug="b")
        self.db.add_all([self.client_a, self.client_b])
        self.db.commit()
        self.app = FastAPI()  # No app.main, lifespan or server startup.
        self.app.include_router(media.router)

    def request(self, raw_path, method="GET", headers=()):
        # One HTTP URL decode, matching the ASGI server boundary, with no client
        # normalization of ../ and no TestClient double-decode workaround in app code.
        import asyncio
        from urllib.parse import unquote
        result = {"status": None, "headers": {}, "body": b""}

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                result["headers"] = {key.decode(): value.decode() for key, value in message["headers"]}
            elif message["type"] == "http.response.body":
                result["body"] += message.get("body", b"")

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": method, "scheme": "http", "path": unquote(raw_path),
                 "raw_path": raw_path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"host", b"testserver"), *headers],
                 "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
        asyncio.run(self.app(scope, receive, send))
        return result

    def add_content(self, client, stored_path):
        from app.models.entities import ContentItem, ContentType
        item = ContentItem(client_id=client.id, name="Fixture", type=ContentType.VIDEO, file_path=stored_path)
        self.db.add(item)
        self.db.commit()
        return item.id

    def test_colon_cross_client_cascade_preserves_surviving_reference_and_http(self):
        from app.models.entities import Client, ContentItem
        from app.services.client_cleanup import delete_client_cascade
        a_id, b_id = self.client_a.id, self.client_b.id
        path = self.root / a_id / "video:final.mp4"
        path.parent.mkdir()
        path.write_bytes(b"colon historical fixture")
        url = f"/media/{a_id}/video:final.mp4"
        a_item = self.add_content(self.client_a, url)
        b_item = self.add_content(self.client_b, url)
        self.assertEqual(resolve_stored_media_path(self.root, url), path)
        for method in ("GET", "HEAD"):
            self.assertEqual(self.request(url, method)["status"], 200)
        delete_client_cascade(self.client_a, self.db)  # Actual service, including central cleanup.
        self.db.expire_all()
        self.assertIsNone(self.db.get(Client, a_id))
        self.assertIsNone(self.db.get(ContentItem, a_item))
        self.assertIsNotNone(self.db.get(Client, b_id))
        self.assertEqual(self.db.get(ContentItem, b_item).file_path, url)
        self.assertEqual(path.read_bytes(), b"colon historical fixture")
        for method in ("GET", "HEAD"):
            response = self.request(url, method)
            self.assertEqual(response["status"], 200)
            self.assertEqual(response["body"], path.read_bytes() if method == "GET" else b"")

    def test_historical_names_http_and_stored_paths_select_exact_file(self):
        from urllib.parse import quote
        names = ("video:final.mp4", "CON.mp4", "video.mp4.", "video%20final.mp4",
                 "video final.mp4", "archivo%20literal.mp4", "video%2520final.mp4")
        for name in names:
            (self.root / name).write_bytes(name.encode())
        for name in names:
            self.assertEqual(resolve_stored_media_path(self.root, "/media/" + name), self.root / name)
            for method in ("GET", "HEAD"):
                with self.subTest(name=name, method=method):
                    response = self.request("/media/" + quote(name, safe=""), method)
                    self.assertEqual(response["status"], 200)
                    self.assertEqual(response["body"], name.encode() if method == "GET" else b"")
                    self.assertEqual(response["headers"]["content-length"], str(len(name.encode())))

    def test_http_dangerous_paths_and_external_symlink_stay_404(self):
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"outside fixture")
        (self.root / "escape.mp4").symlink_to(outside)
        for url in ("/media/../outside.mp4", "/media/%2e%2e/outside.mp4", "/media/..%2foutside.mp4",
                    "/media/%2e%2e%5coutside.mp4", "/media/escape.mp4", "/media//outside.mp4",
                    "/media/C:/outside.mp4", "/media/a/./outside.mp4", "/media/a//outside.mp4",
                    "/media/bad%00.mp4"):
            for method in ("GET", "HEAD"):
                with self.subTest(url=url, method=method):
                    self.assertEqual(self.request(url, method)["status"], 404)
        self.assertEqual(outside.read_bytes(), b"outside fixture")

    def test_unresolved_external_symlink_reference_preserves_candidate(self):
        from fastapi import HTTPException
        from app.models.entities import ContentItem
        from app.services.client_cleanup import delete_client_cascade
        path = self.root / self.client_a.id / "candidate.mp4"
        path.parent.mkdir()
        path.write_bytes(b"candidate fixture")
        self.add_content(self.client_a, f"/media/{self.client_a.id}/candidate.mp4")
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"outside fixture")
        (self.root / "external.mp4").symlink_to(outside)
        survivor_id = self.add_content(self.client_b, "/media/external.mp4")
        with self.assertRaises(HTTPException) as raised:
            delete_client_cascade(self.client_a, self.db)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "UNRESOLVED_SURVIVING_REFERENCE")
        self.assertEqual(path.read_bytes(), b"candidate fixture")
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(ContentItem, survivor_id))

    def test_upload_suffix_mime_get_head_and_range_on_linux(self):
        import mimetypes
        for suffix in ("mp4", "mov", "webm", "wmv", "m2v", "jpg", "jpeg", "png", "webp"):
            upload = write_media_upload(self.root, ("uploads",), "original." + suffix.upper(),
                                        "application/octet-stream", BytesIO(b"0123456789"))
            self.assertEqual(upload.physical_path.suffix, "." + suffix)
            url = "/media/" + upload.physical_path.relative_to(self.root).as_posix()
            expected = mimetypes.guess_type("original." + suffix)[0] or "application/octet-stream"
            for method in ("GET", "HEAD"):
                response = self.request(url, method)
                self.assertEqual(response["status"], 200)
                self.assertEqual(response["headers"]["content-type"], expected)
                partial = self.request(url, method, [(b"range", b"bytes=0-3")])
                self.assertEqual(partial["status"], 206)
                self.assertEqual(partial["headers"]["content-range"], "bytes 0-3/10")
                self.assertEqual(partial["body"], b"0123" if method == "GET" else b"")
                self.assertEqual(self.request(url, method, [(b"range", b"bytes=99-")])["status"], 416)
