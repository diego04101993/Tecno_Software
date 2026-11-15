from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

from docker_mounts import canonicalize_mounts, encode_mount_evidence, mount_evidence
from gates import BACKEND_ARGV, LEGACY_BLUE_CLASS, GateError, ReadyGate, live_identity, sign_receipt, stable_slot_identity
from legacy_frontend_blue import LEGACY_FRONTEND_IMAGE_EXACT, R3_POLICY
from release_state import Stop, digest
from test_gates import KEY, NOW, api_slot
from test_legacy_blue import FixtureProbe, envelope, legacy_fixture, payload
from test_legacy_frontend_gate import DockerFrontend


OBSERVED_CANONICAL_SHA256 = "12192366a334ec55abbcc04f577ac6ec0223c9f73ca42b1208277c63507e13e0"
OBSERVED_REVERSED_SHA256 = "0e17ef01b3daff9e0cb1d276850fa4037cd7c16f7313c995d29cc70f01f71978"


def observed_r3_mounts():
    return [
        {
            "Destination": f"/{target}",
            "Driver": "local",
            "Mode": "rw",
            "Name": f"tecnocontrol_caddy_{target}",
            "Propagation": "",
            "RW": True,
            "Source": f"/var/lib/docker/volumes/tecnocontrol_caddy_{target}/_data",
            "Type": "volume",
        }
        for target in ("config", "data")
    ]


def native_fixture():
    fixture = api_slot()
    item, _, _, _, binding = fixture
    item.update(Path=BACKEND_ARGV[0], Args=BACKEND_ARGV[1:], Created="2026-11-12T17:59:00Z")
    item["NetworkSettings"]["Networks"][binding["network"]]["EndpointID"] = "f" * 64
    item["Mounts"].append({"Type": "bind", "Source": "/mnt/fixture", "Destination": "/mnt/data", "RW": False})
    return fixture


def native_identity(fixture):
    item, image, _, _, binding = fixture
    live = live_identity(*fixture, "api", "GREEN", BACKEND_ARGV, 90122)
    return live, stable_slot_identity(item, image, live, BACKEND_ARGV, binding)


