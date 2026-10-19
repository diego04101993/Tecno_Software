"""SECURITY-1C: real routes/storage, disposable files and verified memory SQLite.

Configure DATABASE_URL=sqlite://, AUTO_SEED=false and a temporary MEDIA_ROOT
before discovery. Never import app.main or start its lifespan for these tests.
"""
import gzip
import os
from datetime import UTC, datetime
from email.utils import formatdate
from hashlib import sha256
from mimetypes import guess_type
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import quote

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Configure memory SQLite and temporary MEDIA_ROOT before importing app modules")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.routes import contents, media
from app.core.config import get_settings
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignChannelAssignment, CampaignSequenceItem,
    CampaignSequenceItemType, Channel, Client, ContentItem, ContentType, UserRole,
)
from app.services.player_runtime import build_player_runtime_config, serialize_content
from app.services.active_media_policy import ActiveMediaPolicy, classify_active_media


HTML = b"<!doctype html><script>window.__SECURITY_1C_HTML__=1</script>"
XHTML = (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
         b'<body onload="window.__SECURITY_1C_XHTML__=1">test</body></html>')
DOTFILE_XHTML = XHTML.replace(b"__SECURITY_1C_XHTML__", b"__SECURITY_1C_DOTFILE__")
SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80">'
SVG_FIXTURES = {
    "simple": SVG_OPEN + '<rect width="160" height="80" fill="green"/></svg>',
    "onload": SVG_OPEN.replace('width="160"', 'onload="window.__SECURITY_1C_SVG__=1" width="160"', 1)
        + '<rect width="160" height="80" fill="green"/></svg>',
    "script": SVG_OPEN + '<script>window.__SECURITY_1C_SVG__=1</script><rect width="160" height="80" fill="green"/></svg>',
    "external": SVG_OPEN + '<image href="http://127.0.0.1:9/qa-only.png" width="80" height="80"/></svg>',
    "style": SVG_OPEN + '<style>.tile { fill: green }</style><rect class="tile" width="160" height="80"/></svg>',
    "data": SVG_OPEN + '<image width="80" height="80" href="data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII="/></svg>',
    "internal": SVG_OPEN + '<defs><rect id="tile" width="160" height="80" fill="green"/></defs><use href="#tile"/></svg>',
}
EXPECTED_SVG_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
ENCODING_SUFFIXES = (".gz", ".Z", ".bz2", ".xz", ".br")
HTML_NAMES = sorted({variant for suffix in (".html", ".htm", ".xhtml", ".xht")
                     for encoding in ("", *ENCODING_SUFFIXES)
                     for name in ("a" + suffix + encoding,)
                     for variant in (name, name.lower(), name.upper())})
SVG_NAMES = sorted({variant for suffix in (".svg", ".svgz")
                    for encoding in ("", *ENCODING_SUFFIXES)
                    for name in ("a" + suffix + encoding,)
                    for variant in (name, name.lower(), name.upper())})
HTML_DOTFILE_NAMES = sorted({variant for suffix in (".html", ".htm", ".xhtml", ".xht")
                            for encoding in ("", *ENCODING_SUFFIXES)
                            for name in (suffix + encoding,)
                            for variant in (name, name.lower(), name.upper())}
                           | {".xhtml.GZ", ".XHTML.gz", ".XhT.Br"})
SVG_DOTFILE_NAMES = sorted({variant for suffix in (".svg", ".svgz")
                           for encoding in ("", *ENCODING_SUFFIXES)
                           for name in (suffix + encoding,)
                           for variant in (name, name.lower(), name.upper())} | {".SvGz.Br"})
EXTRA_HTML_NAMES = ("index.html", "index.htm", "index.xhtml", "index.xht", "index.HTML",
                    "index.html.gz", "index.xhtml.gz", "index.xht.br", "a..xhtml",
                    "a..xhtml.gz", "..xhtml", "..xhtml.gz", "...xhtml.gz")
