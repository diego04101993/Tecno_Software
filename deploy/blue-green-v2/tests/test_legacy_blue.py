from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from gates import (
    BACKEND_ARGV, LEGACY_BLUE_CLASS, REVISION_LABEL, TREE_LABEL, DockerProbe, GateError, ReadyGate,
    legacy_blue_payload, live_identity, sign_receipt, verify_legacy_blue_manifest,
)
from release_state import Stop, digest
from routectl import RouteConfig, bind_legacy_blue
from test_gates import api_slot
from test_routectl import edge_config


COMPOSE = {"project": "tecnocontrol", "service": "backend"}
TARGET = "172.30.250.3:8000"
BOOT_ID = "6707ccab-7c1f-4d21-9135-fbcba66821f8"
POSTGRES_ID = "2" * 64


def legacy_fixture():
    item, image, network, peers, binding = api_slot()
    item["Name"] = "/tecnocontrol-backend-1"
    item.update(Path=BACKEND_ARGV[0], Args=BACKEND_ARGV[1:], Created="2026-11-12T17:59:00Z")
    item["Config"]["Cmd"] = list(BACKEND_ARGV)
    labels = item["Config"]["Labels"]
    for key in ("io.tecnocontrol.bgv2.slot", "io.tecnocontrol.bgv2.role", REVISION_LABEL, TREE_LABEL):
        del labels[key]
    labels.update({"com.docker.compose.project": "tecnocontrol", "com.docker.compose.service": "backend"})
    image["Config"]["Labels"].clear()
    item["NetworkSettings"]["Networks"][binding["network"]].update({
        "Aliases": ["backend", "tecnocontrol-backend-1"], "IPAddress": "172.30.250.3", "EndpointID": "f" * 64,
    })
    binding.update(name="tecnocontrol-backend-1", upstream=TARGET, ip="172.30.250.3", postgres_id=POSTGRES_ID)
    return item, image, network, peers, binding


def payload(fixture, *, argv=BACKEND_ARGV, ticks=90122, namespace="net:[42]", boot_id=BOOT_ID):
    return legacy_blue_payload(*fixture, argv, ticks, argv, namespace, COMPOSE, boot_id)


def envelope(fixture):
    data = payload(fixture)
    return {"sha256": digest(data), "payload": data}


class FixtureProbe(DockerProbe):
    def __init__(self, fixture, manifest=None, expected=None):
        self.fixture = fixture
        super().__init__({"api:BLUE": fixture[-1]}, edge_name="edge-qa",
                         legacy_blue=manifest, legacy_blue_sha256=expected)

    def _json(self, *args):
        if args[:2] != ("context", "inspect"):
            raise AssertionError("Unexpected Docker request")
        return [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]

    def _probe(self, role, slot, legacy_compose=None):
        if legacy_compose is not None:
            self.assert_scope(role, slot, legacy_compose)
            return payload(self.fixture)
        return live_identity(*self.fixture, role, slot, BACKEND_ARGV, 90122)

    def _postgres(self):
        return POSTGRES_ID

    @staticmethod
    def assert_scope(role, slot, legacy_compose):
        if (role, slot, legacy_compose) != ("api", "BLUE", COMPOSE):
            raise AssertionError("Invalid legacy scope")


