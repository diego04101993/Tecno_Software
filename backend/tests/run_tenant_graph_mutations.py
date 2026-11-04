"""A10 closure inventory and certified mutation entry point (QA only).

Importing this module neither starts FastAPI nor opens a database. Evidence must
come from fresh test executions; missing evidence is a gap, never a default PASS.
Run with the isolated backend environment and an output directory outside the
checkout. Mutants are installed in memory by the A7-R3 identity-aware harness.
"""
import argparse
import ast
import csv
import inspect
import json
import os
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path


CENTRAL_GUARDS = frozenset({
    "app.services.tenant_ownership.context_for_actor",
    "app.services.tenant_ownership.assert_same_tenant_relation",
    "app.services.tenant_queries.tenant_safe_query",
    "app.services.tenant_queries.readable_query",
    "app.services.tenant_queries.read_integrity_predicate",
    "app.services.tenant_responses.tenant_safe_response",
    "app.services.tenant_responses.safe_model_read",
    "app.services.tenant_responses.read_snapshot",
    "app.services.tenant_mutations.validate_proposed_state",
    "app.services.tenant_mutations.preflight_relationship_graph",
    "app.services.tenant_mutations.write_guard",
    "app.services.tenant_mutations.player_write_guard",
    "app.services.tenant_mutations.delete_guard",
    "app.services.tenant_mutations.require_delete_coverage",
})


def live_endpoints():
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from app.api.router import api_router
    from app.api.routes import media

    rows = []
    for router, prefix in ((api_router, "/api"), (media.router, "")):
        for route in router.routes:
            if not isinstance(route, (APIRoute, APIWebSocketRoute)):
                continue
            fn = inspect.unwrap(route.endpoint)
            dependencies = set()

            def visit(dependant):
                for item in dependant.dependencies:
                    dependencies.add(item.call.__module__ + "." + item.call.__name__)
                    visit(item)

            visit(route.dependant)
            for method in sorted(route.methods if isinstance(route, APIRoute) else ("WS",)):
                rows.append(dict(METHOD=method, PATH=prefix + route.path,
                                 FUNCTION=fn.__module__ + "." + fn.__name__,
                                 LINE=inspect.getsourcelines(fn)[1],
                                 ACTOR_AUTH=sorted(dependencies)))
    # main.py has startup/create_all. Inspect its declarations without import.
    path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in fn.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"
                    and dec.func.attr in {"get", "head", "post", "patch", "put", "delete"}
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                rows.append(dict(METHOD=dec.func.attr.upper(), PATH=dec.args[0].value,
                                 FUNCTION="app.main." + fn.name, LINE=fn.lineno,
                                 ACTOR_AUTH=[]))
    return sorted(rows, key=lambda row: (row["PATH"], row["METHOD"]))


def source_graph():
    """Resolve source-level function calls; do not equate names with execution."""
    root = Path(__file__).resolve().parents[1]
    nodes = {}
    for path in sorted((root / "app").rglob("*.py")):
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        aliases = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = node.module + "." + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_aliases = dict(aliases)
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module and not child.level:
                    for alias in child.names:
                        local_aliases[alias.asname or alias.name] = child.module + "." + alias.name
            calls = set()
            references = [call.func for call in ast.walk(node) if isinstance(call, ast.Call)]
            references.extend(dec for dec in node.decorator_list if not isinstance(dec, ast.Call))
            for current in references:
                parts = []
                while isinstance(current, ast.Attribute):
                    parts.insert(0, current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    base = local_aliases.get(current.id, module + "." + current.id)
                    calls.add(".".join([base] + parts))
            nodes[module + "." + node.name] = dict(line=node.lineno, calls=calls)
    return nodes


def reachable_functions(name, graph):
    seen, pending = set(), [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, {}).get("calls", ()))
    return seen