EXTRA_SVG_NAMES = ("image.svg", "image.svgz", "image.SVG", "image.svg.gz", "image.svgz.gz")
WEIRD_NORMAL_NAMES = (".xhtml.", ".xhtml.gz.", ".xhtml .gz", ".xhtml. gz", ".xhtml.gz ")
NORMAL_NAMES = ("a.png", "a.png.gz", "a.tar.gz", "a.xhtml.txt", "a.xhtml.gz.exe",
                "a.svg.backup.gz", "ahtml.gz", "text.xhtml.backup.gz", "a.xhtml.gz.gz",
                "a.svg.gz.br", "a.xml", "a.xsl", "html-logo.png", "no-extension",
                "html", "xhtml", "svg", "index.xhtml.txt", "index.svg.txt",
                "index.xhtml.gz.exe", "index.svg.gz.exe", "index.xhtml.backup.gz",
                "index.svg.backup.gz", "archive.tar.gz", "photo.png.gz", "document.pdf.br",
                "asvg.gz", ".gz", ".br", "index.xhtml.gz.br", "index.svg.gz.br",
                "foo.xhtml.backup", "foo.xhtml.backup.gz", "fooxhtml", "xhtml.txt")
CLASSIFICATION_CASES = ([(name, ActiveMediaPolicy.HTML_INERT)
                         for name in (*HTML_NAMES, *HTML_DOTFILE_NAMES, *EXTRA_HTML_NAMES)]
                        + [(name, ActiveMediaPolicy.SVG_RESTRICTED)
                           for name in (*SVG_NAMES, *SVG_DOTFILE_NAMES, *EXTRA_SVG_NAMES)]
                        + [(name, ActiveMediaPolicy.NORMAL_MEDIA)
                           for name in (*NORMAL_NAMES, *WEIRD_NORMAL_NAMES)])


class ActiveMediaClassificationTests(TestCase):
    def test_explicit_direct_compound_and_normal_classification_table(self):
        for filename, expected in CLASSIFICATION_CASES:
            with self.subTest(filename=filename):
                self.assertEqual(classify_active_media(filename), expected)

    def test_classification_does_not_consult_platform_mime_registry(self):
        with patch("mimetypes.guess_type", side_effect=AssertionError("No MIME registry for security")) as registry:
            for filename, expected in CLASSIFICATION_CASES:
                with self.subTest(filename=filename):
                    self.assertEqual(classify_active_media(filename), expected)
            registry.assert_not_called()


