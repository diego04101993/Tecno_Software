"""Exercise explicit legacy rollback attestation and its protected identities."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from gates import DockerProbe, GateError, LEGACY_BLUE_CLASS
from legacy_frontend_blue import (
    attest_reconnected_historical, classify_historical, historical_evidence, historical_reconnect_policy,
    require_historical_ownership, seal_manifest, sha,
)
from release_state import Stop, digest
from test_legacy_frontend_blue import HISTORICAL_ID, IMAGE, PRIVATE_SOURCE, SLOT_CADDYFILE, ReadFixture, fixtures


class HistoricalReconnectTests(unittest.TestCase):
    def setUp(self):
        historical, self.image, self.policy = fixtures()
        self.source = ReadFixture()
        self.historical = historical
        historical["Config"]["Labels"]["com.docker.compose.config-hash"] = "a" * 64
        historical["State"]["StartedAt"] = "2026-11-09T00:00:02Z"
        historical["Mounts"][0]["Driver"] = "local"
        self.policy = replace(self.policy, historical_config_sha256=digest(historical["Config"]),
                              historical_mounts_sha256=digest(historical["Mounts"]))
        expected = historical["NetworkSettings"]["Networks"]
        for index, (name, row) in enumerate(expected.items(), start=1):
            row.update(EndpointID=str(index + 6) * 64, MacAddress=f"02:00:00:00:00:0{index}", IPPrefixLen=24,
                       Aliases=[historical["Name"].lstrip("/"), "frontend"], DriverOpts=None,
                       GlobalIPv6Address="", GlobalIPv6PrefixLen=0, IPv6Gateway="", IPAMConfig=None,
                       Gateway="172.19.0.1" if name == "edge" else "")
        expected["proxy"]["IPAMConfig"] = {"IPv4Address": "172.30.250.2"}
        before = historical_evidence(historical, self.image, self.source.read, policy=self.policy,
                                     read_host=self.source.read_host)
        slot = {"network_name": "proxy", "network_id": expected["proxy"]["NetworkID"], "ipv4": "172.30.250.4",
                "slot_caddyfile_sha256": sha(SLOT_CADDYFILE), "slot_caddyfile_source": PRIVATE_SOURCE}
        self.manifest = seal_manifest(before, slot, policy=self.policy)
        self.pin = self.manifest["sha256"]
        self.planned = {name: row["IPAddress"] for name, row in expected.items()}
        self.compose = {
            "compose_files_match_prestage": True, "compose_hash_matches_container": True,
            "container_create_hash": "a" * 64, "container_network_mode": "edge", "container_declared_mac": None,
            "endpoints": [{"name": name, "service_mac": None, "endpoint_mac": None, "endpoint_driver_opts": None,
                           "ipv4_address": (row["IPAMConfig"] or {}).get("IPv4Address"), "ipv6_address": None,
                           "link_local_ips": None} for name, row in expected.items()],
        }
        self.observed = deepcopy(historical)
        for index, (name, row) in enumerate(self.observed["NetworkSettings"]["Networks"].items(), start=1):
            row.update(EndpointID=str(index + 3) * 64, MacAddress=f"02:00:00:00:00:1{index}", DriverOpts={},
                       IPAMConfig={"IPv4Address": self.planned[name]}, Aliases=list(reversed(row["Aliases"])))
        self.observed["State"].update(Pid=4321, StartedAt="2026-11-09T00:01:02Z")

    def rules(self):
        return historical_reconnect_policy(self.historical["NetworkSettings"]["Networks"], self.compose, self.planned)

    def attest(self):
        return attest_reconnected_historical(self.observed, self.image, self.source.read, self.manifest, self.pin,
                                             reconnect=self.rules(), policy=self.policy, read_host=self.source.read_host)

    def test_exact_reconnect_and_fresh_epoch_with_canonical_aliases(self):
        evidence = self.attest()
        self.assertEqual(set(evidence["stable_network_contract"]), {"edge", "proxy"})
        self.assertEqual(evidence["stable_network_sha256"], digest(evidence["stable_network_contract"]))
        self.assertEqual(evidence["raw_observed_network_settings"], self.observed["NetworkSettings"]["Networks"])
        self.assertEqual(evidence["raw_expected_network_settings"], self.historical["NetworkSettings"]["Networks"])
        self.assertNotEqual(evidence["volatile_endpoint_evidence"]["proxy"]["approved"]["EndpointID"],
                            evidence["volatile_endpoint_evidence"]["proxy"]["observed"]["EndpointID"])
        with self.assertRaisesRegex(Stop, "LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED"):
            classify_historical(self.observed, self.image, self.source.read, self.manifest, self.pin,
                                policy=self.policy, read_host=self.source.read_host)

    def test_container_image_config_mount_ports_and_caddyfile_remain_exact(self):
        original = self.observed
        mutations = {
            "container": lambda row: row.update(Id="f" * 64),
            "image": lambda row: row.update(Image="sha256:" + "f" * 64),
            "config": lambda row: row["Config"].update(WorkingDir="/changed"),
            "host config": lambda row: row["HostConfig"].update(Privileged=True),
            "mount": lambda row: row["Mounts"][0].update(Source="/wrong-volume"),
            "port": lambda row: row["NetworkSettings"]["Ports"]["80/tcp"][0].update(HostPort="81"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), self.assertRaisesRegex(Stop, "DRIFT"):
                self.observed = deepcopy(original)
                mutate(self.observed)
                self.attest()
        self.observed = original
        self.source.caddyfile += b" changed"
        with self.assertRaisesRegex(Stop, "CONTENT_DRIFTED"):
            self.attest()

    def test_missing_generated_proof_and_explicit_mac_do_not_allow_mac_drift(self):
        self.compose["compose_hash_matches_container"] = False
        with self.assertRaisesRegex(Stop, "COMPOSE_UNPROVEN"):
            self.attest()
        self.compose["compose_hash_matches_container"] = True
        self.compose["endpoints"][0]["endpoint_mac"] = self.historical["NetworkSettings"]["Networks"]["edge"]["MacAddress"]
        with self.assertRaisesRegex(Stop, "RECONNECTED_NETWORK_DRIFT"):
            self.attest()

    def test_unrecognized_endpoint_fields_are_still_compared(self):
        endpoint = self.observed["NetworkSettings"]["Networks"]["proxy"]
        endpoint["HypotheticalField"] = "unexpected"
        with self.assertRaisesRegex(Stop, "RECONNECTED_NETWORK_DRIFT"):
            self.attest()

    def test_malformed_compose_proof_does_not_establish_absence(self):
        self.compose["endpoints"][0]["link_local_ips"] = False
        with self.assertRaisesRegex(Stop, "COMPOSE_ENDPOINT_INVALID"):
            self.attest()

    def test_membership_ports_and_tls_are_independent_hard_gates(self):
        historical = self.manifest["payload"]["historical"]
        name = historical["name"]
        attached = {"edge": 1, "proxy": 1}
        require_historical_ownership(historical, attached, name, name)
        for member, port, tls, error in (({"edge": 1, "proxy": 0}, name, name, "MEMBERSHIP"),
                                         ({"edge": 2, "proxy": 1}, name, name, "MEMBERSHIP"),
                                         (attached, "another-frontend", name, "PORT_OWNER"),
                                         (attached, name, "another-writer", "TLS_WRITER")):
            with self.subTest(error=error), self.assertRaisesRegex(Stop, error):
                require_historical_ownership(historical, member, port, tls)

    def live_probe(self, *, final=None, peer=None, reachable=True, endpoints=None):
        probe = DockerProbe.__new__(DockerProbe)
        probe.legacy_frontend_blue = self.manifest["payload"]
        probe.legacy_frontend_blue_sha256 = self.pin
        probe.legacy_frontend_policy = self.policy
        probe.edge_name = self.policy.historical_name
        probe.legacy_blue_endpoint = "172.30.250.3:8000"
        probe.mount_observations = None
        probe.classify_legacy_blue = lambda: (LEGACY_BLUE_CLASS, {})
        scans = [self.observed, final or self.observed]
        if peer is None:
            scans.insert(0, self.observed)
        scans = iter(scans)

        def inspect(*args):
            match args:
                case ("container", "inspect", actual_id) if actual_id == HISTORICAL_ID:
                    return [next(scans)]
                case ("container", "inspect", actual_id, _) if actual_id == HISTORICAL_ID and peer is not None:
                    return [self.observed, peer]
                case ("image", "inspect", image_id) if image_id == IMAGE:
                    return [self.image]
                case ("network", "inspect", name):
                    row = (endpoints or self.observed["NetworkSettings"]["Networks"])[name]
                    return [{"Name": name, "Id": row["NetworkID"], "Containers": {HISTORICAL_ID: {
                        "Name": self.policy.historical_name, "EndpointID": row["EndpointID"], "MacAddress": row["MacAddress"],
                        "IPv4Address": f'{row["IPAddress"]}/{row["IPPrefixLen"]}', "IPv6Address": ""}}}]
            raise AssertionError(args)
        def read(*args):
            if args == ("ps", "--quiet", "--no-trunc"):
                ids = [HISTORICAL_ID] + ([peer["Id"]] if peer else [])
                return ("\n".join(ids) + "\n").encode()
            if args == ("exec", HISTORICAL_ID, "nc", "-z", "-w", "2", "172.30.250.3", "8000"):
                if not reachable:
                    raise GateError("BACKEND_UNREACHABLE")
                return b""
            return self.source.read(*args)
        probe._json = inspect
        probe._run = read
        with patch("gates.attest_reconnected_historical", side_effect=lambda *args, **kw: attest_reconnected_historical(
                *args, **kw, read_host=self.source.read_host)):
            return probe.verify_reconnected_legacy_frontend_source(compose_proof=self.compose,
                                                                    reconnected_ipv4=self.planned)

    def test_live_reconnect_checks_owners_membership_and_backend(self):
        self.assertEqual(self.live_probe(), self.attest())
        peer = {"Id": "e" * 64, "Name": "/unexpected", "State": {"Running": True}, "Mounts": [],
                "NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": "80"}]}}}
        with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.live_probe(peer=peer)
        peer["NetworkSettings"]["Ports"] = {}
        peer["Mounts"] = deepcopy(self.observed["Mounts"])
        with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.live_probe(peer=peer)
        with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.live_probe(reachable=False)
        wrong = deepcopy(self.observed["NetworkSettings"]["Networks"])
        wrong["proxy"]["EndpointID"] = "1" * 64
        with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.live_probe(endpoints=wrong)

    def test_live_probe_does_not_tolerate_a_second_reconnect_during_inspection(self):
        later = deepcopy(self.observed)
        later["NetworkSettings"]["Networks"]["proxy"]["EndpointID"] = "6" * 64
        with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.live_probe(final=later)


if __name__ == "__main__":
    unittest.main()