def business_key(endpoint):
    """Reviewed aliases join alternate routes; remaining operations stay distinct."""
    path, method = endpoint["PATH"], endpoint["METHOD"]
    name = endpoint["FUNCTION"].rsplit(".", 1)[-1]
    if name in {"get_runtime_config", "get_channel_runtime_config", "get_player_config"}:
        return "channel_playback_runtime"
    if name in {"list_buttons", "list_touch_buttons", "create_button", "create_touch_button"}:
        return "touch_buttons_" + ("read" if method == "GET" else "create")
    if name in {"list_screens", "list_touch_screens", "create_screen", "create_touch_screen"}:
        return "touch_screens_" + ("read" if method == "GET" else "create")
    if "/campaigns/" in path and any(x in name for x in ("playlist", "sequence")):
        return name.replace("playlist", "campaign_items").replace("sequence", "campaign_items")
    if name in {"assign_campaign_to_channel", "replace_channel_campaign_assignment"}:
        return "campaign_channel_assignment"
    if name in {"get_layout_binding", "list_layout_binding_fields", "get_layout_data_preview",
                "validate_layout_binding", "get_layout_runtime_data"}:
        return "layout_bound_data_read"
    return endpoint["FUNCTION"] + ":" + method


def certificate_errors(record):
    """Verify expected-defense identity as well as a mutant's nonzero exit."""
    errors = []
    if record.get("mutation_result") != "DETECTED" or not record.get("detected"):
        errors.append("NOT_DETECTED")
    for key in ("errors", "harness_errors", "skipped", "non_discriminating", "invalid_mutations"):
        if record.get(key, 0):
            errors.append(key)
    for key in ("harness_health", "harness_health_before_controls", "harness_health_after_mutant"):
        value = record.get(key, {})
        if value.get("status") != "PASS" or value.get("issues"):
            errors.append(key)
    for key in ("control_original", "control_recompiled"):
        value = record.get(key, {})
        if (value.get("status") != "PASS" or not value.get("tests_run")
                or any(value.get(x, 0) for x in ("failures", "errors", "skipped"))):
            errors.append(key)
    provenance = record.get("mutation_provenance", {})
    if (provenance.get("MUTATION_APPLIED") != "YES" or not provenance.get("checked_before_test")
            or not provenance.get("BEFORE_FINGERPRINT")
            or provenance.get("BEFORE_FINGERPRINT") == provenance.get("AFTER_FINGERPRINT")):
        errors.append("PROVENANCE")
    mutant = record.get("mutant") or {}
    actual = set(map(tuple, mutant.get("assertion_sites", ())))
    expected = set(map(tuple, record.get("expected_defense_sites", ())))
    if (not mutant.get("failures") or mutant.get("errors") or mutant.get("skipped")
            or not actual or not actual <= expected):
        errors.append("EXPECTED_DEFENSE")
    return errors


def coverage_gaps(relations, endpoints, businesses, observations, certificates):
    """All evidence is supplied explicitly so negative controls can remove it."""
    gaps = []
    observed = {(r["EDGE"], r["PROFILE"], r["ENGINE"]) for r in observations
                if r.get("RESULT") == "PASS" and r.get("TEST")}
    for relation in relations:
        for profile in ("READ", "WRITE", "DELETE", "RUNTIME"):
            if profile not in relation["operations"]:
                if not dict(relation["exclusions"]).get(profile):
                    gaps.append(dict(TYPE="UNJUSTIFIED_EXCLUSION", KEY=relation["key"], DETAIL=profile))
                continue
            for engine in ("sqlite", "postgresql"):
                if (relation["key"], profile, engine) not in observed:
                    gaps.append(dict(TYPE="RUNTIME_EDGE_WITHOUT_TEST" if profile == "RUNTIME" else "RELATION_WITHOUT_TEST",
                                     KEY=relation["key"], DETAIL=profile + ":" + engine))
    for endpoint in endpoints:
        if endpoint.get("EXCLUSION"):
            continue
        key = endpoint["METHOD"] + " " + endpoint["PATH"]
        if not endpoint.get("CENTRAL_GUARD"):
            gaps.append(dict(TYPE="ENDPOINT_WITHOUT_GUARD", KEY=key, DETAIL=endpoint["FUNCTION"]))
        if not endpoint.get("TESTS"):
            gaps.append(dict(TYPE="ENDPOINT_WITHOUT_EXECUTED_TEST", KEY=key, DETAIL=endpoint["FUNCTION"]))
    indexed = {row["METHOD"] + " " + row["PATH"]: row for row in endpoints}
    for business in businesses:
        for key in business["ENTRYPOINTS"]:
            if key not in indexed or not indexed[key].get("TESTS"):
                gaps.append(dict(TYPE="ALTERNATE_ROUTE_WITHOUT_COVERAGE", KEY=business["BUSINESS_OPERATION"], DETAIL=key))
    for record in certificates:
        errors = certificate_errors(record)
        if errors:
            gaps.append(dict(TYPE="MUTATION_WITHOUT_EXPECTED_DEFENSE", KEY=record.get("mutation", "MISSING"), DETAIL=errors))
    return gaps


