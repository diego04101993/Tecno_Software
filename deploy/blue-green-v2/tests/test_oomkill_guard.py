"""The effective OOM guard must not rewrite historical runtime identities."""

from copy import deepcopy
import unittest

from gates import (
    BACKEND_ARGV, GateError, comparison_host_config,
    effective_comparison_host_config, live_identity,
)
from test_gates import api_slot


MISSING = object()


def host_config(oom=None):
    """Sanitized Engine HostConfig shape; no external Docker fixture required."""
    result = {
        "AutoRemove": False, "Binds": None,
        "BlkioDeviceReadBps": [], "BlkioDeviceReadIOps": [],
        "BlkioDeviceWriteBps": [], "BlkioDeviceWriteIOps": [],
        "BlkioWeight": 0, "BlkioWeightDevice": [], "CapAdd": None, "CapDrop": None,
        "Cgroup": "", "CgroupParent": "", "CgroupnsMode": "private",
        "ConsoleSize": [0, 0], "ContainerIDFile": "", "CpuCount": 0,
        "CpuPercent": 0, "CpuPeriod": 0, "CpuQuota": 0,
        "CpuRealtimePeriod": 0, "CpuRealtimeRuntime": 0, "CpuShares": 0,
        "CpusetCpus": "", "CpusetMems": "", "DeviceCgroupRules": None,
        "DeviceRequests": None, "Devices": [], "Dns": None,
        "DnsOptions": [], "DnsSearch": [], "ExtraHosts": None, "GroupAdd": None,
        "IOMaximumBandwidth": 0, "IOMaximumIOps": 0, "IpcMode": "private",
        "Isolation": "", "Links": None,
        "LogConfig": {"Config": {}, "Type": "json-file"},
        "MaskedPaths": ["/proc/acpi", "/proc/kcore", "/sys/firmware"],
        "Memory": 0, "MemoryReservation": 0, "MemorySwap": 0,
        "MemorySwappiness": None,
        "Mounts": [{"Source": "fixture-media", "Target": "/app/storage/uploads", "Type": "volume"}],
        "NanoCpus": 0, "NetworkMode": "fixture-private-proxy", "OomScoreAdj": 0,
        "PidMode": "", "PidsLimit": None, "PortBindings": {},
        "Privileged": False, "PublishAllPorts": False,
        "ReadonlyPaths": ["/proc/bus", "/proc/sys"], "ReadonlyRootfs": False,
        "RestartPolicy": {"MaximumRetryCount": 0, "Name": "unless-stopped"},
        "Runtime": "runc", "SecurityOpt": None, "ShmSize": 67108864,
        "UTSMode": "", "Ulimits": [], "UsernsMode": "", "VolumeDriver": "",
        "VolumesFrom": None,
    }
    if oom is not MISSING:
        result["OomKillDisable"] = oom
    return result


