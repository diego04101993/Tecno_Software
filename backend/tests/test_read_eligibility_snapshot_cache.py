"""Scoped SQL predicate reuse preserves eligibility and caller authorization."""

from unittest.mock import patch

from sqlalchemy import bindparam, select
from sqlalchemy.orm import aliased

from app.api.routes import channels
from app.models.entities import Branch, Channel
from app.services import player_runtime, tenant_queries, tenant_responses
from app.services.db_snapshots import readonly_snapshot_session
from app.services.tenancy import apply_client_filter
from test_tenant_isolation import TenantFixture


MEMO = "a9_read_predicates"


class ReadEligibilitySnapshotCacheTests(TenantFixture):
    def setUp(self):
        super().setUp()
        self.a2_branch = self.add(Branch, client_id=self.tenants["A"].id, name="Branch A2", code="A2")
        self.a2_channel = self.add(
            Channel, client_id=self.tenants["A"].id, branch_id=self.a2_branch.id, name="Channel A2",
        )
        self.db.commit()

    def test_exact_model_profile_and_scopes_reuse_only_the_predicate(self):
        self.a2_branch.is_active = False
        self.db.commit()
        a = self.tenants["A"].id
        b = self.tenants["B"].id
        a_branch = self.res["A"]["branch"].id
        b_branch = self.res["B"]["branch"].id
        with readonly_snapshot_session(self.db) as db:
            db.info[MEMO] = {}
            with patch.object(tenant_queries, "read_integrity_predicate",
                              wraps=tenant_queries.read_integrity_predicate) as build:
                first = tenant_queries.readable_query(select(Branch.id), Branch, db=db, client_id=a)
                second = tenant_queries.readable_query(select(Branch), "Branch", db=db, client_id=a)
                self.assertEqual(build.call_count, 1)
                self.assertEqual(set(db.scalars(first)), {a_branch, self.a2_branch.id})
                self.assertEqual({row.id for row in db.scalars(second)}, {a_branch, self.a2_branch.id})

                cases = [
                    (Branch, {"client_id": b}, {b_branch}),
                    (Branch, {"client_id": a, "branch_id": a_branch}, {a_branch}),
                    (Branch, {"client_id": a, "branch_id": self.a2_branch.id}, {self.a2_branch.id}),
                    (Branch, {"client_id": a, "branch_id": b_branch}, set()),
                    (Branch, {"client_id": a, "profile": "RUNTIME"}, {a_branch}),
                    (Channel, {"client_id": a}, {self.res["A"]["channel"].id, self.a2_channel.id}),
                ]
                for model, options, expected in cases:
                    with self.subTest(model=model.__name__, **options):
                        for _ in range(2):
                            stmt = tenant_queries.readable_query(select(model.id), model, db=db, **options)
                            self.assertEqual(set(db.scalars(stmt)), expected)
                self.assertEqual(build.call_count, 1 + len(cases))

    def test_aliases_and_preconfigured_selects_do_not_enter_the_memo(self):
        a_branch = self.res["A"]["branch"].id
        b_branch = self.res["B"]["branch"].id
        first_alias = aliased(Branch, name="first_branch_scope")
        second_alias = aliased(Branch, name="second_branch_scope")
        statements = (
            (select(Branch.id).join(first_alias, first_alias.id == Branch.id)
             .where(first_alias.id == a_branch), a_branch),
            (select(Branch.id).join(second_alias, second_alias.id == Branch.id)
             .where(second_alias.id == b_branch), b_branch),
            (select(Branch.id).where(Branch.id == a_branch).limit(1), a_branch),
            (select(Branch.id).where(Branch.id == b_branch).execution_options(stream_results=True), b_branch),
        )
        with readonly_snapshot_session(self.db) as db:
            db.info[MEMO] = {}
            with patch.object(tenant_queries, "read_integrity_predicate",
                              wraps=tenant_queries.read_integrity_predicate) as build:
                for statement, expected in statements:
                    query = tenant_queries.readable_query(statement, Branch, db=db)
                    self.assertEqual(list(db.scalars(query)), [expected])
                    self.assertEqual(query.get_execution_options().get("stream_results"),
                                     statement.get_execution_options().get("stream_results"))
                core = tenant_queries.readable_query(select(Branch.__table__.c.id), Branch, db=db)
                self.assertEqual(set(db.scalars(core)), {a_branch, self.a2_branch.id, b_branch})
                explicit_correlation = tenant_queries.readable_query(
                    select(Branch.id).correlate(None), Branch, db=db,
                )
                self.assertEqual(set(db.scalars(explicit_correlation)), {a_branch, self.a2_branch.id, b_branch})
                # The existing builder accepts an aliased SELECT with canonical
                # eligibility. Its original alias must remain on the statement.
                alias_only = tenant_queries.readable_query(select(first_alias), Branch, db=db)
                self.assertIs(alias_only.column_descriptions[0]["entity"], first_alias)
                self.assertEqual(build.call_count, len(statements) + 3)
                self.assertEqual(db.info[MEMO], {})

    def test_bound_expression_scopes_are_built_independently(self):
        with readonly_snapshot_session(self.db) as db:
            db.info[MEMO] = {}
            with patch.object(tenant_queries, "read_integrity_predicate",
                              wraps=tenant_queries.read_integrity_predicate) as build:
                for side in ("A", "B"):
                    parameters = {
                        f"client_{side}": self.tenants[side].id,
                        f"branch_{side}": self.res[side]["branch"].id,
                    }
                    query = tenant_queries.readable_query(
                        select(Branch.id), Branch, db=db,
                        client_id=bindparam(f"client_{side}"), branch_id=bindparam(f"branch_{side}"),
                    )
                    self.assertEqual(list(db.scalars(query, parameters)), [parameters[f"branch_{side}"]])
                self.assertEqual(build.call_count, 2)
                self.assertEqual(db.info[MEMO], {})

    def test_caller_authorization_is_applied_to_each_statement_after_reuse(self):
        a_channel = self.res["A"]["channel"].id
        b_channel = self.res["B"]["channel"].id
        with readonly_snapshot_session(self.db) as db:
            db.info[MEMO] = {}
            with patch.object(tenant_queries, "read_integrity_predicate",
                              wraps=tenant_queries.read_integrity_predicate) as build:
                for actor, grants, expected in (
                    (self.users["A_client_admin"], (a_channel, b_channel), {a_channel}),
                    (self.users["B_client_admin"], (a_channel, b_channel), {b_channel}),
                    (self.users["A_client_admin"], (self.a2_channel.id,), {self.a2_channel.id}),
                    (self.users["A_client_admin"], (), set()),
                ):
                    statement = tenant_queries.readable_query(select(Channel.id), Channel, db=db)
                    statement = apply_client_filter(statement, Channel, actor).where(Channel.id.in_(grants))
                    self.assertEqual(set(db.scalars(statement)), expected)
                self.assertEqual(build.call_count, 1)

    def test_http_reloads_actor_and_clears_projection_memo_on_success_and_failure(self):
        projects = tenant_responses.safe_model_read
        route_query = channels.readable_query
        projected = []

        def scoped_query(statement, model, **options):
            self.assertNotIn(MEMO, options["db"].info)
            return route_query(statement, model, **options)

        def capture(row, schema, db, **options):
            if type(row) is Channel:
                projected.append((db, db.info[MEMO]))
            return projects(row, schema, db, **options)

        def assert_ids(who, *expected, **params):
            response = self.request("GET", "/api/channels/", who=who, params=params)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({row["id"] for row in response.json()}, set(expected))

        operator = self.users["A_operator"]
        with patch.object(channels, "readable_query", side_effect=scoped_query):
            with patch.object(tenant_responses, "safe_model_read", side_effect=capture):
                assert_ids("A_operator", self.res["A"]["channel"].id)
                assert_ids("B_client_admin", self.res["B"]["channel"].id)
                denied = self.request("GET", "/api/channels/", who="A_operator",
                                      params={"branch_id": self.a2_branch.id})
                self.assertEqual(denied.status_code, 403)
                operator.branch_id = self.a2_branch.id
                self.db.commit()
                assert_ids("A_operator", self.a2_channel.id, branch_id=self.a2_branch.id)
                operator.status = "suspended"
                operator.is_active = False
                self.db.commit()
                self.assertEqual(self.request("GET", "/api/channels/", who="A_operator").status_code, 403)

            def fail(row, schema, db, **options):
                result = capture(row, schema, db, **options)
                if type(row) is Channel:
                    raise RuntimeError("synthetic eligibility projection failure")
                return result

            with patch.object(tenant_responses, "safe_model_read", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "synthetic eligibility projection failure"):
                    self.request("GET", "/api/channels/", who="B_client_admin")

        self.assertEqual(len(projected), 4)
        self.assertEqual(len({id(db) for db, _ in projected}), len(projected))
        self.assertEqual(len({id(memo) for _, memo in projected}), len(projected))
        for db, _ in projected:
            self.assertNotIn(MEMO, db.info)
        self.assertNotIn(MEMO, self.db.info)

    def test_runtime_and_non_projection_sessions_build_without_a_memo(self):
        route_query = player_runtime.readable_query
        runtime_calls = []

        def runtime_query(statement, model, **options):
            self.assertNotIn(MEMO, options["db"].info)
            runtime_calls.append(model)
            return route_query(statement, model, **options)

        with patch.object(player_runtime, "readable_query", side_effect=runtime_query):
            response = self.request("GET", f"/api/channels/{self.res['A']['channel'].id}/runtime-config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(len(runtime_calls), 0)
        self.assertEqual(response.json()["videowall"], {"enabled": False})

        with patch.object(tenant_queries, "read_integrity_predicate",
                          wraps=tenant_queries.read_integrity_predicate) as build:
            for _ in range(2):
                tenant_queries.readable_query(select(Branch.id), Branch, db=self.db)
            self.assertNotIn(MEMO, self.db.info)
            with readonly_snapshot_session(self.db) as snapshot:
                for _ in range(2):
                    tenant_queries.readable_query(select(Branch.id), Branch, db=snapshot)
                self.assertNotIn(MEMO, snapshot.info)
            self.assertEqual(build.call_count, 4)
