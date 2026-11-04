"""Every existing HTTP/WS path has an explicit A6 policy or named exception."""
import ast
from pathlib import Path
from unittest import TestCase
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.services.tenant_relations import OPERATIONS, MODELS


class TenantEndpointCoverageTests(TestCase):
    def test_routes_match_explicit_catalog_without_startup(self):
        from app.api.router import api_router
        from app.api.routes import media
        actual = set()
        for router, prefix in ((api_router, "/api"), (media.router, "")):
            for route in router.routes:
                if isinstance(route, APIRoute):
                    for method in route.methods:
                        actual.add((method, prefix + route.path, route.endpoint.__name__))
                elif isinstance(route, APIWebSocketRoute):
                    actual.add(("WS", prefix + route.path, route.endpoint.__name__))
        # Read the root declaration without importing startup/create_all.
        main = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"
                            and dec.args and isinstance(dec.args[0], ast.Constant)):
                        if dec.func.attr in {"get", "post", "put", "patch", "delete", "head"}:
                            actual.add((dec.func.attr.upper(), dec.args[0].value, node.name))
        expected = {(o.method, o.path, o.function) for o in OPERATIONS}
        self.assertEqual(actual, expected)
        self.assertEqual(sum(o.method != "WS" for o in OPERATIONS), 153)
        self.assertEqual(sum(o.method == "WS" for o in OPERATIONS), 2)

    def test_models_and_exclusions_are_explicit_not_implicit_allow(self):
        self.assertEqual(len({(o.method, o.path) for o in OPERATIONS}), len(OPERATIONS))
        for op in OPERATIONS:
            with self.subTest(method=op.method, path=op.path):
                self.assertTrue(op.kind)
                self.assertTrue(op.source.startswith("backend/app/"))
                self.assertTrue(op.models or op.exclusion)
                self.assertTrue(set(op.models) <= MODELS.keys())
        public_media = [o for o in OPERATIONS if o.path == "/media/{media_path:path}"]
        self.assertEqual(len(public_media), 2)
        self.assertTrue(all(o.exclusion == "H1_PUBLIC_MEDIA" for o in public_media))


class A10CoverageGeneratorTests(TestCase):
    def test_live_inventory_reconciles_functions_not_only_route_counts(self):
        from run_tenant_graph_mutations import CENTRAL_GUARDS, inventory
        data = inventory()
        actual = {(r["METHOD"], r["PATH"], r["FUNCTION"].rsplit(".", 1)[-1])
                  for r in data["endpoints"]}
        self.assertEqual(actual, {(o.method, o.path, o.function) for o in OPERATIONS})
        self.assertEqual(len(data["models"]), 36)
        self.assertEqual(len(data["relations"]), 87)
        for relation in data["relations"]:
            self.assertEqual(relation["fingerprint"], relation["approved_fingerprint"])
        self.assertNotIn("app.services.tenant_ownership.RowRef", CENTRAL_GUARDS)
        self.assertNotIn("app.services.tenant_mutations.MutationIntent", CENTRAL_GUARDS)
        for endpoint in data["endpoints"]:
            if not endpoint["EXCLUSION"]:
                self.assertTrue(endpoint["CENTRAL_GUARD"], endpoint["PATH"])

    def test_missing_runtime_and_engine_evidence_are_gaps(self):
        from run_tenant_graph_mutations import coverage_gaps
        relation = dict(key="ContentItem.folder_id", operations=["READ", "RUNTIME"],
                        exclusions=[("WRITE", "synthetic negative control"),
                                    ("DELETE", "synthetic negative control")])
        rows = [dict(EDGE=relation["key"], PROFILE=profile, ENGINE=engine,
                     RESULT="PASS", TEST="independent_test")
                for profile in ("READ", "RUNTIME") for engine in ("sqlite", "postgresql")]
        self.assertEqual(coverage_gaps([relation], [], [], rows, []), [])
        gaps = coverage_gaps([relation], [], [], rows[:-1], [])
        self.assertEqual(gaps, [dict(TYPE="RUNTIME_EDGE_WITHOUT_TEST",
                                    KEY=relation["key"], DETAIL="RUNTIME:postgresql")])
        rows[0]["TEST"] = ""
        self.assertTrue(any(g["TYPE"] == "RELATION_WITHOUT_TEST"
                            for g in coverage_gaps([relation], [], [], rows, [])))

    def test_denial_without_guard_and_untested_alternate_do_not_close_coverage(self):
        from run_tenant_graph_mutations import coverage_gaps
        endpoint = dict(METHOD="POST", PATH="/synthetic", FUNCTION="synthetic.create",
                        CENTRAL_GUARD=[], TESTS=[])
        business = dict(BUSINESS_OPERATION="synthetic", ENTRYPOINTS=["POST /synthetic"])
        types = {g["TYPE"] for g in coverage_gaps([], [endpoint], [business], [], [])}
        self.assertEqual(types, {"ENDPOINT_WITHOUT_GUARD", "ENDPOINT_WITHOUT_EXECUTED_TEST",
                                 "ALTERNATE_ROUTE_WITHOUT_COVERAGE"})

    def test_unexplained_not_applicable_is_a_gap(self):
        from run_tenant_graph_mutations import coverage_gaps
        gaps = coverage_gaps([dict(key="synthetic", operations=[], exclusions=[])], [], [], [], [])
        self.assertEqual({g["DETAIL"] for g in gaps}, {"READ", "WRITE", "DELETE", "RUNTIME"})
        self.assertTrue(all(g["TYPE"] == "UNJUSTIFIED_EXCLUSION" for g in gaps))

    def test_mutant_failure_without_certificate_is_not_detection(self):
        from run_tenant_graph_mutations import certificate_errors, coverage_gaps
        bad = dict(mutation="synthetic", detected=True, mutation_result="DETECTED",
                   mutant=dict(failures=1, errors=0, assertion_sites=[["wrong", "defense", 1]]),
                   expected_defense_sites=[["expected", "defense", 2]])
        errors = certificate_errors(bad)
        self.assertIn("EXPECTED_DEFENSE", errors)
        self.assertIn("PROVENANCE", errors)
        self.assertIn("harness_health", errors)
        self.assertIn("control_original", errors)
        self.assertEqual(coverage_gaps([], [], [], [], [bad])[0]["TYPE"],
                         "MUTATION_WITHOUT_EXPECTED_DEFENSE")

    def test_output_cannot_write_into_checkout(self):
        from run_tenant_graph_mutations import external_output
        checkout = Path(__file__).resolve().parents[2]
        for path in (checkout, checkout / "generated", checkout / "backend" / ".." / "report"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                external_output(path)

    def test_business_map_has_all_named_alternate_surfaces(self):
        from run_tenant_graph_mutations import inventory
        businesses = {row["BUSINESS_OPERATION"]: row for row in inventory()["businesses"]}
        for name, minimum in (("channel_playback_runtime", 3), ("touch_buttons_create", 2),
                              ("touch_screens_create", 2), ("get_campaign_items", 2),
                              ("campaign_channel_assignment", 2), ("layout_bound_data_read", 5)):
            with self.subTest(operation=name):
                self.assertGreaterEqual(len(businesses[name]["ENTRYPOINTS"]), minimum)