class LegacyBlueTests(unittest.TestCase):
    def test_approved_legacy_backend_preserves_id_and_excludes_secrets(self):
        fixture = legacy_fixture()
        manifest = FixtureProbe(fixture).capture_legacy_blue(expected_project="tecnocontrol", expected_service="backend")
        probe = FixtureProbe(fixture, manifest, manifest["sha256"])
        label, identity = probe.classify_legacy_blue()
        self.assertEqual(label, LEGACY_BLUE_CLASS)
        self.assertEqual(identity, probe("api", "BLUE"))
        self.assertEqual(identity["container_id"], fixture[0]["Id"])
        self.assertEqual(identity["upstream"], TARGET)
        self.assertEqual(manifest["payload"]["stable_identity"]["health"], "healthy")
        self.assertNotIn("synthetic-fixture-value", json.dumps(manifest))

    def test_manifest_cannot_approve_itself_or_authorize_other_slots(self):
        fixture = legacy_fixture()
        manifest = envelope(fixture)
        with self.assertRaisesRegex(GateError, "APPROVAL_REQUIRED"):
            FixtureProbe(fixture, manifest)
        with self.assertRaisesRegex(GateError, "MANIFEST_CHECKSUM"):
            FixtureProbe(fixture, manifest, "1" * 64)
        probe = FixtureProbe(fixture, manifest, manifest["sha256"])
        for role, slot in (("api", "GREEN"), ("frontend", "BLUE")):
            with self.subTest(role=role, slot=slot), self.assertRaises(GateError):
                probe(role, slot)
        with self.assertRaisesRegex(GateError, "UNEXPECTED_UPSTREAM"):
            FixtureProbe(fixture)("api", "BLUE")
        manifest["payload"]["compose"]["project"] = "other"
        self.assertEqual(probe.classify_legacy_blue()[0], LEGACY_BLUE_CLASS)
        with self.assertRaisesRegex(GateError, "MANIFEST_CHECKSUM"):
            verify_legacy_blue_manifest(manifest, manifest["sha256"])

    def test_manifest_pins_process_config_network_mounts_and_project(self):
        original = legacy_fixture()
        manifest = envelope(original)
        edits = {
            "container_id": lambda item, binding: item.update(Id="1" * 64),
            "runtime_config": lambda item, binding: item["Config"]["Env"].append("CHANGED=1"),
            "restart_count": lambda item, binding: item.update(RestartCount=1),
            "started_at": lambda item, binding: item["State"].update(StartedAt="2026-11-12T18:02:00Z"),
            "pid": lambda item, binding: item["State"].update(Pid=643),
            "oom": lambda item, binding: item["State"].update(OOMKilled=True),
            "health": lambda item, binding: item["State"].update(Health={"Status": "unhealthy"}),
            "mount": lambda item, binding: item["Mounts"][0].update(RW=False),
            "command": lambda item, binding: item.update(Path="/bin/sh"),
            "project": lambda item, binding: item["Config"]["Labels"].update({"com.docker.compose.project": "other"}),
            "service": lambda item, binding: item["Config"]["Labels"].update({"com.docker.compose.service": "other"}),
            "endpoint": lambda item, binding: item["NetworkSettings"]["Networks"][binding["network"]].update(EndpointID="9" * 64),
            "alias": lambda item, binding: item["NetworkSettings"]["Networks"][binding["network"]]["Aliases"].append("fake"),
        }
        for name, edit in edits.items():
            expected = ("STOP_REATTEST_REQUIRED" if name in ("restart_count", "started_at", "pid", "endpoint")
                        else "LEGACY_BLUE_UNKNOWN_OR_DRIFTED")
            with self.subTest(name=name), self.assertRaisesRegex(GateError, expected):
                fixture = deepcopy(original)
                edit(fixture[0], fixture[-1])
                FixtureProbe(fixture, manifest, manifest["sha256"])("api", "BLUE")

    def test_legacy_fixture_cannot_skip_command_image_or_unique_ip_checks(self):
        fixture = legacy_fixture()
        with self.assertRaisesRegex(GateError, "UNSAFE_BACKEND_RUNTIME"):
            payload(fixture, argv=[*BACKEND_ARGV, "--reload"])
        fixture[3].append("1" * 64)
        with self.assertRaisesRegex(GateError, "SLOT_NETWORK_NOT_EXCLUSIVE"):
            payload(fixture)
        fixture = legacy_fixture()
        fixture[1]["Id"] = "sha256:" + "1" * 64
        with self.assertRaisesRegex(GateError, "WRONG_IMAGE_ID"):
            payload(fixture)

    def test_only_reviewed_operational_mac_changes_leave_stable_hash_equal(self):
        fixture = legacy_fixture()
        item, binding = fixture[0], fixture[-1]
        proxy = binding["network"]
        networks = item["NetworkSettings"]["Networks"]
        networks[proxy]["MacAddress"] = "02:00:00:00:00:01"
        networks["application-net"] = {**networks[proxy], "MacAddress": "02:00:00:00:00:02"}
        binding["generated_mac_networks"] = [proxy]
        manifest = envelope(fixture)
        original = manifest["payload"]
        networks[proxy]["MacAddress"] = "02:00:00:00:00:03"
        changed = payload(fixture)
        self.assertEqual(changed["stable_identity"], original["stable_identity"])
        self.assertNotEqual(changed["runtime_epoch"]["network_hash"], original["runtime_epoch"]["network_hash"])
        with self.assertRaisesRegex(GateError, "STOP_REATTEST_REQUIRED"):
            FixtureProbe(fixture, manifest, manifest["sha256"]).classify_legacy_blue()
        networks["application-net"]["MacAddress"] = "02:00:00:00:00:04"
        with self.assertRaisesRegex(GateError, "LEGACY_BLUE_UNKNOWN_OR_DRIFTED"):
            FixtureProbe(fixture, manifest, manifest["sha256"]).classify_legacy_blue()
        binding["generated_mac_networks"] = []
        with self.assertRaisesRegex(GateError, "LEGACY_BLUE_BINDING_MISMATCH"):
            FixtureProbe(fixture, manifest, manifest["sha256"])

    def test_generated_mac_allowlist_rejects_malformed_unattached_or_explicit(self):
        fixture = legacy_fixture()
        item, binding = fixture[0], fixture[-1]
        proxy = binding["network"]
        endpoint = item["NetworkSettings"]["Networks"][proxy]
        endpoint["MacAddress"] = "02:00:00:00:00:01"
        for value, error in (([proxy, proxy], "INVALID_GENERATED_MAC_NETWORKS"),
                             ("*", "INVALID_GENERATED_MAC_NETWORKS"),
                             (["not-attached"], "GENERATED_MAC_NETWORK_NOT_PRESENT")):
            binding["generated_mac_networks"] = value
            with self.subTest(value=value), self.assertRaisesRegex(GateError, error):
                payload(fixture)
        binding["generated_mac_networks"] = [proxy]
        item["Config"]["MacAddress"] = endpoint["MacAddress"]
        item["HostConfig"]["NetworkMode"] = proxy
        with self.assertRaisesRegex(GateError, "GENERATED_MAC_NETWORK_HAS_CONFIGURED_MAC"):
            payload(fixture)
        del item["Config"]["MacAddress"]
        del endpoint["MacAddress"]
        with self.assertRaisesRegex(GateError, "GENERATED_MAC_EVIDENCE_INVALID"):
            payload(fixture)

    def test_endpoint_only_changes_blue_api_initial_and_rolls_back_exact(self):
        config = edge_config()
        manifest = envelope(legacy_fixture())
        amended = bind_legacy_blue(config, manifest, manifest["sha256"])
        route = RouteConfig(amended)
        self.assertEqual(route.target_for("api", "BLUE"), TARGET)
        self.assertEqual(route.target_for("api", "GREEN"), "backend-green:8000")
        self.assertEqual(route.target_for("frontend", "BLUE"), "frontend-blue:80")
        self.assertEqual(RouteConfig(config).target_for("api", "BLUE"), "backend-blue:8000")
        changed = route.changed({"api": "GREEN", "frontend": "BLUE"}, 1)
        restored = changed.changed({"api": "BLUE", "frontend": "BLUE"}, 2)
        self.assertEqual(restored._dial(restored.proxies["api"]), TARGET)
        with self.assertRaisesRegex(Stop, "INITIAL_GENERATION"):
            bind_legacy_blue(changed.config, manifest, manifest["sha256"])
        amended["apps"]["http"]["servers"]["preview-blue"]["routes"][0]["handle"][0]["routes"][0]["handle"][1]["upstreams"][0]["dial"] = "8.8.8.8:8000"
        with self.assertRaisesRegex(GateError, "LEGACY_BLUE_ENDPOINT_INVALID"):
            RouteConfig(amended)

    def test_phase3_infra_evidence_never_qualifies_phase4_dual_slot(self):
        manifest = envelope(legacy_fixture())
        legacy_identity = manifest["payload"]["stable_identity"]
        key = b"a" * 32
        context = {"role": "api", "slot": "BLUE", "adoption": "LEGACY_BLUE",
                   "started_at": manifest["payload"]["runtime_epoch"]["started_at"], "identity_hash": digest(legacy_identity),
                   "release": {"images": {"api": legacy_identity["image_id"]}}}
        checks = {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                  "runtime_serial_ok": 36, "runtime_concurrent_ok": 16, "runtime_clients": 4,
                  "runtime_transport_timeout_s": 10, "channels_elapsed_ns": 42, "pool": "BALANCED",
                  "phase3_infra_capacity": True, "schema_unchanged": True, "media_unchanged": True,
                  "contracts": {"windows_legacy": True, "android_normal": True}}
        now = datetime(2026, 11, 12, 18, 2, tzinfo=timezone.utc)
        extra = {"healthy_at": now.isoformat(), "ready_at": now.isoformat(), "private_route": True, "checks": checks}
        gate = ReadyGate(key, now=lambda: now)
        self.assertTrue(gate(sign_receipt("READY", {**context, **extra}, key), context))
        native = {key: value for key, value in context.items() if key != "adoption"}
        self.assertFalse(gate(sign_receipt("READY", {**native, **extra}, key), native))
        green = {**context, "slot": "GREEN"}
        self.assertFalse(gate(sign_receipt("READY", {**green, **extra}, key), green))
        checks["phase3_infra_capacity"] = False
        self.assertFalse(gate(sign_receipt("READY", {**context, **extra}, key), context))


if __name__ == "__main__":
    unittest.main()