def mutation_runners():
    import test_tenant_relation_matrix as matrix
    import test_rbac_matrix as rbac
    from test_tenant_mutation_guards import run_a6_mutations
    return {
        "A6": run_a6_mutations, "A7": matrix.run_a7_mutations,
        "A7_R1": matrix.run_a7_r1_mutations,
        "historical": matrix.run_previous_mutations_with_a7_boundary,
        "A8": matrix.run_a8_mutations, "A9": rbac.run_a9_read_mutations,
        "A9_R1": rbac.run_a9_r1_mutations, "A9_R2": rbac.run_a9_r2_mutations,
        "A9_R3": rbac.run_a9_r3_mutations, "A9_R4": rbac.run_a9_r4_mutations,
        "A10_R1": rbac.run_a10_r1_mutations,
        "A10_R2": rbac.run_a10_r2_mutations,
    }


def external_output(value):
    path = Path(value).resolve()
    checkout = Path(__file__).resolve().parents[2]
    if path == checkout or checkout in path.parents:
        raise ValueError("A10 artifacts must be outside the checkout")
    return path


def write_csv(path, rows, columns=None):
    columns = columns or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                             if isinstance(value, (list, tuple, dict)) else value for key, value in row.items()})


def inventory():
    from app.services import tenant_relations as registry
    registry.validate_registry()
    graph = source_graph()
    policy = {(op.method, op.path): op for op in registry.OPERATIONS}
    endpoints = live_endpoints()
    if {(r["METHOD"], r["PATH"]) for r in endpoints} != set(policy):
        raise ValueError("Live endpoint inventory differs from explicit registry")
    for row in endpoints:
        op = policy[row["METHOD"], row["PATH"]]
        reachable = reachable_functions(row["FUNCTION"], graph)
        for dependency in row["ACTOR_AUTH"]:
            reachable.update(reachable_functions(dependency, graph))
        row.update(CATEGORY=op.kind, MODELS=list(op.models), EXCLUSION=op.exclusion or "",
                   BRANCH_SCOPE={name: registry.MODELS[name].branch_policy for name in op.models},
                   CENTRAL_GUARD=sorted(reachable & CENTRAL_GUARDS),
                   REACHABLE_SOURCE_FUNCTIONS=sorted(reachable), TESTS=[])
    grouped = defaultdict(list)
    for row in endpoints:
        if not row["EXCLUSION"]:
            grouped[business_key(row)].append(row)
    businesses = []
    for key, rows in sorted(grouped.items()):
        common = set(rows[0]["CENTRAL_GUARD"])
        for row in rows[1:]:
            common.intersection_update(row["CENTRAL_GUARD"])
        businesses.append(dict(BUSINESS_OPERATION=key,
                               ENTRYPOINTS=[r["METHOD"] + " " + r["PATH"] for r in rows],
                               COMMON_GUARD=sorted(common), ALTERNATE_ROUTE_TESTS=[]))
    relations = []
    for edge in registry.RELATION_SPECS:
        row = asdict(edge)
        row.update(key=edge.key, operations=sorted(edge.operations),
                   fingerprint=registry.relation_fingerprint(edge),
                   approved_fingerprint=registry.APPROVED_RELATION_FINGERPRINTS[edge.key])
        relations.append(row)
    return dict(models=[asdict(model) for model in registry.MODEL_SPECS],
                relations=relations, endpoints=endpoints, businesses=businesses)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mutation-group")
    args = parser.parse_args()
    if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
        raise RuntimeError("Use an explicitly isolated QA environment")
    output = external_output(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.mutation_group:
        runners = mutation_runners()
        if args.mutation_group not in runners:
            raise ValueError("Unknown mutation group")
        records = runners[args.mutation_group]()
        (output / ("mutations-" + args.mutation_group + ".json")).write_text(
            json.dumps(records, indent=2), encoding="utf-8")
        failed = [r["mutation"] for r in records if certificate_errors(r)]
        print(json.dumps(dict(group=args.mutation_group, total=len(records), invalid=failed)), flush=True)
        return int(bool(failed))
    data = inventory()
    (output / "LIVE_INVENTORY.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({key: len(value) for key, value in data.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
