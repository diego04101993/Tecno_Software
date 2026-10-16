"""Real PostgreSQL sessions; never run against an application database."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
import os

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Only the isolated test harness may import these tests")

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes import contents
from app.core.config import get_settings
from app.models.base import Base
from app.models.entities import Client, ContentItem, ContentType, UserRole
from app.schemas.domain import ContentCreate
from app.services import content_cleanup
from app.services.media_references import lock_media_paths, media_lock_key, media_reference_operation, save_content_reference
from app.services.media_storage import MediaPathError


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@skipUnless(TEST_DATABASE_URL, "Requires disposable local PostgreSQL")
class PostgreSQLMediaReferenceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_DATABASE_URL)
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
                or url.database != "tecno_control_retention_test"):
            raise RuntimeError("Refusing nonlocal or non-test database")
        cls.engine = create_engine(url)
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="tecno-sec1a-pg-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        setting = patch.object(get_settings(), "MEDIA_ROOT", self.root)
        setting.start()
        self.addCleanup(setting.stop)
        self.path = self.root / (os.urandom(8).hex() + ".mp4")
        self.path.write_bytes(b"complete fixture")
        self.url = "/media/" + self.path.name
        with Session(self.engine) as db:
            suffix = os.urandom(8).hex()
            first = Client(name="Delete fixture", slug="delete-" + suffix)
            second = Client(name="Reference fixture", slug="reference-" + suffix)
            db.add_all([first, second])
            db.flush()
            item = ContentItem(client_id=first.id, name="Original", type=ContentType.VIDEO, file_path=self.url)
            db.add(item)
            db.commit()
            self.item_id, self.client_id, self.other_id = item.id, first.id, second.id

    def user(self, client_id):
        return SimpleNamespace(role=UserRole.CLIENT_ADMIN, client_id=client_id, branch_id=None)

    def delete_original(self):
        with Session(self.engine, autoflush=False) as db:
            contents.delete_content(self.item_id, db=db, current_user=self.user(self.client_id))

    def create_reference(self):
        with Session(self.engine, autoflush=False) as db:
            try:
                item = contents.create_content(ContentCreate(name="New", type=ContentType.VIDEO, file_path=self.url),
                                               db=db, current_user=self.user(self.other_id))
                return item.id
            except HTTPException as exc:
                return exc.status_code, exc.detail

    def wait_for_pg_waiter(self):
        # Evidence is a real waiting PostgreSQL backend, not just thread timing.
        deadline = monotonic() + 5
        key = media_lock_key(self.path) & ((1 << 64) - 1)
        while monotonic() < deadline:
            with self.engine.connect() as probe:
                rows = probe.execute(text(
                    "SELECT pid, granted FROM pg_locks WHERE locktype='advisory' "
                    "AND classid::bigint=:hi AND objid::bigint=:lo AND objsubid=1"
                ), {"hi": key >> 32, "lo": key & 0xffffffff}).all()
            if any(not row.granted for row in rows):
                self.assertGreaterEqual(len({row.pid for row in rows}), 2)
                self.assertTrue(any(row.granted for row in rows))
                return
            sleep(0.02)
        self.fail("No real PostgreSQL advisory lock waiter observed")

    def assert_lock_released(self):
        with self.engine.begin() as connection:
            self.assertTrue(connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                              {"key": media_lock_key(self.path)}))

    def test_delete_wins_concurrent_create_cannot_commit_dangling_reference(self):
        reached_check, proceed = Event(), Event()
        real_check = content_cleanup._referenced_media_paths

        def paused_check(db):
            reached_check.set()  # Delete already committed; lock must STILL exist.
            if not proceed.wait(8):
                raise AssertionError("Timed out waiting for competing writer")
            return real_check(db)

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(content_cleanup, "_referenced_media_paths", paused_check):
            deletion = pool.submit(self.delete_original)
            try:
                self.assertTrue(reached_check.wait(5))
                creation = pool.submit(self.create_reference)
                self.wait_for_pg_waiter()
                self.assertFalse(creation.done())
            finally:
                proceed.set()
            deletion.result(timeout=10)
            self.assertEqual(creation.result(timeout=10), (409, "FILE_NOT_FOUND"))
        self.assertFalse(self.path.exists())
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ContentItem).where(
                ContentItem.client_id.in_([self.client_id, self.other_id]))), 0)
        self.assert_lock_released()

    def test_create_wins_deleter_rechecks_global_references_and_keeps_file(self):
        committed, proceed = Event(), Event()

        def writer():
            with Session(self.engine, autoflush=False) as db, media_reference_operation(db):
                lock_media_paths(db, [self.path])
                item_id = contents.create_content(ContentCreate(name="Other tenant", type=ContentType.VIDEO,
                    file_path=self.url), db=db, current_user=self.user(self.other_id)).id
                committed.set()
                if not proceed.wait(8):
                    raise AssertionError("Timed out waiting for competing deleter")
                return item_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            creation = pool.submit(writer)
            try:
                self.assertTrue(committed.wait(5))
                deletion = pool.submit(self.delete_original)
                self.wait_for_pg_waiter()
                self.assertFalse(deletion.done())
            finally:
                proceed.set()
            new_id = creation.result(timeout=10)
            deletion.result(timeout=10)
        self.assertEqual(self.path.read_bytes(), b"complete fixture")
        with Session(self.engine) as db:
            self.assertIsNone(db.get(ContentItem, self.item_id))
            self.assertEqual(db.get(ContentItem, new_id).client_id, self.other_id)
        self.assert_lock_released()

    def test_delete_wins_concurrent_file_path_update_is_rejected(self):
        reached_check, proceed = Event(), Event()
        other_path = self.root / "other.mp4"
        other_path.write_bytes(b"old target")
        with Session(self.engine) as db:
            item = ContentItem(client_id=self.other_id, name="Update", type=ContentType.VIDEO, file_path="/media/other.mp4")
            db.add(item)
            db.commit()
            updated_id = item.id
        real_check = content_cleanup._referenced_media_paths

        def paused_check(db):
            reached_check.set()
            if not proceed.wait(8):
                raise AssertionError("Timed out")
            return real_check(db)

        def update():
            with Session(self.engine, autoflush=False) as db:
                item = db.get(ContentItem, updated_id)
                item.file_path = self.url
                try:
                    save_content_reference(item, db)
                    return "unexpected commit"
                except MediaPathError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(content_cleanup, "_referenced_media_paths", paused_check):
            deletion = pool.submit(self.delete_original)
            try:
                self.assertTrue(reached_check.wait(5))
                updating = pool.submit(update)
                self.wait_for_pg_waiter()
            finally:
                proceed.set()
            deletion.result(timeout=10)
            self.assertEqual(updating.result(timeout=10), "FILE_NOT_FOUND")
        with Session(self.engine) as db:
            self.assertEqual(db.get(ContentItem, updated_id).file_path, "/media/other.mp4")
        self.assertEqual(other_path.read_bytes(), b"old target")
        self.assert_lock_released()

    def test_db_failure_rolls_back_before_releasing_lock_without_unlink(self):
        with Session(self.engine, autoflush=False) as db, patch.object(db, "commit", side_effect=SQLAlchemyError("fixture")), \
                patch.object(Path, "unlink") as unlink:
            with self.assertRaises(SQLAlchemyError):
                contents.delete_content(self.item_id, db=db, current_user=self.user(self.client_id))
            unlink.assert_not_called()
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(ContentItem, self.item_id))
        self.assertTrue(self.path.exists())
        self.assert_lock_released()

    def test_unlink_failure_after_commit_releases_lock_and_keeps_orphan(self):
        with patch.object(Path, "unlink", side_effect=OSError("fixture")):
            with self.assertRaises(HTTPException) as caught:
                self.delete_original()
        self.assertEqual(caught.exception.detail, "MEDIA_DELETE_FAILED")
        with Session(self.engine) as db:
            self.assertIsNone(db.get(ContentItem, self.item_id))
        self.assertTrue(self.path.exists())
        self.assert_lock_released()


    def test_stale_snapshot_is_rejected_before_deletion(self):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with Session(connection, autoflush=False) as db:
                with self.assertRaises(HTTPException) as caught:
                    contents.delete_content(self.item_id, db=db, current_user=self.user(self.client_id))
                self.assertEqual(caught.exception.detail, "MEDIA_LOCK_REQUIRES_READ_COMMITTED")
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(ContentItem, self.item_id))
        self.assertTrue(self.path.exists())
        self.assert_lock_released()
