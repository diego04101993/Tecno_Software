"""SECURITY-1D: synthetic files, real loopback HTTP, no application startup.

Set DATABASE_URL=sqlite://, AUTO_SEED=false and a temporary MEDIA_ROOT before
discovery. The existing regression suites require the same isolated settings.
"""
import http.client
import logging
import os
import re
import socket
import threading
import time
import unittest
from email.utils import formatdate
from hashlib import md5
from mimetypes import guess_type
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote, unquote

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Configure memory SQLite and temporary MEDIA_ROOT before importing app modules")

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

from app.api.routes import media
from app.services import media_streaming as streaming


NAME_CASES = (
    ("simple.mp4", "simple.mp4"),
    ("318.mp4", "318.mp4"),
    ("a b.mp4", "a b.mp4"),
    ("café.mp4", "café.mp4"),
    ("漢字.mp4", "漢字.mp4"),
    ("emoji-😀.mp4", "emoji-😀.mp4"),
    ('a"b.mp4', 'a"b.mp4'),
    ('a"; qa="yes.mp4', 'a"; qa="yes.mp4'),
    ('a"; filename="evil.mp4', 'a"; filename="evil.mp4'),
    ("a\\b.mp4", "a_b.mp4"),
    ("a;b.mp4", "a;b.mp4"),
    ("a,b.mp4", "a,b.mp4"),
    ("a(1).mp4", "a(1).mp4"),
    ("a[1].mp4", "a[1].mp4"),
    ("100%.mp4", "100%.mp4"),
)
CONTROL_CASES = tuple((f"a{chr(code)}b.mp4", "a_b.mp4") for code in (*range(32), 127))
INJECTION_CASES = tuple(
    (f"a\r\n{header}: qa.mp4", f"a__{header}: qa.mp4")
    for header in ("X-QA", "Content-Type", "Content-Security-Policy", "Set-Cookie")
)
PLATFORM_CASES = NAME_CASES + CONTROL_CASES + INJECTION_CASES + (("a\r\nb.mp4", "a__b.mp4"),)
EXPECTED_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
PAYLOAD = b"0123456789abcdef"  # Transport fixture only, not a playable MP4.


def assert_safe_header(test, header, disposition, presentation):
    test.assertTrue(header.isascii(), repr(header))
    header.encode("ascii")
    test.assertFalse(any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in header))
    # Full match permits exactly two parameters, with no quoted-string escapes.
    match = re.fullmatch(r'(inline|attachment); filename="([A-Za-z0-9 ._-]+)"; filename\*=UTF-8\'\'([A-Za-z0-9._~%!-]*)', header)
    test.assertIsNotNone(match, repr(header))
    test.assertEqual(match[1], disposition)
    test.assertTrue(any(char.isalnum() for char in match[2]))
    test.assertEqual(len(re.findall(r'; filename=', header)), 1)
    test.assertEqual(len(re.findall(r'; filename\*=', header)), 1)
    test.assertEqual(unquote(match[3], encoding="utf-8", errors="strict"), presentation)
    test.assertEqual(match[3], quote(presentation, safe=""))
    response = Response(headers={"Content-Disposition": header})
    test.assertEqual(dict(response.raw_headers)[b"content-disposition"], header.encode("ascii"))


class ContentDispositionHelperTests(unittest.TestCase):
    def test_name_matrix_for_both_dispositions(self):
        for name, presentation in NAME_CASES:
            for disposition in ("inline", "attachment"):
                with self.subTest(name=name, disposition=disposition):
                    assert_safe_header(self, streaming.build_content_disposition(disposition, name), disposition, presentation)

    def test_all_c0_controls_and_del_are_sanitized_before_encoding(self):
        for name, presentation in CONTROL_CASES:
            with self.subTest(name=name):
                assert_safe_header(self, streaming.build_content_disposition("inline", name), "inline", presentation)

    def test_crlf_header_injection_payloads(self):
        for name, presentation in INJECTION_CASES:
            with self.subTest(name=name):
                assert_safe_header(self, streaming.build_content_disposition("inline", name), "inline", presentation)

    def test_fallback_is_nontrivial_and_preserves_safe_extension(self):
        for name, fallback in (("", "download"), ("_", "download"), (".mp4", "download.mp4"),
                               ("___.mp4", "download.mp4"), ("漢字.mp4", "download.mp4"),
                               ("café.mp4", "caf_.mp4"), ("emoji-😀.mp4", "emoji-_.mp4"),
                               ("...", "download"), ("😀.☃", "download")):
            with self.subTest(name=name):
                header = streaming.build_content_disposition("inline", name)
                assert_safe_header(self, header, "inline", name)
                self.assertIn(f'; filename="{fallback}";', header)

    def test_safe_ascii_names_keep_exact_legacy_header(self):
        for name in ("simple.mp4", "318.mp4", "a b.mp4", "qa_file-1.png"):
            with self.subTest(name=name):
                self.assertEqual(streaming.build_content_disposition("inline", name),
                                 f'inline; filename="{name}"; filename*=UTF-8\'\'{quote(name)}')

    def test_slashes_percent_and_plus_are_encoded_not_interpreted(self):
        name = "a/b+100%.mp4"
        header = streaming.build_content_disposition("attachment", name)
        assert_safe_header(self, header, "attachment", name)
        self.assertIn("a%2Fb%2B100%25.mp4", header)

    def test_rejects_other_dispositions(self):
        for disposition in ("", "INLINE", "attachment; qa=yes", "inline\r\nX-QA: yes", None, True):
            with self.subTest(disposition=disposition), self.assertRaises(ValueError):
                streaming.build_content_disposition(disposition, "simple.mp4")