class ActiveMediaDeliveryTests(TestCase):
    def setUp(self):
        self.root = Path(mkdtemp(prefix="tecno-sec1c-test-")).resolve()
        self.addCleanup(self.remove_own_fixtures)
        settings_patch = patch.object(get_settings(), "MEDIA_ROOT", self.root)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)  # Verified disposable memory schema only.
        self.db = Session(self.engine, autoflush=False)
        self.addCleanup(self.db.close)
        self.tenant = Client(name="SECURITY-1C fixture", slug="security-1c-fixture")
        self.db.add(self.tenant)
        self.db.commit()
        user = SimpleNamespace(role=UserRole.CLIENT_ADMIN, client_id=self.tenant.id, branch_id=None)
        app = FastAPI()
        app.include_router(contents.router, prefix="/api/contents")
        app.include_router(media.router)

        def isolated_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: user
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def remove_own_fixtures(self):
        # This test owns a unique directory; never follow links or delete a tree.
        for entry in sorted(self.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not entry.resolve().is_relative_to(self.root):
                raise RuntimeError("Refusing cleanup outside the isolated fixture directory")
            if entry.is_file():
                entry.unlink()
            else:
                entry.rmdir()
        self.root.rmdir()

    def upload(self, filename, data, mime):
        return self.http.post("/api/contents/upload", data={"name": "QA fixture"},
                              files={"file": (filename, data, mime)})

    def assert_rejected_without_side_effects(self, filename, mime):
        with patch.object(contents, "write_media_upload") as writer, \
             patch.object(contents, "ContentItem") as constructor, \
             patch.object(contents, "save_content_reference") as reference_writer:
            result = self.upload(filename, HTML, mime)
            self.assertEqual(result.status_code, 415, result.text)
            self.assertEqual(result.json(), {"detail": "ACTIVE_HTML_UPLOAD_NOT_ALLOWED"})
            writer.assert_not_called()
            constructor.assert_not_called()
            reference_writer.assert_not_called()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_html_extension_and_mismatch_rejected_before_storage_or_reference(self):
        for filename, mime in (("file.html", "text/html"), ("FILE.HTML", "image/png"),
                               ("file.htm", "application/octet-stream"), ("file.HTM", "image/svg+xml"),
                               (" file.HTML ", "image/png")):
            with self.subTest(filename=filename, mime=mime):
                self.assert_rejected_without_side_effects(filename, mime)

    def test_normalized_html_and_xhtml_mime_rejected_for_other_extensions(self):
        for filename, mime in (("file.jpg", "text/html"), ("file.png", "text/html;charset=UTF-8"),
                               ("file", " Text/HTML ; charset=utf-8"),
                               ("file.svg", "application/xhtml+xml"),
                               ("file.jpg", "Application/XHTML+XML; charset=UTF-8")):
            with self.subTest(filename=filename, mime=mime):
                self.assert_rejected_without_side_effects(filename, mime)

    def assert_upload_cases_rejected_without_writes(self, cases):
        # Exercise real storage/reference writers if the guard regresses.
        # Observe DB writes/commits without replacing the endpoint's behavior.
        writes, commits = [], []

        def before_statement(connection, cursor, statement, parameters, context, executemany):
            if context.isinsert or context.isupdate or context.isdelete:
                writes.append(statement)

        def on_commit(connection):
            commits.append(True)

        event.listen(self.engine, "before_cursor_execute", before_statement)
        event.listen(self.engine, "commit", on_commit)
        try:
            for filename, mime in cases:
                with self.subTest(filename=filename, mime=mime):
                    result = self.upload(filename, XHTML, mime)
                    self.assertEqual(result.status_code, 415, result.text)
                    self.assertEqual(result.json(), {"detail": "ACTIVE_HTML_UPLOAD_NOT_ALLOWED"})
                    self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 0)
                    self.assertEqual(list(self.root.rglob("*")), [])
                    self.assertEqual(writes, [])
                    self.assertEqual(commits, [])
        finally:
            event.remove(self.engine, "before_cursor_execute", before_statement)
            event.remove(self.engine, "commit", on_commit)

    def test_xhtml_upload_correct_mime_and_mismatches_before_write(self):
        self.assert_upload_cases_rejected_without_writes((
            ("fixture.xhtml", "application/xhtml+xml"), ("fixture.xhtml", "image/png"),
            ("fixture.XHTML", "image/png"), ("fixture.xht", "image/png"),
            ("fixture.XhT", "application/octet-stream"), ("fixture.jpg", "application/xhtml+xml"),
            ("fixture.png", "application/xhtml+xml; charset=utf-8"),
        ))

    def test_all_html_original_names_rejected_before_any_storage_or_db_write(self):
        self.assert_upload_cases_rejected_without_writes((name, "image/png") for name in HTML_NAMES)

    def test_html_dotfile_upload_rejected_before_any_storage_or_db_write(self):
        self.assert_upload_cases_rejected_without_writes(
            [(name, "image/png") for name in HTML_DOTFILE_NAMES]
            + [("innocent.jpg", "application/xhtml+xml")])

    def test_svg_upload_preserves_bytes_uuid_type_metadata_and_reference(self):
        for filename, data in (("fixture.svg", SVG_FIXTURES["simple"].encode()),
                               ("fixture.SVG", SVG_FIXTURES["onload"].encode()),
                               ("fixture.svgz", gzip.compress(SVG_FIXTURES["script"].encode())),
                               ("plain.svgz", SVG_FIXTURES["script"].encode())):
            with self.subTest(filename=filename):
                result = self.upload(filename, data, "image/svg+xml")
                self.assertEqual(result.status_code, 201, result.text)
                body = result.json()
                physical = self.root / body["file_path"].removeprefix("/media/")
                self.assertEqual(physical.read_bytes(), data)
                self.assertEqual(physical.parent, self.root / self.tenant.id)
                self.assertRegex(physical.name, r"^[0-9a-f]{32}\.svgz?$")
                self.assertEqual(physical.suffix, Path(filename).suffix.lower())
                self.assertEqual(body["type"], "image")
                self.assertEqual(body["metadata_json"], {"filename": filename, "content_type": "image/svg+xml"})
                row = self.db.get(ContentItem, body["id"])
                self.assertEqual(row.file_path, body["file_path"])
                self.assertEqual(row.type, ContentType.IMAGE)
                self.assertEqual(self.http.get(body["file_path"]).content, data)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 4)
        self.assertEqual(list(self.root.rglob(".upload-*")), [])

    def test_logical_html_content_still_creates_without_a_file(self):
        result = self.http.post("/api/contents/", json={
            "name": "Logical HTML fixture", "type": "html", "html_content": HTML.decode(),
        })
        self.assertEqual(result.status_code, 201, result.text)
        body = result.json()
        self.assertEqual(body["html_content"], HTML.decode())
        self.assertEqual(body["type"], "html")
        self.assertIsNone(body["file_path"])
        self.assertEqual(self.db.get(ContentItem, body["id"]).html_content, HTML.decode())
        self.assertEqual(list(self.root.rglob("*")), [])

    def assert_delivery(self, filename, data, mime, disposition, *, active=None):
        path = self.root / filename
        path.write_bytes(data)
        url = "/media/" + quote(filename, safe="/")
        full = self.http.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, data)
        expected_last_modified = formatdate(path.stat().st_mtime, usegmt=True)
        cases = ((None, 200, data, None), ("bytes=2-5", 206, data[2:6], f"bytes 2-5/{len(data)}"),
                 ("bytes=8-", 206, data[8:], f"bytes 8-{len(data)-1}/{len(data)}"),
                 ("bytes=-3", 206, data[-3:], f"bytes {len(data)-3}-{len(data)-1}/{len(data)}"),
                 (f"bytes={len(data)}-", 416, b"", f"bytes */{len(data)}"),
                 ("bytes=5-2", 416, b"", f"bytes */{len(data)}"),
                 ("bytes=0-1,3-4", 416, b"", f"bytes */{len(data)}"))
        for method in ("GET", "HEAD"):
            for value, status, expected_bytes, content_range in cases:
                with self.subTest(filename=filename, method=method, range=value):
                    result = self.http.request(method, url, headers={"Range": value} if value else {})
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(result.content, expected_bytes if method == "GET" else b"")
                    self.assertEqual(result.headers["content-type"], mime)
                    self.assertTrue(result.headers["content-disposition"].startswith(disposition + ";"))
                    self.assertIn("filename*=UTF-8''", result.headers["content-disposition"])
                    self.assertEqual(result.headers["content-length"], str(len(expected_bytes)))
                    self.assertEqual(result.headers["content-range"] if content_range else None, content_range)
                    self.assertEqual(result.headers["etag"], full.headers["etag"])
                    self.assertEqual(result.headers["last-modified"], expected_last_modified)
                    self.assertEqual(result.headers["accept-ranges"], "bytes")
                    self.assertNotIn("content-encoding", result.headers)
                    if active:
                        self.assertEqual(result.headers["x-content-type-options"], "nosniff")
                    else:
                        self.assertNotIn("x-content-type-options", result.headers)
                    if active == "svg":
                        self.assertEqual(result.headers["content-security-policy"], EXPECTED_SVG_CSP)
                        self.assertNotIn("allow-scripts", result.headers["content-security-policy"])
                        self.assertNotIn("allow-same-origin", result.headers["content-security-policy"])
                    else:
                        self.assertNotIn("content-security-policy", result.headers)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(sha256(path.read_bytes()).digest(), sha256(data).digest())
        self.assertEqual(sha256(full.content).digest(), sha256(data).digest())

    def test_historical_html_get_head_all_ranges_attachment(self):
        self.assert_delivery("legacy.html", HTML, "application/octet-stream", "attachment", active="html")

    def test_historical_htm_and_uppercase_suffixes(self):
        for filename in ("legacy.htm", "legacy.HTML", "legacy.HTM"):
            self.assert_delivery(filename, HTML, "application/octet-stream", "attachment", active="html")

    def test_historical_xhtml_get_head_all_ranges_attachment(self):
        self.assert_delivery("legacy.xhtml", XHTML, "application/octet-stream", "attachment", active="html")

    def test_historical_xht_and_mixed_case_suffixes(self):
        for filename in ("legacy.xht", "legacy.XHTML", "legacy.XhT"):
            self.assert_delivery(filename, XHTML, "application/octet-stream", "attachment", active="html")

    def test_all_historical_html_direct_and_compound_names_keep_bytes_and_ranges(self):
        for filename in HTML_NAMES:
            self.assert_delivery(filename, XHTML, "application/octet-stream", "attachment", active="html")

    def test_historical_html_dotfiles_at_media_root_and_client_directory(self):
        (self.root / self.tenant.id).mkdir()
        for filename in HTML_DOTFILE_NAMES:
            for prefix in ("", self.tenant.id + "/"):
                self.assert_delivery(prefix + filename, DOTFILE_XHTML,
                                     "application/octet-stream", "attachment", active="html")

    def test_dotfile_security_uses_basename_despite_full_path_mime(self):
        # Simulate Windows' active full-path MIME on every platform. Security
        # must force its own headers and never consult that fallback at all.
        for parent in ("ordinary", "parent.xhtml", "parent.svg.gz"):
            (self.root / parent).mkdir()
            with patch("app.services.media_streaming.guess_type",
                       return_value=("application/xhtml+xml", "gzip")) as registry, \
                 patch("app.services.media_streaming.classify_active_media",
                       wraps=classify_active_media) as classifier:
                self.assert_delivery(parent + "/.xhtml.gz", DOTFILE_XHTML,
                                     "application/octet-stream", "attachment", active="html")
                registry.assert_not_called()
                self.assertTrue(classifier.call_args_list)
                self.assertTrue(all(call.args == (".xhtml.gz",) for call in classifier.call_args_list))

    def test_weird_historical_names_are_protected_or_not_active(self):
        for filename in ("..xhtml", "..xhtml.gz", "...xhtml.gz", "a..xhtml", "a..xhtml.gz"):
            self.assert_delivery(filename, DOTFILE_XHTML,
                                 "application/octet-stream", "attachment", active="html")
        for filename in WEIRD_NORMAL_NAMES:
            if os.name == "nt" and filename.endswith((".", " ")):
                # Do not create a Win32 alias of another filename. The real
                # media resolver must reject the request before serving it.
                for method in ("GET", "HEAD"):
                    response = self.http.request(method, "/media/" + quote(filename, safe=""))
                    self.assertEqual(response.status_code, 404)
            else:
                self.assert_delivery(filename, DOTFILE_XHTML, "application/octet-stream", "inline")

    def test_active_delivery_is_independent_of_platform_mime_registry(self):
        with patch("app.services.media_streaming.guess_type", side_effect=AssertionError("No MIME registry for active delivery")) as registry:
            for filename in ("legacy.html", "legacy.xhtml", "legacy.xhtml.gz", "legacy.xht.br"):
                self.assert_delivery(filename, XHTML, "application/octet-stream", "attachment", active="html")
            for filename in ("legacy.svg", "legacy.svgz", "legacy.svg.gz", "legacy.SVG.Z"):
                self.assert_delivery(filename, SVG_FIXTURES["script"].encode(), "image/svg+xml", "inline", active="svg")
            registry.assert_not_called()

    def test_all_historical_svg_direct_and_compound_names_keep_bytes_and_ranges(self):
        for filename in SVG_NAMES:
            self.assert_delivery(filename, SVG_FIXTURES["onload"].encode(), "image/svg+xml", "inline", active="svg")

    def test_historical_svg_dotfiles_keep_csp_bytes_and_ranges(self):
        for filename in SVG_DOTFILE_NAMES:
            self.assert_delivery(filename, SVG_FIXTURES["script"].encode(),
                                 "image/svg+xml", "inline", active="svg")

    def test_actual_gzip_html_and_svg_keep_encoded_bytes_hashes_and_ranges(self):
        self.assert_delivery("compressed.xhtml.gz", gzip.compress(XHTML),
                             "application/octet-stream", "attachment", active="html")
        self.assert_delivery("compressed.svg.gz", gzip.compress(SVG_FIXTURES["script"].encode()),
                             "image/svg+xml", "inline", active="svg")
        self.assert_delivery(".xhtml.gz", gzip.compress(DOTFILE_XHTML),
                             "application/octet-stream", "attachment", active="html")
        self.assert_delivery(".svg.gz", gzip.compress(SVG_FIXTURES["script"].encode()),
                             "image/svg+xml", "inline", active="svg")

    def test_svg_fixtures_get_head_ranges_csp_and_original_bytes(self):
        for name, value in SVG_FIXTURES.items():
            self.assert_delivery(name + ".svg", value.encode(), "image/svg+xml", "inline", active="svg")

    def test_uppercase_svg_suffix_receives_security_headers(self):
        self.assert_delivery("legacy.SVG", SVG_FIXTURES["script"].encode(), "image/svg+xml", "inline", active="svg")

    def test_svgz_compressed_keeps_original_bytes_without_content_encoding(self):
        self.assert_delivery("compressed.svgz", gzip.compress(SVG_FIXTURES["script"].encode()),
                             "image/svg+xml", "inline", active="svg")

    def test_svgz_uncompressed_cannot_bypass_security_headers(self):
        self.assert_delivery("uncompressed.SVGZ", SVG_FIXTURES["onload"].encode(),
                             "image/svg+xml", "inline", active="svg")

    def test_non_active_media_headers_and_ranges_unchanged(self):
        for suffix in ("png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "wmv", "m2v", "mp3", "wav", "bin"):
            filename = "fixture." + suffix
            expected_mime = guess_type(filename)[0] or "application/octet-stream"
            self.assert_delivery(filename, b"0123456789 fixture bytes", expected_mime, "inline")

    def test_non_active_compound_names_keep_ordinary_headers_and_ranges(self):
        for filename in NORMAL_NAMES:
            expected_mime = guess_type(filename)[0] or "application/octet-stream"
            self.assert_delivery(filename, b"0123456789 fixture bytes", expected_mime, "inline")

    def test_delivery_does_not_mutate_content_runtime_playlist_or_retention(self):
        branch = Branch(client_id=self.tenant.id, name="QA branch", code="QA")
        campaign = Campaign(client_id=self.tenant.id, name="QA campaign")
        self.db.add_all([branch, campaign])
        self.db.flush()
        channel = Channel(client_id=self.tenant.id, branch_id=branch.id, name="QA screen")
        self.db.add(channel)
        self.db.flush()
        self.db.add(CampaignChannelAssignment(channel_id=channel.id, campaign_id=campaign.id))
        items = []
        (self.root / self.tenant.id).mkdir()
        for order, (filename, data, mime) in enumerate((
            ("historical.html", HTML, "text/html"),
            ("historical.xhtml", XHTML, "application/xhtml+xml"),
            ("historical.xht", XHTML, "application/xhtml+xml"),
            ("historical.xhtml.gz", XHTML, "application/xhtml+xml"),
            ("historical.svg.gz", SVG_FIXTURES["script"].encode(), "image/svg+xml"),
            ("historical.svg", SVG_FIXTURES["script"].encode(), "image/svg+xml"),
            ("historical.mp4", b"video fixture", "video/mp4"),
        ), 1):
            (self.root / self.tenant.id / filename).write_bytes(data)
            item = ContentItem(client_id=self.tenant.id, name=filename, type=ContentType.IMAGE,
                               file_path=f"/media/{self.tenant.id}/{filename}", metadata_json={"content_type": mime})
            self.db.add(item)
            self.db.flush()
            self.db.add(CampaignSequenceItem(campaign_id=campaign.id, content_id=item.id,
                                            item_type=CampaignSequenceItemType.CONTENT, sort_order=order, is_enabled=True))
            items.append(item)
        self.db.commit()
        base_url = "http://testserver/"
        before_items = [serialize_content(item, base_url) for item in items]
        with patch("app.services.player_runtime.now_utc", return_value=datetime(2026, 10, 16, 12, tzinfo=UTC)):
            before = build_player_runtime_config(channel, self.db, base_url, snapshot_coherent=True)
            for item in items:
                self.assertEqual(self.http.get(item.file_path).status_code, 200)
                self.assertEqual(self.http.head(item.file_path).status_code, 200)
            self.db.expire_all()
            after = build_player_runtime_config(channel, self.db, base_url, snapshot_coherent=True)
        self.assertEqual(after, before)
        self.assertEqual([serialize_content(item, base_url) for item in items], before_items)
        self.assertEqual(len(before["contents"]), 7)
        self.assertTrue(before["media_retention"]["complete"])
        self.assertEqual(len(before["media_retention"]["assets"]), 7)
        for item in before_items:
            self.assertEqual(item["download_url"], "http://testserver" + item["file_path"])
