"""Stable legacy frontend identity permits only approved endpoint representation changes."""

from copy import deepcopy
from functools import partial
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gates import DockerProbe, GateError
from legacy_frontend_blue import (
    LEGACY_FRONTEND_IMAGE_EXACT, attest_historical_stable, classify_historical, sha,
    verify_historical_stable_projection, verify_manifest,
)
from phase3_ready import legacy_frontend_ready_pin
from qualify_r3 import R3_CADDY_ARGV, R3_IMAGES, qualify_role
from release_state import Stop, canonical
import test_historical_reconnect


class LegacyFrontendStableTests(unittest.TestCase):
    def setUp(self):
        fixture = test_historical_reconnect.HistoricalReconnectTests()
        fixture.setUp()
        self.fixture = fixture
        self.manifest = fixture.manifest
        self.pin = fixture.pin
        self.policy = fixture.policy
        self.compose = fixture.compose
        self.srv_tree_sha256 = self.policy.historical_srv_tree_sha256
        self.before = {key: deepcopy(self.manifest["payload"][key]) for key in ("historical", "image", "content")}
        self.after = deepcopy(self.before)
        self.after["historical"]["networks"] = deepcopy(fixture.observed["NetworkSettings"]["Networks"])

    def verify(self, projection, tree_sha256=None):
        return verify_historical_stable_projection(
            self.manifest, self.pin, projection, compose_proof=self.compose,
            observed_srv_tree_sha256=self.srv_tree_sha256 if tree_sha256 is None else tree_sha256,
            policy=self.policy,
        )

    def test_semantically_same_reconnect_keeps_identity_and_preserves_raw_evidence(self):
        state_before, state_after = deepcopy(self.before), deepcopy(self.after)
        before, after = self.verify(self.before), self.verify(self.after)
        self.assertEqual(before["classification"], LEGACY_FRONTEND_IMAGE_EXACT)
        self.assertEqual(after["classification"], LEGACY_FRONTEND_IMAGE_EXACT)
        self.assertEqual(before["legacy_frontend_stable_pin"], after["legacy_frontend_stable_pin"])
        self.assertNotEqual(before["legacy_frontend_evidence_sha256"], after["legacy_frontend_evidence_sha256"])
        self.assertEqual(after["volatile_endpoint_evidence"]["proxy"]["observed"]["EndpointID"],
                         self.after["historical"]["networks"]["proxy"]["EndpointID"])
        self.assertNotIn("EndpointID", after["stable_projection"]["historical"]["networks"]["proxy"]["endpoint"])
        self.assertEqual(self.before, state_before)
        self.assertEqual(self.after, state_after)

    def test_live_classifier_requires_exact_stable_pin_and_compose_proof(self):
        fixture = self.fixture
        stable_pin = self.verify(self.before)["legacy_frontend_stable_pin"]

        def classify(container, **approval):
            return classify_historical(container, fixture.image, fixture.source.read, self.manifest, self.pin,
                                       policy=self.policy, read_host=fixture.source.read_host, **approval)

        with patch("legacy_frontend_inventory.measure_srv_tree", return_value=self.srv_tree_sha256):
            self.assertEqual(classify(fixture.historical, stable_pin=stable_pin, compose_proof=self.compose),
                             LEGACY_FRONTEND_IMAGE_EXACT)
            self.assertEqual(classify(fixture.observed, stable_pin=stable_pin, compose_proof=self.compose),
                             LEGACY_FRONTEND_IMAGE_EXACT)
            for approval in ({}, {"stable_pin": stable_pin},
                             {"stable_pin": "0" * 64, "compose_proof": self.compose},
                             {"stable_pin": stable_pin, "compose_proof": {**self.compose, "container_create_hash": "0" * 64}}):
                with self.subTest(approval=approval), self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                    classify(fixture.observed, **approval)

    def test_docker_probe_binds_approval_and_checks_current_frontend(self):
        fixture = self.fixture
        release = self.policy.release
        stable_pin = self.verify(self.before)["legacy_frontend_stable_pin"]
        identity = {**release, "image_id": release["images"]["api"], "upstream": "backend-blue:8000",
                    "generated_mac_networks": []}
        backend = {"name": "backend-blue", "postgres_id": "c" * 64, "stable_identity": identity}
        approved = {"manifest": self.manifest, "sha256": self.pin, "stable_pin": stable_pin,
                    "compose_proof": self.compose}
        bindings = {"api:BLUE": {**backend, "upstream": identity["upstream"]},
                    "frontend:BLUE": {**release, "image_id": release["images"]["frontend"],
                                     "legacy_frontend_adoption": approved}}

        def inspect(*args):
            if args == ("context", "inspect", "default"):
                return [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
            if args == ("container", "inspect", fixture.observed["Id"]):
                return [fixture.observed]
            self.assertEqual(args, ("image", "inspect", fixture.image["Id"]))
            return [fixture.image]

        def create():
            return DockerProbe(bindings, edge_name=self.policy.historical_name, legacy_blue={},
                               legacy_blue_sha256="c" * 64, legacy_frontend_policy=self.policy)

        with (patch("gates.verify_legacy_blue_manifest", return_value=backend),
              patch.object(DockerProbe, "_json", side_effect=inspect),
              patch.object(DockerProbe, "_run", side_effect=fixture.source.read),
              patch("legacy_frontend_inventory.measure_srv_tree", return_value=self.srv_tree_sha256) as measure,
              patch("gates.classify_historical", side_effect=partial(classify_historical, read_host=fixture.source.read_host))):
            probe = create()
            self.assertEqual(probe.legacy_frontend_blue_sha256, stable_pin)
            self.assertEqual(probe.legacy_frontend_source_sha256, self.pin)
            with patch.dict(approved, {"stable_pin": self.pin}), self.assertRaisesRegex(GateError, "STABLE_PIN_MISMATCH"):
                create()

            probe.verify_initial_legacy_frontend_source()
            measure.assert_called_once_with(fixture.observed, probe._run)
            probe.legacy_frontend_blue_sha256 = self.pin
            with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
                probe.verify_initial_legacy_frontend_source()

    def test_qualification_emits_stable_and_raw_pins_and_rejects_raw_approval(self):
        fixture = self.fixture
        container = deepcopy(fixture.observed)
        container["Name"] = "/tecnocontrol-frontend-1"
        container["Config"]["Labels"]["com.docker.compose.project"] = "tecnocontrol"
        caddyfile = fixture.source.caddyfile
        proof = self.verify(self.after)

        def read(*args):
            self.assertEqual(args, ("container", "inspect", container["Id"]))
            return canonical([container])

        def qualify(pin):
            return qualify_role("frontend", self.policy.release, container["Id"], container, container,
                                fixture.image, R3_CADDY_ARGV, caddyfile, read=read,
                                legacy_frontend_manifest=self.manifest, legacy_frontend_sha256=self.pin,
                                legacy_frontend_stable_pin=pin, legacy_frontend_compose_proof=self.compose)

        with (patch("qualify_r3.R3_CADDYFILE_SHA256", sha(caddyfile)),
              patch("qualify_r3.verify_manifest", side_effect=partial(verify_manifest, policy=self.policy)),
              patch("qualify_r3.historical_evidence", return_value=self.after),
              patch("qualify_r3.attest_historical_stable", side_effect=partial(attest_historical_stable, policy=self.policy)),
              patch("qualify_r3.require_private_network_available"),
              patch("legacy_frontend_inventory.measure_srv_tree", return_value=self.srv_tree_sha256)):
            result = qualify(proof["legacy_frontend_stable_pin"])
            self.assertEqual(result["frontend_classifier"], LEGACY_FRONTEND_IMAGE_EXACT)
            self.assertEqual(result["legacy_frontend_manifest_sha256"], proof["legacy_frontend_stable_pin"])
            self.assertEqual(result["legacy_frontend_evidence_sha256"], proof["legacy_frontend_evidence_sha256"])
            with self.assertRaisesRegex(Stop, "STABLE_PIN_MISMATCH"):
                qualify(self.pin)

    def test_readiness_requires_bound_stable_pin_and_raw_evidence(self):
        proof = self.verify(self.after)
        stable_pin = proof["legacy_frontend_stable_pin"]
        frontend = {"frontend_classifier": LEGACY_FRONTEND_IMAGE_EXACT, "provenance": "PASS_LEGACY_MANIFEST",
                    "legacy_frontend_manifest_sha256": stable_pin, "frontend_blue_feasibility": "PASS",
                    "frontend_blue_create_image_id": R3_IMAGES["frontend"], "legacy_frontend_stable_pin": stable_pin,
                    "legacy_frontend_evidence_sha256": proof["legacy_frontend_evidence_sha256"]}
        probe = SimpleNamespace(legacy_frontend_blue_sha256=stable_pin, legacy_frontend_compose_proof=self.compose)
        self.assertEqual(legacy_frontend_ready_pin({"roles": {"frontend": frontend}}, probe), stable_pin)
        for field, invalid in (("legacy_frontend_stable_pin", self.pin), ("legacy_frontend_evidence_sha256", "not-a-hash")):
            missing = {key: value for key, value in frontend.items() if key != field}
            for rejected in (missing, {**frontend, field: invalid}):
                with self.subTest(field=field, value=rejected.get(field)), self.assertRaisesRegex(Stop, "STABLE_EVIDENCE_REQUIRED"):
                    legacy_frontend_ready_pin({"roles": {"frontend": rejected}}, probe)

    def test_semantic_network_changes_are_rejected(self):
        changes = {
            "effective IP": lambda networks: networks["proxy"].update(IPAddress="172.30.250.5"),
            "NetworkID": lambda networks: networks["proxy"].update(NetworkID="0" * 64),
            "network name": lambda networks: networks.update(other=networks.pop("proxy")),
            "prefix": lambda networks: networks["proxy"].update(IPPrefixLen=25),
            "gateway": lambda networks: networks["edge"].update(Gateway="172.19.0.254"),
            "missing required alias": lambda networks: networks["proxy"]["Aliases"].remove("frontend"),
            "unauthorized extra alias": lambda networks: networks["proxy"]["Aliases"].append("other"),
            "new driver option": lambda networks: networks["proxy"].update(DriverOpts={"other": "value"}),
            "new configured IP": lambda networks: networks["proxy"]["IPAMConfig"].update(IPv4Address="172.30.250.6"),
        }
        for name, change in changes.items():
            with self.subTest(name=name), self.assertRaises(Stop):
                changed = deepcopy(self.after)
                change(changed["historical"]["networks"])
                self.verify(changed)

    def test_identity_changes_outside_network_are_rejected(self):
        changes = {
            "image ID": lambda projection: projection["image"].update(id="sha256:" + "e" * 64),
            "RepoDigest": lambda projection: projection["image"]["repo_digests"].append("other"),
            "image config": lambda projection: projection["image"].update(config_sha256="e" * 64),
            "RootFS": lambda projection: projection["image"]["rootfs_layers"].append("sha256:" + "e" * 64),
            "container identity": lambda projection: projection["historical"].update(container_id="e" * 64),
            "container config": lambda projection: projection["historical"].update(container_config_sha256="e" * 64),
            "HostConfig": lambda projection: projection["historical"].update(host_config_sha256="e" * 64),
            "mount": lambda projection: projection["historical"]["mounts"][0].update(Source="/other"),
            "published port": lambda projection: projection["historical"]["ports"]["80/tcp"][0].update(HostPort="81"),
            "Caddy version": lambda projection: projection["content"].update(caddy_version="different"),
            "Caddyfile": lambda projection: projection["content"].update(caddyfile_sha256="e" * 64),
            "index": lambda projection: projection["content"].update(index_sha256="e" * 64),
            "asset": lambda projection: projection["content"]["asset_sha256"].update({"/assets/app.js": "e" * 64}),
        }
        for name, change in changes.items():
            with self.subTest(name=name), self.assertRaises(Stop):
                changed = deepcopy(self.after)
                change(changed)
                self.verify(changed)
        with self.subTest(name="full /srv tree"), self.assertRaises(Stop):
            self.verify(self.after, "0" * 64)

    def test_requires_original_compose_intent(self):
        self.compose["compose_hash_matches_container"] = False
        with self.assertRaises(Stop):
            self.verify(self.after)

    def test_explicit_mac_does_not_become_volatile(self):
        first = self.compose["endpoints"][0]
        first["endpoint_mac"] = self.before["historical"]["networks"][first["name"]]["MacAddress"]
        self.verify(self.before)
        with self.assertRaises(Stop):
            self.verify(self.after)


if __name__ == "__main__":
    unittest.main()