class _ServerErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class ContentDispositionHttpTests(unittest.TestCase):
    request_count = 0

    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory(prefix="SEC1D_R1_HTTP_")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.root_patch = patch.object(media.settings, "MEDIA_ROOT", cls.root)
        cls.root_patch.start()
        cls.addClassCleanup(cls.root_patch.stop)
        app = FastAPI()
        app.include_router(media.router)
        cls.errors = _ServerErrors()
        logging.getLogger("uvicorn.error").addHandler(cls.errors)
        cls.addClassCleanup(logging.getLogger("uvicorn.error").removeHandler, cls.errors)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.port = cls.listener.getsockname()[1]
        cls.addClassCleanup(cls.listener.close)
        # httptools is the server implementation that previously rejected DEL.
        cls.server = uvicorn.Server(uvicorn.Config(app, http="httptools", lifespan="off", access_log=False, log_config=None))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Isolated loopback Uvicorn did not start")

    @classmethod
    def stop_server(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        if cls.thread.is_alive():
            raise AssertionError("Test Uvicorn did not stop")
        if cls.errors.messages:
            raise AssertionError(f"Unexpected HTTP server errors: {cls.errors.messages}")

    def request(self, method, relative, range_header=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, "/media/" + quote(relative, safe="/"),
                               headers={"Range": range_header} if range_header else {})
            response = connection.getresponse()
            result = response.status, response.getheaders(), response.read()
            type(self).request_count += 1
            return result
        finally:
            connection.close()

    def check_delivery(self, name, presentation=None, *, alias=False, disposition="inline", mime=None):
        directory = self.root / str(type(self).request_count)
        directory.mkdir()
        target = directory / name
        target.write_bytes(PAYLOAD)
        before = target.stat()
        relative = target.relative_to(self.root).as_posix()
        if alias:
            link = self.root / f"alias-{type(self).request_count}.mp4"
            link.symlink_to(target)
            relative = link.name
        presentation = name if presentation is None else presentation
        mime = mime or guess_type(str(target))[0] or "application/octet-stream"
        etag = '"' + md5(f"{before.st_mtime}-{before.st_size}".encode(), usedforsecurity=False).hexdigest() + '"'
        variants = ((None, 200, PAYLOAD, None), ("bytes=2-5", 206, PAYLOAD[2:6], "bytes 2-5/16"),
                    ("bytes=4-", 206, PAYLOAD[4:], "bytes 4-15/16"), ("bytes=-4", 206, PAYLOAD[-4:], "bytes 12-15/16"),
                    ("bytes=99-", 416, b"", "bytes */16"))
        for method in ("GET", "HEAD"):
            for range_header, expected_status, expected_bytes, content_range in variants:
                with self.subTest(name=name, method=method, range_header=range_header, alias=alias):
                    status, raw_headers, body = self.request(method, relative, range_header)
                    self.assertEqual(status, expected_status)
                    headers = dict(raw_headers)
                    self.assertEqual(len(headers), len(raw_headers), "Duplicate response header")
                    self.assertEqual(body, b"" if method == "HEAD" else expected_bytes)
                    self.assertEqual(headers["content-type"], mime)
                    self.assertEqual(headers["accept-ranges"], "bytes")
                    self.assertEqual(headers["content-length"], str(len(expected_bytes)))
                    self.assertEqual(headers.get("content-range"), content_range)
                    self.assertEqual(headers["etag"], etag)
                    self.assertEqual(headers["last-modified"], formatdate(before.st_mtime, usegmt=True))
                    assert_safe_header(self, headers["content-disposition"], disposition, presentation)
                    allowed = {"date", "server", "content-type", "content-length", "content-disposition", "accept-ranges", "etag", "last-modified"}
                    if content_range:
                        allowed.add("content-range")
                    if disposition == "attachment" or mime == "image/svg+xml":
                        self.assertEqual(headers["x-content-type-options"], "nosniff")
                        allowed.add("x-content-type-options")
                    if mime == "image/svg+xml":
                        self.assertEqual(headers["content-security-policy"], EXPECTED_CSP)
                        allowed.add("content-security-policy")
                    self.assertEqual(set(headers), allowed, "Injected or unexpected response header")
        after = target.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns), (before.st_ino, before.st_size, before.st_mtime_ns))
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual([entry.name for entry in directory.iterdir()], [name])

    def test_unicode_and_safe_punctuation_real_http(self):
        for name, presentation in NAME_CASES:
            if '"' not in name and "\\" not in name:
                self.check_delivery(name, presentation)

    def test_del_real_http_no_connection_close(self):
        self.check_delivery("a\x7fb.mp4", "a_b.mp4")

    @unittest.skipUnless(os.name == "posix", "Quotes in physical basenames require POSIX")
    def test_quote_and_parameter_injection_real_http(self):
        for name, presentation in NAME_CASES:
            if '"' in name:
                self.check_delivery(name, presentation)

    @unittest.skipUnless(os.name == "posix", "Control/backslash physical basenames require POSIX")
    def test_internal_aliases_all_representable_controls_and_injection(self):
        cases = CONTROL_CASES[1:] + INJECTION_CASES + (("a\\b.mp4", "a_b.mp4"), ("a\r\nb.mp4", "a__b.mp4"))
        for name, presentation in cases:
            self.check_delivery(name, presentation, alias=True)

    def test_html_and_xhtml_unicode_still_attachment(self):
        for suffix in (".html", ".xhtml"):
            for stem in ("café", "漢字", "emoji-😀"):
                self.check_delivery(stem + suffix, disposition="attachment", mime="application/octet-stream")

    def test_svg_unicode_still_inline_with_exact_csp(self):
        for stem in ("café", "漢字", "emoji-😀"):
            self.check_delivery(stem + ".svg", mime="image/svg+xml")

    @unittest.skipUnless(os.name == "posix", "Quotes in physical basenames require POSIX")
    def test_active_media_quote_still_uses_unchanged_policy(self):
        self.check_delivery('a"b.xhtml', disposition="attachment", mime="application/octet-stream")
        self.check_delivery('a"b.svg', mime="image/svg+xml")

    def test_normal_media_protocol_and_legacy_headers(self):
        for suffix in ("png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "wmv", "m2v"):
            name = f"simple.{suffix}"
            self.check_delivery(name)
            header = streaming.build_content_disposition("inline", name)
            self.assertEqual(header, f'inline; filename="{name}"; filename*=UTF-8\'\'{name}')

    @unittest.skipUnless(os.name == "posix", "Symlink confinement is exercised on Linux")
    def test_alias_outside_media_root_still_rejected(self):
        with TemporaryDirectory(prefix="SEC1D_R1_OUTSIDE_") as outside:
            target = Path(outside) / "outside.mp4"
            target.write_bytes(PAYLOAD)
            link = self.root / "outside-alias.mp4"
            link.symlink_to(target)
            for method in ("GET", "HEAD"):
                status, headers, body = self.request(method, link.name)
                self.assertEqual(status, 404)
                self.assertNotIn("content-disposition", dict(headers))
                if method == "HEAD":
                    self.assertEqual(body, b"")
            self.assertEqual(target.read_bytes(), PAYLOAD)


class ContentDispositionMutationTests(unittest.TestCase):
    def assert_mutation_caught(self, mutant, test_name):
        # Mutate in memory only; run the same assertion tests against the mutant.
        result = unittest.TestResult()
        with patch.object(streaming, "build_content_disposition", mutant):
            ContentDispositionHelperTests(test_name).run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.failures, "Safety test failed to detect the mutation")

    def test_raw_filename_mutation_is_caught(self):
        def mutant(disposition, name):
            return f'{disposition}; filename="{name}"; filename*=UTF-8\'\'{quote(name)}'
        self.assert_mutation_caught(mutant, "test_name_matrix_for_both_dispositions")

    def test_quote_sanitization_removed_mutation_is_caught(self):
        original = streaming.build_content_disposition
        def mutant(disposition, name):
            header = original(disposition, name)
            if '"' in name:
                header = header.replace('filename="' + header.split('filename="')[1].split('";')[0], 'filename="' + name, 1)
            return header
        self.assert_mutation_caught(mutant, "test_name_matrix_for_both_dispositions")

    def test_raw_unicode_fallback_mutation_is_caught(self):
        original = streaming.build_content_disposition
        def mutant(disposition, name):
            header = original(disposition, name)
            if not name.isascii():
                header = re.sub(r'filename="[^"]*";', lambda _: f'filename="{name}";', header, count=1)
            return header
        self.assert_mutation_caught(mutant, "test_name_matrix_for_both_dispositions")

    def test_del_not_sanitized_mutation_is_caught(self):
        original = streaming.build_content_disposition
        def mutant(disposition, name):
            header = original(disposition, name)
            if "\x7f" in name:
                header = header.split("filename*=")[0] + "filename*=UTF-8''" + quote(name, safe="")
            return header
        self.assert_mutation_caught(mutant, "test_all_c0_controls_and_del_are_sanitized_before_encoding")

    def test_filename_star_missing_mutation_is_caught(self):
        original = streaming.build_content_disposition
        def mutant(disposition, name):
            return original(disposition, name).split("; filename*=")[0]
        self.assert_mutation_caught(mutant, "test_name_matrix_for_both_dispositions")
