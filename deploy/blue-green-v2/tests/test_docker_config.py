from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from docker_config import parse_optional_config
from gates import BACKEND_ARGV, LEGACY_BLUE_CLASS, REVISION_LABEL, TREE_LABEL, GateError, live_identity
from legacy_frontend_blue import R3_POLICY
from qa_docker import LocalDocker
from qualify_r3 import qualify_role
from release_state import Stop, digest
from test_gates import api_slot
from test_legacy_blue import FixtureProbe, envelope, legacy_fixture
from test_qualify_r3 import IDS, RELEASE, fixture as qualification_fixture


DEFAULTS = {
    "Cmd": [], "Entrypoint": [], "Env": [], "Labels": {}, "OnBuild": [],
    "User": "", "Volumes": {}, "WorkingDir": "",
}
VALUES = {
    "Cmd": ["command", "arg"], "Entrypoint": ["/entry"], "Env": ["A=B"],
    "Labels": {"key": "value"}, "OnBuild": ["RUN true"], "User": "1000",
    "Volumes": {"/cache": {}}, "WorkingDir": "/app",
}


class DockerConfigTests(unittest.TestCase):
    def test_each_omitted_field_has_only_its_typed_default(self):
        for field, default in DEFAULTS.items():
            with self.subTest(field=field):
                raw = {key: value for key, value in VALUES.items() if key != field}
                start = deepcopy(raw)
                parsed = parse_optional_config(raw)
                self.assertEqual(parsed, {**VALUES, field: default})
                self.assertIs(type(parsed[field]), type(default))
                self.assertEqual(raw, start)
                self.assertNotIn(field, raw)

    def test_empty_label_map_and_missing_have_same_parsed_semantics(self):
        self.assertEqual(parse_optional_config({}), parse_optional_config({"Labels": {}}))

    def test_older_explicit_null_fields_use_the_same_defaults(self):
        self.assertEqual(parse_optional_config(dict.fromkeys(DEFAULTS)), DEFAULTS)

    def test_invalid_fields_are_rejected_instead_of_coerced(self):
        invalid = {
            "Cmd": "command", "Entrypoint": [123], "Env": {}, "Labels": {"key": 123},
            "OnBuild": {"RUN": "true"}, "User": 0, "Volumes": {"/cache": []}, "WorkingDir": [],
        }
        for field, value in invalid.items():
            with self.subTest(field=field), self.assertRaisesRegex(Stop, f"DOCKER_CONFIG_{field.upper()}_INVALID"):
                parse_optional_config({field: value})

    def test_missing_command_is_valid_if_a_consumer_expects_no_command(self):
        self.assertEqual(parse_optional_config({"Env": ["A=B"]})["Cmd"], [])

    def test_qualification_rejects_missing_required_image_command(self):
        container, image = qualification_fixture("api")
        del image["Config"]["Cmd"]
        with self.assertRaisesRegex(Stop, "R3_COMMAND_MISMATCH"):
            qualify_role("api", RELEASE, IDS["api"], container, container, image, BACKEND_ARGV)

    def test_qualification_accepts_unset_entrypoint_but_rejects_override(self):
        container, image = qualification_fixture("api")
        del image["Config"]["Entrypoint"]
        del container["Config"]["Entrypoint"]
        self.assertIsNone(qualify_role("api", RELEASE, IDS["api"], container, container, image,
                                      BACKEND_ARGV)["entrypoint"])
        image["Config"]["Entrypoint"] = ["/alternate"]
        with self.assertRaisesRegex(Stop, "R3_COMMAND_MISMATCH"):
            qualify_role("api", RELEASE, IDS["api"], container, container, image, BACKEND_ARGV)

    def native_identity(self, fixture):
        return live_identity(*fixture, "api", "GREEN", BACKEND_ARGV, 90122)

    def test_missing_labels_on_native_image_fail_provenance(self):
        fixture = api_slot()
        del fixture[1]["Config"]["Labels"]
        with self.assertRaisesRegex(GateError, "WRONG_OCI_LABELS"):
            self.native_identity(fixture)

    def test_disposable_qa_rejects_unlabeled_native_artifact_with_provenance_error(self):
        engine = json.dumps({"OSType": "linux", "NCPU": 4, "MemTotal": 8_000_000_000})
        docker = LocalDocker()
        with patch.object(docker, "run", return_value=engine), patch.object(
            docker, "inspect", side_effect=lambda kind, image: {"Id": image, "Config": {}}
        ), self.assertRaisesRegex(RuntimeError, "exact sealed release image/OCI labels"):
            docker.verify_inputs()

    def test_native_image_accepts_real_labels_that_match_its_binding(self):
        fixture = api_slot()
        identity = self.native_identity(fixture)
        self.assertEqual(identity["revision"], fixture[-1]["revision"])
        self.assertEqual(identity["tree"], fixture[-1]["tree"])

    def test_missing_backend_runtime_env_fails_the_runtime_guard(self):
        fixture = api_slot()
        del fixture[0]["Config"]["Env"]
        with self.assertRaisesRegex(GateError, "UNSAFE_BACKEND_RUNTIME"):
            self.native_identity(fixture)

    def test_legacy_backend_without_image_labels_passes_exact_manifest_classifier(self):
        fixture = legacy_fixture()
        item, image, _, _, binding = fixture
        item["Image"] = image["Id"] = binding["image_id"] = R3_POLICY.backend_image_id
        binding.update(revision=R3_POLICY.revision, tree=R3_POLICY.tree)
        image["Config"] = {"Cmd": BACKEND_ARGV[:-2], "WorkingDir": "/app"}
        manifest = envelope(fixture)
        self.assertEqual(manifest["payload"]["stable_identity"]["image_config_hash"], digest(image["Config"]))
        probe = FixtureProbe(fixture, manifest, manifest["sha256"])
        self.assertEqual(probe.classify_legacy_blue()[0], LEGACY_BLUE_CLASS)

        image["Config"]["Labels"] = {}
        with self.assertRaisesRegex(GateError, "LEGACY_BLUE_UNKNOWN_OR_DRIFTED"):
            probe.classify_legacy_blue()

    def test_contradictory_labels_reject_legacy_and_native_images(self):
        for field in (REVISION_LABEL, TREE_LABEL):
            with self.subTest(field=field):
                legacy = legacy_fixture()
                native = api_slot()
                for fixture in (legacy, native):
                    fixture[1]["Config"]["Labels"][field] = fixture[0]["Config"]["Labels"][field] = "0" * 40
                with self.assertRaisesRegex(GateError, "WRONG_OCI_LABELS"):
                    envelope(legacy)
                with self.assertRaisesRegex(GateError, "WRONG_OCI_LABELS"):
                    self.native_identity(native)

    def test_explicit_nonempty_option_changes_still_change_the_pinned_config(self):
        for field, value in VALUES.items():
            if field == "Labels":
                continue
            with self.subTest(field=field):
                fixture = legacy_fixture()
                manifest = envelope(fixture)
                fixture[1]["Config"][field] = value
                probe = FixtureProbe(fixture, manifest, manifest["sha256"])
                with self.assertRaisesRegex(GateError, "LEGACY_BLUE_UNKNOWN_OR_DRIFTED"):
                    probe.classify_legacy_blue()