class MountCanonicalizationTests(TestCase):
    def assert_field_drift(self, key, value):
        original = observed_r3_mounts()
        changed = deepcopy(original)
        changed[0][key] = value
        self.assertNotEqual(mount_evidence(original)["canonical_mounts_sha256"],
                            mount_evidence(changed)["canonical_mounts_sha256"])

    def test_same_order_has_expected_mounts_and_identity(self):
        original = observed_r3_mounts()
        projected = canonicalize_mounts(original)
        self.assertEqual(projected, original)
        self.assertEqual(digest(projected), OBSERVED_CANONICAL_SHA256)

    def test_reversed_order_preserves_raw_evidence_and_canonical_identity(self):
        original = observed_r3_mounts()
        reversed_mounts = original[::-1]
        evidence = mount_evidence(reversed_mounts)
        self.assertEqual(digest(reversed_mounts), OBSERVED_REVERSED_SHA256)
        self.assertEqual(evidence["raw_mounts"], reversed_mounts)
        self.assertEqual(evidence["canonical_mounts"], original)
        self.assertEqual(evidence["canonical_mounts_sha256"], OBSERVED_CANONICAL_SHA256)
        self.assertEqual([row["Destination"] for row in reversed_mounts], ["/data", "/config"])
        self.assertIsNot(evidence["canonical_mounts"][0], reversed_mounts[1])
        self.assertIsNot(evidence["raw_mounts"][0], reversed_mounts[0])
        persisted = json.loads(encode_mount_evidence(evidence))
        self.assertEqual([list(row) for row in persisted["raw_mounts"]],
                         [list(row) for row in reversed_mounts])

    def test_observed_order_alternates_one_hundred_times(self):
        original = observed_r3_mounts()
        for index in range(100):
            with self.subTest(index=index):
                rows = original if index % 2 == 0 else original[::-1]
                self.assertEqual(mount_evidence(rows)["canonical_mounts_sha256"], OBSERVED_CANONICAL_SHA256)

    def test_source_drift(self):
        self.assert_field_drift("Source", "/var/lib/docker/volumes/different/_data")

    def test_destination_drift(self):
        self.assert_field_drift("Destination", "/CONFIG")

    def test_type_drift(self):
        self.assert_field_drift("Type", "bind")

    def test_rw_drift(self):
        self.assert_field_drift("RW", False)

    def test_mode_drift(self):
        self.assert_field_drift("Mode", "ro")

    def test_propagation_drift(self):
        self.assert_field_drift("Propagation", "rprivate")

    def test_driver_drift(self):
        self.assert_field_drift("Driver", "another-driver")

    def test_name_drift(self):
        self.assert_field_drift("Name", "another-volume")

    def test_extra_mount_is_drift(self):
        original = observed_r3_mounts()
        extra = {**original[0], "Destination": "/extra"}
        self.assertNotEqual(digest(canonicalize_mounts([*original, extra])), OBSERVED_CANONICAL_SHA256)

    def test_missing_mount_is_drift(self):
        self.assertNotEqual(digest(canonicalize_mounts(observed_r3_mounts()[:1])), OBSERVED_CANONICAL_SHA256)

    def test_duplicate_destination_fails_closed_even_when_rows_are_identical(self):
        original = observed_r3_mounts()
        for extra in (deepcopy(original[0]), {**original[0], "Name": "different-volume"}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(Stop, "^DUPLICATE_MOUNT_DESTINATION$"):
                    canonicalize_mounts([*original, extra])

    def test_json_key_order_is_canonical_at_every_depth(self):
        original = observed_r3_mounts()
        original[0]["Additional"] = {"z": {"b": 2, "a": 1}, "a": ["z", "a"]}
        reordered = [dict(reversed(list(row.items()))) for row in original]
        reordered[0]["Additional"] = {"a": ["z", "a"], "z": {"a": 1, "b": 2}}
        self.assertEqual(json.dumps(canonicalize_mounts(reordered)), json.dumps(canonicalize_mounts(original)))

    def test_unknown_identical_field_is_preserved(self):
        original = observed_r3_mounts()
        original[0]["Additional"] = {"policy": ["z", "a"], "expiry": None}
        proof = mount_evidence(original[::-1])
        self.assertEqual(proof["canonical_mounts"][0]["Additional"], original[0]["Additional"])
        self.assertEqual(proof["canonical_mounts_sha256"], digest(canonicalize_mounts(original)))
        self.assertNotEqual(proof["canonical_mounts_sha256"], OBSERVED_CANONICAL_SHA256)
        proof["canonical_mounts"][0]["Additional"]["policy"].append("changed")
        self.assertEqual(proof["raw_mounts"][1]["Additional"]["policy"], ["z", "a"])
        self.assertEqual(original[0]["Additional"]["policy"], ["z", "a"])

    def test_unknown_changed_field_is_drift_including_json_types_and_array_order(self):
        original = observed_r3_mounts()
        original[0]["Additional"] = {"flag": True, "policy": ["z", "a"]}
        baseline = digest(canonicalize_mounts(original))
        for value in ({"flag": False, "policy": ["z", "a"]},
                      {"flag": 1, "policy": ["z", "a"]},
                      {"flag": True, "policy": ["a", "z"]}):
            with self.subTest(value=value):
                changed = deepcopy(original)
                changed[0]["Additional"] = value
                self.assertNotEqual(digest(canonicalize_mounts(changed)), baseline)

    def test_missing_field_is_distinct_from_empty_and_null(self):
        original = observed_r3_mounts()
        missing = deepcopy(original)
        del missing[0]["Propagation"]
        null = deepcopy(original)
        null[0]["Propagation"] = None
        self.assertEqual(len({digest(canonicalize_mounts(rows)) for rows in (original, missing, null)}), 3)


class MountIdentityTests(TestCase):
    def test_native_v2_stable_identity_ignores_only_outer_order(self):
        fixture = native_fixture()
        first_live, first_stable = native_identity(fixture)
        fixture[0]["Mounts"].reverse()
        self.assertEqual(native_identity(fixture), (first_live, first_stable))
        fixture[0]["Mounts"][0]["Additional"] = True
        bool_live, bool_stable = native_identity(fixture)
        self.assertNotEqual(bool_live["mounts_hash"], first_live["mounts_hash"])
        self.assertNotEqual(bool_stable, first_stable)
        fixture[0]["Mounts"][0]["Additional"] = 1
        self.assertNotEqual(native_identity(fixture), (bool_live, bool_stable))

    def test_native_v2_ready_is_valid_after_reordering_and_invalid_on_drift(self):
        fixture = native_fixture()
        identity, _ = native_identity(fixture)
        release = {"revision": identity["revision"], "tree": identity["tree"],
                   "images": {"api": identity["image_id"], "frontend": identity["image_id"]}}
        context = {"role": "api", "slot": "GREEN", "started_at": identity["started_at"],
                   "release": release, "identity_hash": digest(identity)}
        checks = {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                  "channels_elapsed_ns": 1, "runtime_serial_ok": 36, "runtime_concurrent_ok": 16,
                  "runtime_clients": 4, "runtime_transport_timeout_s": 10, "pool": "BALANCED",
                  "dual_slot_schema": True, "media_cross_slot": True, "dual_slot_capacity": True,
                  "contracts": {"windows_legacy": True, "android_normal": True, "android_node_slice": True}}
        receipt = sign_receipt("READY", {**context, "healthy_at": "2026-11-12T18:00:05Z",
                                       "ready_at": "2026-11-12T18:00:40Z", "private_route": True,
                                       "checks": checks}, KEY)
        gate = ReadyGate(KEY, NOW)
        self.assertTrue(gate(receipt, context))
        fixture[0]["Mounts"].reverse()
        self.assertTrue(gate(receipt, {**context, "identity_hash": digest(native_identity(fixture)[0])}))
        fixture[0]["Mounts"][0]["Mode"] = "ro"
        self.assertFalse(gate(receipt, {**context, "identity_hash": digest(native_identity(fixture)[0])}))

    def test_r3_backend_manifest_classification_ignores_order_and_rejects_real_drift(self):
        fixture = legacy_fixture()
        item, image, _, _, binding = fixture
        item["Image"] = image["Id"] = binding["image_id"] = R3_POLICY.backend_image_id
        binding.update(revision=R3_POLICY.revision, tree=R3_POLICY.tree)
        item["Mounts"].append({"Type": "bind", "Source": "/mnt/fixture", "Destination": "/mnt/data", "RW": False})
        manifest = envelope(fixture)
        approved = FixtureProbe(fixture, manifest, manifest["sha256"]).classify_legacy_blue()
        self.assertEqual(approved[0], LEGACY_BLUE_CLASS)
        item["Mounts"].reverse()
        self.assertEqual(payload(fixture)["stable_identity"], manifest["payload"]["stable_identity"])
        self.assertEqual(FixtureProbe(fixture, manifest, manifest["sha256"]).classify_legacy_blue(), approved)
        item["Mounts"][0]["Source"] = "/mnt/other"
        with self.assertRaisesRegex(GateError, "^LEGACY_BLUE_UNKNOWN_OR_DRIFTED$"):
            FixtureProbe(fixture, manifest, manifest["sha256"]).classify_legacy_blue()

    def test_native_and_legacy_identity_reject_duplicate_destination(self):
        native = native_fixture()
        legacy = legacy_fixture()
        for fixture, probe in ((native, native_identity), (legacy, payload)):
            with self.subTest(name=fixture[0]["Name"]):
                fixture[0]["Mounts"].append(deepcopy(fixture[0]["Mounts"][0]))
                with self.assertRaisesRegex(Stop, "^DUPLICATE_MOUNT_DESTINATION$"):
                    probe(fixture)


class MountInspectionTests(TestCase):
    def test_historical_probe_accepts_reversed_order_and_keeps_both_raw_samples(self):
        probe = DockerFrontend()
        before = deepcopy(probe.historical)
        before["Mounts"] = observed_r3_mounts()
        after = deepcopy(before)
        after["Mounts"].reverse()
        probe.mount_observations = []
        with (patch.object(probe, "_json", side_effect=[[before], [probe.image], [after]]),
              patch("gates.classify_historical", return_value=LEGACY_FRONTEND_IMAGE_EXACT)):
            probe.verify_initial_legacy_frontend_source()
        self.assertEqual(probe.mount_observations, [
            {"inspection": "historical_frontend_before", "container_id": before["Id"],
             **mount_evidence(before["Mounts"])},
            {"inspection": "historical_frontend_after", "container_id": after["Id"],
             **mount_evidence(after["Mounts"])},
        ])

    def test_historical_same_process_probe_rejects_unknown_mount_value_drift(self):
        probe = DockerFrontend()
        before = deepcopy(probe.historical)
        before["Mounts"] = observed_r3_mounts()
        before["Mounts"][0]["Additional"] = True
        after = deepcopy(before)
        after["Mounts"][0]["Additional"] = 1
        after["Mounts"].reverse()
        with (patch.object(probe, "_json", side_effect=[[before], [probe.image], [after]]),
              patch("gates.classify_historical", return_value=LEGACY_FRONTEND_IMAGE_EXACT),
              self.assertRaisesRegex(GateError, "^LEGACY_FRONTEND_HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED$")):
            probe.verify_initial_legacy_frontend_source()

    def test_private_same_process_probe_preserves_evidence_and_rejects_mount_drift(self):
        probe = DockerFrontend()
        before = deepcopy(probe.item)
        before["Mounts"].append({"Type": "tmpfs", "Destination": "/config", "RW": False, "Additional": True})
        after = deepcopy(before)
        after["Mounts"].reverse()
        inspect = probe._json

        def check():
            probe.mount_observations = []
            samples = iter([before, after])

            def inspect_samples(*args):
                if args == ("container", "inspect", probe.binding["name"]):
                    return [next(samples)]
                return inspect(*args)

            with (patch.object(probe, "_json", side_effect=inspect_samples),
                  patch("gates.verify_private_frontend", return_value=probe.proof())):
                return probe._probe("frontend", "BLUE", with_epoch=True, legacy_frontend=True)

        result = check()
        self.assertEqual(result["identity"]["mounts_hash"], digest(canonicalize_mounts(before["Mounts"])))
        self.assertEqual([row["raw_mounts"] for row in probe.mount_observations], [before["Mounts"], after["Mounts"]])
        after["Mounts"][0]["Additional"] = 1
        with self.assertRaisesRegex(GateError, "^LEGACY_FRONTEND_CHANGED_DURING_INSPECTION$"):
            check()