class OomKillGuardTests(unittest.TestCase):
    def assert_effective_equal(self, expected, actual):
        self.assertEqual(effective_comparison_host_config(expected),
                         effective_comparison_host_config(actual))

    def assert_effective_different(self, expected, actual):
        self.assertNotEqual(effective_comparison_host_config(expected),
                            effective_comparison_host_config(actual))

    def test_expected_null_actual_false_passes(self):
        self.assert_effective_equal(host_config(None), host_config(False))

    def test_expected_missing_actual_false_passes(self):
        self.assert_effective_equal(host_config(MISSING), host_config(False))

    def test_expected_false_actual_false_passes(self):
        self.assert_effective_equal(host_config(False), host_config(False))

    def test_expected_null_actual_true_blocks(self):
        self.assert_effective_different(host_config(None), host_config(True))

    def test_expected_false_actual_true_blocks(self):
        self.assert_effective_different(host_config(False), host_config(True))

    def test_expected_true_actual_false_blocks(self):
        self.assert_effective_different(host_config(True), host_config(False))

    def test_unset_equivalence_is_symmetric_and_true_stays_true(self):
        for left in (MISSING, None, False):
            for right in (MISSING, None, False):
                with self.subTest(left=left, right=right):
                    self.assert_effective_equal(host_config(left), host_config(right))
        self.assert_effective_equal(host_config(True), host_config(True))
        self.assertIs(effective_comparison_host_config(host_config(True))["OomKillDisable"], True)
        for value in (MISSING, None, False):
            self.assertIs(effective_comparison_host_config(host_config(value))["OomKillDisable"], False)

    def test_invalid_oom_types_fail_closed_instead_of_boolean_coercion(self):
        for value in (0, 1, -1, 0.0, "", "false", "true", [], {}, [False]):
            with self.subTest(value=value), self.assertRaises(GateError):
                effective_comparison_host_config(host_config(value))

    def test_input_and_raw_evidence_are_not_mutated(self):
        for value in (MISSING, None, False, True):
            with self.subTest(value=value):
                raw = host_config(value)
                original = deepcopy(raw)
                result = effective_comparison_host_config(raw)
                self.assertEqual(raw, original)
                self.assertIsNot(result, raw)
                if value is MISSING:
                    self.assertNotIn("OomKillDisable", raw)
                else:
                    self.assertIs(raw["OomKillDisable"], value)

    def test_only_oom_gets_new_null_missing_false_equivalence(self):
        for field in host_config():
            if field in ("OomKillDisable", "Dns", "DnsOptions", "DnsSearch"):
                continue
            for left, right in ((None, False), (MISSING, False), (MISSING, None)):
                with self.subTest(field=field, left=left, right=right):
                    expected, actual = host_config(), host_config()
                    if left is MISSING:
                        expected.pop(field)
                    else:
                        expected[field] = left
                    actual[field] = right
                    self.assert_effective_different(expected, actual)

    def test_each_other_hostconfig_field_change_remains_visible(self):
        original = host_config()
        for field, value in original.items():
            if field in ("OomKillDisable", "Dns", "DnsOptions", "DnsSearch"):
                continue
            changed = deepcopy(original)
            if value is None:
                changed[field] = False
            elif type(value) is bool:
                changed[field] = not value
            elif type(value) is int:
                changed[field] = value + 1
            elif type(value) is str:
                changed[field] = value + "-different"
            elif type(value) is list:
                changed[field].append("different")
            else:
                changed[field]["different"] = True
            with self.subTest(field=field):
                self.assert_effective_different(original, changed)
            missing = deepcopy(original)
            del missing[field]
            with self.subTest(missing=field):
                self.assert_effective_different(original, missing)
        unknown = deepcopy(original)
        unknown["FutureHostConfigField"] = False
        self.assert_effective_different(original, unknown)

    def test_dns_approved_null_empty_rule_and_missing_key_guard_are_preserved(self):
        for field in ("Dns", "DnsOptions", "DnsSearch"):
            for left, right in ((None, []), ([], None), (None, None), ([], [])):
                with self.subTest(field=field, left=left, right=right):
                    expected, actual = host_config(), host_config(False)
                    expected[field], actual[field] = left, right
                    self.assert_effective_equal(expected, actual)
            missing = host_config()
            del missing[field]
            self.assert_effective_different(missing, host_config(False))
            for value in (False, 0, "", {}, [1]):
                invalid = host_config()
                invalid[field] = value
                with self.subTest(field=field, invalid=value), self.assertRaises(GateError):
                    effective_comparison_host_config(invalid)

    def test_real_dns_values_and_order_remain_exact(self):
        for field in ("Dns", "DnsOptions", "DnsSearch"):
            original, changed = host_config(), host_config(False)
            original[field] = ["first", "second"]
            changed[field] = ["first", "second"]
            self.assert_effective_equal(original, changed)
            for value in (None, [], ["first"], ["first", "other"], ["second", "first"]):
                with self.subTest(field=field, value=value):
                    changed[field] = value
                    self.assert_effective_different(original, changed)

    def test_persisted_identity_projection_keeps_its_original_oom_representations(self):
        self.assertNotIn("OomKillDisable", comparison_host_config(host_config(MISSING)))
        self.assertIsNone(comparison_host_config(host_config(None))["OomKillDisable"])
        self.assertIs(comparison_host_config(host_config(False))["OomKillDisable"], False)
        self.assertNotEqual(comparison_host_config(host_config(None)),
                            comparison_host_config(host_config(False)))

    def test_existing_runtime_hashes_are_not_migrated_by_the_comparison_fix(self):
        hashes = {
            "missing": "e7a82d4b82b23e041c00b7c4bbf3776a93895eba24d20c396b8ea63ecb689d9f",
            "null": "0796fdc9b07612dcc301aa1cc03ed42c6f8fa41ebd39026e7f0c79aab6493f34",
            "false": "aff50c7fa3c3830eaf92711daab65185b714832a1e427864676d1e76a345c4be",
            "true": "21c26e9c7b0f86b95d440f43eadd9580fbab98194e75aa8e51607ef1deff20e8",
        }
        for label, value in (("missing", MISSING), ("null", None), ("false", False), ("true", True)):
            with self.subTest(value=label):
                fixture = api_slot()
                if value is not MISSING:
                    fixture[0]["HostConfig"]["OomKillDisable"] = value
                actual = live_identity(*fixture, "api", "GREEN", BACKEND_ARGV, 90122)
                self.assertEqual(actual["runtime_hash"], hashes[label])


if __name__ == "__main__":
    unittest.main()
