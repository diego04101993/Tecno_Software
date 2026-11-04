"""Catalog completeness is checked against ORM declarations, not expected counts alone."""
import os
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from types import MappingProxyType
from fastapi import HTTPException

from app.services import tenant_relations as registry


class TenantRelationRegistryTests(TestCase):
    def test_approved_manifest_fingerprint_anchor_is_stable(self):
        from hashlib import sha256
        # Independently exported from the approved architecture CSV, not from
        # the live RelationSpec declarations. A7-R1 updates nine ORM-only
        # fingerprints after exact FK introspection; the source CSV stays intact.
        self.assertEqual(registry.APPROVED_POLICY_SOURCE_SHA256,
                         "4b64f790cbba3434138efbc4a37cb863efba81b632439c27b4a21292e84d9a8b")
        encoded = json.dumps(dict(registry.APPROVED_RELATION_FINGERPRINTS),
                             sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(sha256(encoded).hexdigest(),
                         "6ee040e6c18ab7574c27dfba93332a268016fc71612efe1d1c7f9fc5fe9aea37")

    def test_semantic_policy_drift_rejected_by_validator(self):
        edge = registry.relation_spec("Channel.branch_id")
        changes = (
            {"owner_kind": "structural-owner"}, {"authoritative_owner": ""},
            {"authoritative_owner": "Channel.branch_id -> Branch.client_id -> Client"},
            {"operations": edge.operations - {"READ"}, "exclusions": edge.exclusions + (("READ", "changed"),)},
            {"operations": edge.operations - {"RUNTIME"}, "exclusions": edge.exclusions + (("RUNTIME", "changed"),)},
            {"conflict_strategy": "OTHER_CONFLICT"}, {"nullable": not edge.nullable},
            {"on_delete": "SET NULL"}, {"context_constraint": "changed"},
            {"read_policy": "FOREIGN_FIELD_NEUTRALIZED"}, {"delete_policy": "changed"},
        )
        for change in changes:
            specs = tuple(replace(e, **change) if e.key == edge.key else e for e in registry.RELATION_SPECS)
            with self.subTest(change=change), patch.object(registry, "RELATION_SPECS", specs), patch.object(
                    registry, "RELATIONS", MappingProxyType({e.key: e for e in specs})):
                with self.assertRaises(HTTPException):
                    registry.validate_registry()

    def test_removed_and_ghost_relation_rejected_by_validator(self):
        for specs in (registry.RELATION_SPECS[:-1], registry.RELATION_SPECS + (
                replace(registry.RELATION_SPECS[0], field="ghost_id"),)):
            # Keep lookup and tuple consistent: rejection must be semantic, not
            # merely a mismatched dictionary length or an external count assert.
            with patch.object(registry, "RELATION_SPECS", specs), patch.object(
                    registry, "RELATIONS", MappingProxyType({e.key: e for e in specs})):
                with self.assertRaises(HTTPException):
                    registry.validate_registry()

    def test_exact_catalog_and_all_operation_policies(self):
        self.assertEqual(len(registry.MODEL_SPECS), 36)
        self.assertEqual(len(registry.RELATION_SPECS), 87)
        self.assertTrue(registry.validate_registry())
        self.assertEqual(sum(e.on_delete == "CASCADE" for e in registry.RELATION_SPECS), 59)
        self.assertEqual(sum(e.on_delete == "SET NULL" for e in registry.RELATION_SPECS), 27)
        self.assertEqual(sum(e.on_delete is None for e in registry.RELATION_SPECS), 1)
        for kind, count in (("direct-owner", 20), ("structural-owner", 15), ("context", 52)):
            self.assertEqual(sum(e.owner_kind == kind for e in registry.RELATION_SPECS), count)
        for edge in registry.RELATION_SPECS:
            with self.subTest(edge=edge.key):
                self.assertIn(edge.model, registry.MODELS)
                self.assertIn(edge.target, registry.MODELS)
                self.assertTrue(edge.authoritative_owner)
                self.assertTrue(edge.context_constraint)
                self.assertEqual(edge.operations | {op for op, why in edge.exclusions},
                                 {"READ", "WRITE", "DELETE", "RUNTIME"})
                self.assertTrue(all(why for _, why in edge.exclusions))
                self.assertEqual(edge.conflict_strategy, "TENANT_RELATION_CONFLICT")

    def test_all_model_owner_paths_reach_client_without_cycles(self):
        for model in registry.MODEL_SPECS:
            visited = set()
            while model.owner_kind != "root":
                self.assertNotIn(model.name, visited)
                visited.add(model.name)
                edge = registry.relation_spec(model.name + "." + model.owner_field)
                self.assertEqual(edge.target, model.owner_target)
                model = registry.model_spec(model.owner_target)
            self.assertEqual(model.name, "Client")

    def test_new_fk_or_phantom_registration_is_rejected(self):
        baseline = registry.inspect_registry()
        for extra in (True, False):
            models, edges, columns, orm = baseline
            changed = set(edges)
            if extra:
                changed.add(("Channel", "unreviewed_id", "Client", True, "CASCADE"))
            else:
                changed.remove(next(e for e in edges if e[0] == "Channel"))
            with self.subTest(extra=extra), self.assertRaises(HTTPException):
                registry.validate_registry((models, changed, columns, orm))

    def test_unregistered_model_relation_and_owner_cycle_fail_closed(self):
        for lookup, name in ((registry.model_spec, "Invented"), (registry.relation_spec, "Channel.invented_id")):
            with self.assertRaises(HTTPException):
                lookup(name)
        specs = tuple(replace(s, owner_target="Channel", owner_field="branch_id")
                      if s.name == "Channel" else s for s in registry.MODEL_SPECS)
        with patch.object(registry, "MODEL_SPECS", specs), patch.object(
                registry, "MODELS", MappingProxyType({s.name: s for s in specs})):
            with self.assertRaises(HTTPException):
                registry.validate_registry()

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            registry.MODELS["new"] = None
        with self.assertRaises(Exception):
            registry.RELATION_SPECS[0].target = "Client"

    def test_each_central_import_has_no_side_effects(self):
        # Preload only the external framework, then forbid environment, directory,
        # filesystem, model, engine and connection effects in fresh interpreters.
        script = r'''
import os, sys, json, pathlib, builtins, logging
from unittest.mock import patch
import sqlalchemy, fastapi
from app.models import entities
from app.models.base import Base
before_env = dict(os.environ)
before_models = tuple(sorted((m.class_.__name__, tuple(c.key for c in m.columns)) for m in Base.registry.mappers))
def denied(*args, **kwargs): raise AssertionError("import side effect")
module = sys.argv[1]
with patch("sqlalchemy.create_engine", denied), patch("sqlalchemy.engine.Engine.connect", denied), patch("os.mkdir", denied), patch("pathlib.Path.mkdir", denied), patch("builtins.open", denied), patch("pathlib.Path.open", denied), patch("logging.Logger._log", denied):
 __import__("app.services." + module)
assert dict(os.environ) == before_env
assert tuple(sorted((m.class_.__name__, tuple(c.key for c in m.columns)) for m in Base.registry.mappers)) == before_models
print("PURE_IMPORT")
'''
        backend = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=backend, PYTHONDONTWRITEBYTECODE="1")
        for module in ("tenant_relations", "tenant_ownership", "tenant_queries", "tenant_responses", "tenant_mutations"):
            result = subprocess.run([sys.executable, "-B", "-c", script, module],
                                    capture_output=True, text=True, env=env)
            with self.subTest(module=module):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "PURE_IMPORT")
