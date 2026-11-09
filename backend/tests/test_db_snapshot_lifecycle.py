"""Snapshot ownership and bounded-pool regressions on an explicitly owned QA DB."""
import os
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.models.base import Base
from app.models.entities import Branch, Channel, Client, User, UserRole
from app.services.player_runtime import build_player_runtime_from_snapshot
from app.services.tenant_responses import read_snapshot


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Isolated local PostgreSQL QA required")
class SnapshotConnectionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "postgres"}
                or url.database != "tecno_control_retention_test"):
            raise RuntimeError("Refusing snapshot test outside the isolated local QA database")
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        system_id = os.environ.get("SECURITY_R5_PG_SYSTEM_IDENTIFIER")
        if not marker or not system_id:
            raise RuntimeError("Owned PostgreSQL marker and system identifier are required")
        cls.admin = create_engine(url, poolclass=NullPool, hide_parameters=True)
        with cls.admin.connect() as connection:
            if (connection.scalar(text("SELECT current_setting('vwflex.qa_worker',true)")) != marker
                    or connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")) != system_id):
                raise RuntimeError("PostgreSQL QA provenance mismatch")
        cls.schema = "snapshot_lifecycle_" + uuid4().hex
        with cls.admin.begin() as connection:
            connection.exec_driver_sql('CREATE SCHEMA "' + cls.schema + '"')
        cls.engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=0.3,
                                   hide_parameters=True,
                                   connect_args={"options": "-c search_path=" + cls.schema + ",public"})
        Base.metadata.create_all(cls.engine)
        with Session(cls.engine) as db:
            client = Client(name="Snapshot QA", slug="snapshot-" + uuid4().hex)
            db.add(client)
            db.flush()
            branch = Branch(client_id=client.id, name="Snapshot QA", code="QA")
            db.add(branch)
            db.flush()
            channel = Channel(client_id=client.id, branch_id=branch.id, name="Snapshot QA")
            actor = User(full_name="Snapshot QA", email="snapshot-" + uuid4().hex + "@example.com",
                         password_hash="synthetic-not-a-login", role=UserRole.SUPER_ADMIN, is_active=True)
            db.add_all([channel, actor])
            db.commit()
            cls.channel_id, cls.actor_id = channel.id, actor.id

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.exec_driver_sql('DROP SCHEMA "' + cls.schema + '" CASCADE')
        cls.admin.dispose()

    def tearDown(self):
        self.assertEqual(self.engine.pool.checkedout(), 0, "Snapshot or source connection leaked")

    def test_admin_snapshot_releases_clean_request_checkout(self):
        @read_snapshot()
        def endpoint(db, current_user):
            return {"id": current_user.id, "read_only": db.connection().exec_driver_sql("SHOW transaction_read_only").scalar_one(),
                    "isolation": db.connection().get_isolation_level()}
        with Session(self.engine) as source:
            actor = source.get(User, self.actor_id)
            self.assertEqual(self.engine.pool.checkedout(), 1)
            result = endpoint(db=source, current_user=actor)
            self.assertEqual(result, {"id": self.actor_id, "read_only": "on", "isolation": "REPEATABLE READ"})
            self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_player_runtime_releases_clean_request_checkout(self):
        with Session(self.engine) as source:
            channel = source.get(Channel, self.channel_id)
            self.assertEqual(self.engine.pool.checkedout(), 1)
            result = build_player_runtime_from_snapshot(channel, source, "http://qa.invalid")
            self.assertEqual(result["runtime_version"], 3)
            self.assertEqual(result["channel"]["id"], self.channel_id)
            self.assertTrue(result["media_retention"]["complete"])
            self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_repeated_admin_exception_closes_snapshot_and_connection(self):
        @read_snapshot()
        def endpoint(db):
            db.scalar(select(1))
            raise RuntimeError("synthetic endpoint failure")
        for _ in range(10):
            with Session(self.engine) as source:
                source.scalar(text("SELECT 1"))
                with self.assertRaisesRegex(RuntimeError, "synthetic endpoint failure"):
                    endpoint(db=source)
                self.assertEqual(self.engine.pool.checkedout(), 0)

    def test_read_snapshot_still_rejects_writes_without_leaking(self):
        @read_snapshot()
        def endpoint(db):
            user = db.get(User, self.actor_id)
            user.full_name = "forbidden snapshot write"
            db.flush()
        with Session(self.engine) as source:
            source.scalar(text("SELECT 1"))
            with self.assertRaises(HTTPException) as caught:
                endpoint(db=source)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.engine.pool.checkedout(), 0)
        with Session(self.engine) as verify:
            self.assertEqual(verify.get(User, self.actor_id).full_name, "Snapshot QA")

    def test_pending_source_changes_are_not_discarded(self):
        @read_snapshot()
        def endpoint(db):
            return True
        with Session(self.engine) as source:
            actor = source.get(User, self.actor_id)
            actor.full_name = "uncommitted pending value"
            with self.assertRaises(HTTPException) as caught:
                endpoint(db=source)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn(actor, source.dirty)
            self.assertEqual(actor.full_name, "uncommitted pending value")
            self.assertTrue(source.in_transaction())
            source.rollback()

    def test_flushed_source_changes_are_not_rolled_back(self):
        @read_snapshot()
        def endpoint(db):
            return True
        with Session(self.engine) as source:
            actor = source.get(User, self.actor_id)
            actor.full_name = "uncommitted flushed value"
            source.flush()
            self.assertFalse(source.dirty)
            with self.assertRaises(HTTPException) as caught:
                endpoint(db=source)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertTrue(source.in_transaction())
            self.assertEqual(source.scalar(text("SELECT full_name FROM users WHERE id=:id"), {"id": self.actor_id}),
                             "uncommitted flushed value")
            source.rollback()

    def test_core_source_writes_are_not_rolled_back(self):
        @read_snapshot()
        def endpoint(db):
            return True
        with Session(self.engine) as source:
            source.execute(update(User).where(User.id == self.actor_id).values(full_name="uncommitted core value"))
            self.assertFalse(source.dirty)
            with self.assertRaises(HTTPException) as caught:
                endpoint(db=source)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertTrue(source.in_transaction())
            self.assertEqual(source.scalar(text("SELECT full_name FROM users WHERE id=:id"), {"id": self.actor_id}),
                             "uncommitted core value")
            source.rollback()

    def test_explicit_source_transaction_is_not_ended(self):
        @read_snapshot()
        def endpoint(db):
            return True
        with Session(self.engine) as source:
            with source.begin() as owner:
                source.scalar(text("SELECT 1"))
                with self.assertRaises(HTTPException) as caught:
                    endpoint(db=source)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertTrue(owner.is_active)

    def test_borrowed_readonly_snapshot_keeps_caller_connection_and_transaction(self):
        @read_snapshot()
        def endpoint(db):
            return {"value": db.scalar(select(1)), "readonly": db.connection().exec_driver_sql("SHOW transaction_read_only").scalar_one()}
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            owner = connection.get_transaction()
            with Session(bind=connection) as source:
                source.scalar(text("SELECT 1"))
                self.assertEqual(endpoint(db=source), {"value": 1, "readonly": "on"})
                self.assertTrue(owner.is_active)
                self.assertFalse(connection.closed)
                self.assertEqual(self.engine.pool.checkedout(), 1)
            self.assertTrue(owner.is_active)

    def test_borrowed_readwrite_connection_is_rejected_without_ending_it(self):
        @read_snapshot()
        def endpoint(db):
            return True
        with self.engine.connect() as connection:
            with connection.begin() as owner:
                with Session(bind=connection) as source:
                    source.scalar(text("SELECT 1"))
                    with self.assertRaises(HTTPException) as caught:
                        endpoint(db=source)
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertTrue(owner.is_active)
                    self.assertFalse(connection.closed)
