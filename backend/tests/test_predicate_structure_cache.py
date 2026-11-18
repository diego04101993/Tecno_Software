"""Regressions: SQL construction reuse must never reuse DB decisions.

Run only with DATABASE_URL=sqlite:// and AUTO_SEED=false. The cache stores
SQL expression structure; every eligibility read still executes against its DB.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import uuid4

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Refusing non-isolated predicate-cache draft execution")

from sqlalchemy import create_engine, event, false, select, update
from sqlalchemy.orm import Session, aliased

from app.models.base import Base
from app.models.entities import Branch, Channel, Client
from app.services import tenant_queries as queries, tenant_relations as registry


def cache_control():
    helper = getattr(queries, "_shared_read_projection_expression", None)
    if helper is not None:
        return {"cache_clear": helper.cache_clear, "cache_info": helper.cache_info}
    return getattr(queries, "_qa_structure_cache_control", None)


class PredicateStructureCacheTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory(prefix="predicate-ast-draft-"))
        self.engine = create_engine("sqlite:///" + str(Path(directory) / "owned.sqlite"),
                                    connect_args={"check_same_thread": False},
                                    pool_size=4, max_overflow=0, hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.ids = {key: str(uuid4()) for key in ("A", "B", "A1", "A2", "B1", "CA")}
        with Session(self.engine) as db:
            db.add_all([Client(id=self.ids["A"], name="Synthetic A", slug="synthetic-a", status="active"),
                        Client(id=self.ids["B"], name="Synthetic B", slug="synthetic-b", status="active")])
            db.flush()
            db.add_all([Branch(id=self.ids["A1"], client_id=self.ids["A"], name="A1", code="A1", is_active=True),
                        Branch(id=self.ids["A2"], client_id=self.ids["A"], name="A2", code="A2", is_active=True),
                        Branch(id=self.ids["B1"], client_id=self.ids["B"], name="B1", code="B1", is_active=True)])
            db.flush()
            db.add(Channel(id=self.ids["CA"], client_id=self.ids["A"], branch_id=self.ids["A1"], name="A"))
            db.commit()
        control = cache_control()
        if control is not None:
            control["cache_clear"]()
            self.addCleanup(control["cache_clear"])

    def predicate(self, db, cls=Branch, *, client="A", branch=None, profile="RUNTIME", statement=None):
        statement = select(cls.id) if statement is None else statement
        client_id = self.ids.get(client, client) if client is not None else None
        branch_id = self.ids.get(branch, branch) if branch is not None else None
        return statement, queries._read_projection_predicate(
            statement, cls, db, profile, client_id, branch_id)

    def read_ids(self, cls=Branch, **kwargs):
        # A real independent Session and a fresh SQL SELECT on every call.
        with Session(self.engine) as db:
            statement, predicate = self.predicate(db, cls, **kwargs)
            return set(db.scalars(statement.where(predicate)))

    def test_construction_is_reused_across_independent_sessions(self):
        with patch.object(queries, "read_integrity_predicate",
                          wraps=queries.read_integrity_predicate) as build:
            self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})
            self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})
            self.assertEqual(build.call_count, 1, "Cache SQL construction, never SQL results")

    def test_tenant_bindings_remain_distinct_after_cache_fill(self):
        expected = ({self.ids["A1"], self.ids["A2"]}, {self.ids["B1"]})
        for _ in range(3):
            self.assertEqual(self.read_ids(client="A"), expected[0])
            self.assertEqual(self.read_ids(client="B"), expected[1])

    def test_runtime_client_suspension_is_visible_after_cache_fill(self):
        self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})
        with self.engine.begin() as connection:
            connection.execute(update(Client).where(Client.id == self.ids["A"]).values(status="inactive"))
        self.assertEqual(self.read_ids(), set())
        self.assertEqual(self.read_ids(client="B"), {self.ids["B1"]})

    def test_foreign_channel_branch_is_rejected_after_cache_fill(self):
        self.assertEqual(self.read_ids(Channel), {self.ids["CA"]})
        with self.engine.begin() as connection:
            connection.execute(update(Channel).where(Channel.id == self.ids["CA"])
                               .values(branch_id=self.ids["B1"]))
        self.assertEqual(self.read_ids(Channel), set())

    def test_branch_context_and_profile_do_not_share_bindings(self):
        for _ in range(2):
            self.assertEqual(self.read_ids(branch="A1"), {self.ids["A1"]})
            self.assertEqual(self.read_ids(branch="A2"), {self.ids["A2"]})
        with self.engine.begin() as connection:
            connection.execute(update(Branch).where(Branch.id == self.ids["A1"]).values(is_active=False))
        self.assertEqual(self.read_ids(branch="A1", profile="RUNTIME"), set())
        self.assertEqual(self.read_ids(branch="A1", profile="READ"), {self.ids["A1"]})

    def test_new_rows_are_visible_after_cache_fill(self):
        previous = self.read_ids()
        identity = str(uuid4())
        with Session(self.engine) as db:
            db.add(Branch(id=identity, client_id=self.ids["A"], name="New", code="NEW", is_active=True))
            db.commit()
        self.assertEqual(self.read_ids(), previous | {identity})

    def test_noncanonical_alias_options_and_correlation_keep_fallback(self):
        alias = aliased(Branch, name="synthetic_branch_alias")
        statements = (
            select(Branch.id).join(alias, alias.id == Branch.id),
            select(Branch.id).execution_options(stream_results=True),
            select(Branch.id).correlate(None),
        )
        with patch.object(queries, "read_integrity_predicate",
                          wraps=queries.read_integrity_predicate) as build:
            for _ in range(2):
                for statement in statements:
                    self.assertEqual(self.read_ids(statement=statement), {self.ids["A1"], self.ids["A2"]})
            self.assertEqual(build.call_count, 6)

    def test_builder_and_registry_replacement_cannot_reuse_old_policy(self):
        self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})
        with patch.object(queries, "_active", lambda entity, name: false()):
            self.assertEqual(self.read_ids(), set())
        self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})
        modified = dict(registry.MODELS)
        modified["Branch"] = replace(modified["Branch"], owner_field="id")
        with patch.object(registry, "MODELS", MappingProxyType(modified)):
            self.assertEqual(self.read_ids(), set())
        self.assertEqual(self.read_ids(), {self.ids["A1"], self.ids["A2"]})

    def test_cache_is_bounded_and_eviction_rebuilds_expression(self):
        control = cache_control()
        self.assertIsNotNone(control, "Bounded structure cache required")
        with Session(self.engine) as db:
            _, first = self.predicate(db)
            limit = control["cache_info"]().maxsize
            self.assertEqual(limit, 128)
            for index in range(limit):
                self.predicate(db, client="unmatched-synthetic-" + str(index))
            self.assertLessEqual(control["cache_info"]().currsize, limit)
            _, rebuilt = self.predicate(db)
            self.assertIsNot(rebuilt, first)

    def test_concurrent_requests_each_execute_sql_and_keep_tenant_scope(self):
        statements = []
        lock = threading.Lock()

        def record(connection, cursor, statement, parameters, context, many):
            with lock:
                statements.append(1)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            clients = ["A", "B"] * 6
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(lambda client: self.read_ids(client=client), clients))
            for client, result in zip(clients, rows):
                expected = {self.ids["A1"], self.ids["A2"]} if client == "A" else {self.ids["B1"]}
                self.assertEqual(result, expected)
            self.assertEqual(len(statements), 12, "Each request must execute fresh eligibility SQL")
        finally:
            event.remove(self.engine, "before_cursor_execute", record)


if __name__ == "__main__":
    unittest.main()
