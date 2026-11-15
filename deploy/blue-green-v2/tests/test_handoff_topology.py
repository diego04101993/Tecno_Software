"""Protect the prepared handoff's exact Docker ownership transitions."""

from copy import deepcopy
import unittest

from handoff_topology import HandoffTopology
from release_state import Stop


IDS = {role: str(index) * 64 for index, role in enumerate(("historical", "backend", "frontend", "edge", "postgres"), 1)}
NETS = {"proxy": "a" * 64, "ingress": "b" * 64}
PUBLISH = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10080"}],
           "443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10443"}]}
NOW = "2026-11-15T19:00:00.000Z"


class TopologyFixture:
    def __init__(self):
        self.running = list(IDS.values())
        self.items = {}
        self.networks = {name: {"Name": name, "Id": network_id, "Driver": "bridge", "Scope": "local",
                                "Internal": name == "proxy", "Containers": {}}
                         for name, network_id in NETS.items()}
        for name, subnet, used in (("proxy", "172.30.250.0/24", 7), ("ingress", "172.31.199.0/24", 5)):
            self.networks[name].update(
                IPAM={"Driver": "default", "Options": {}, "Config": [{"Subnet": subnet}]},
                Status={"IPAM": {"Subnets": {subnet: {"IPsInUse": used, "DynamicIPsAvailable": 256 - used}}}})
        for index, (role, container_id) in enumerate(IDS.items(), 20):
            ingress_role = role in ("historical", "edge")
            self.items[container_id] = {
                "Id": container_id, "Name": "/" + role, "Image": "sha256:" + "a" * 64, "Created": NOW, "RestartCount": 0,
                "Config": {"Env": ["SECRET=test-not-in-receipt"]},
                "State": {"Running": True, "Status": "running", "Pid": index, "StartedAt": NOW,
                          "OOMKilled": False, "Paused": False, "Restarting": False, "Dead": False, "Health": {"Status": "healthy"}},
                "NetworkSettings": {"Networks": {}, "Ports": deepcopy(PUBLISH) if role == "historical" else {}},
                "HostConfig": {"PortBindings": deepcopy(PUBLISH) if ingress_role else {}},
                "Mounts": [{"Type": "volume", "Name": "tls", "Source": "/volumes/tls", "Destination": "/data", "RW": True},
                           {"Type": "bind", "Source": "/settings/" + role, "Destination": "/config", "RW": True}]
                if ingress_role else [],
            }
        self.attach("historical", "proxy", "172.30.250.2", "a")
        self.attach("historical", "ingress", "172.31.199.10", "b")
        self.attach("backend", "proxy", "172.30.250.3", "c")
        self.attach("frontend", "proxy", "172.30.250.4", "d")
        self.attach("edge", "proxy", "172.30.250.5", "e")
        self.calls = []

    def attach(self, role, network, ip, marker):
        item = self.items[IDS[role]]
        mac = f"02:42:00:00:00:1{marker}"
        item["NetworkSettings"]["Networks"][network] = {
            "NetworkID": NETS[network], "EndpointID": marker * 64, "MacAddress": mac,
            "IPAddress": ip, "IPPrefixLen": 24, "GlobalIPv6Address": "", "Aliases": [role],
        }
        self.networks[network]["Containers"][IDS[role]] = {
            "Name": role, "EndpointID": marker * 64, "MacAddress": mac, "IPv4Address": ip + "/24", "IPv6Address": "",
        }

    def read(self, kind, verb, *references):
        self.calls.append((kind, verb))
        self.assert_inspect(verb)
        source = self.items if kind == "container" else self.networks
        return [deepcopy(source[key]) for key in references]

    @staticmethod
    def assert_inspect(verb):
        if verb != "inspect":
            raise AssertionError("Read-only Docker inspection only")

    def move(self):
        self.running.remove(IDS["historical"])
        old, edge = self.items[IDS["historical"]], self.items[IDS["edge"]]
        old["State"].update({"Running": False, "Status": "exited", "Pid": 0})
        old["NetworkSettings"] = {"Networks": {}, "Ports": None}
        for row in self.networks.values():
            del row["Containers"][IDS["historical"]]
        self.attach("edge", "proxy", "172.30.250.2", "f")
        self.attach("edge", "ingress", "172.31.199.10", "9")
        edge["NetworkSettings"]["Ports"] = deepcopy(PUBLISH)
        self.networks["proxy"]["Status"]["IPAM"]["Subnets"]["172.30.250.0/24"].update(
            IPsInUse=6, DynamicIPsAvailable=250)

    def probe(self):
        return HandoffTopology(self.read, list_running=lambda: self.running.copy(), historical_id=IDS["historical"],
                               backend_id=IDS["backend"], frontend_id=IDS["frontend"], edge_id=IDS["edge"],
                               postgres_id=IDS["postgres"], proxy_network="proxy", ingress_network="ingress", tls_volume="tls")


class HandoffTopologyTests(unittest.TestCase):
    def test_exact_transfer_preserves_prepared_roles_and_uses_batched_inspection(self):
        fixture = TopologyFixture()
        topology = fixture.probe()
        prepared = topology.snapshot()
        self.assertEqual(prepared["tls_writer"], IDS["historical"])
        self.assertNotIn("test-not-in-receipt", repr(prepared))
        fixture.move()
        topology.verify_moved(prepared)
        self.assertEqual(prepared["networks"]["proxy"]["network"]["Status"]["IPAM"]["Subnets"]
                         ["172.30.250.0/24"]["IPsInUse"], 7)
        self.assertEqual(fixture.calls, [("container", "inspect"), ("network", "inspect")] * 2)

    def test_prepared_snapshot_detects_network_and_process_changes(self):
        fixture = TopologyFixture()
        topology = fixture.probe()
        prepared = topology.snapshot()
        fixture.items[IDS["frontend"]]["State"]["Pid"] += 1
        fixture.attach("frontend", "proxy", "172.30.250.4", "7")
        self.assertNotEqual(topology.snapshot(), prepared)

    def test_prepared_snapshot_rejects_wrong_postgres_health(self):
        fixture = TopologyFixture()
        fixture.items[IDS["postgres"]]["State"]["Health"]["Status"] = "unhealthy"
        with self.assertRaisesRegex(Stop, "HANDOFF_BLUE_OR_POSTGRES_NOT_HEALTHY"):
            fixture.probe().snapshot()

    def test_competing_tls_writer_or_publisher_blocks_before_downtime(self):
        for kind in ("publisher", "writer"):
            with self.subTest(kind=kind):
                fixture = TopologyFixture()
                intruder = fixture.items[IDS["postgres"]]
                if kind == "publisher":
                    intruder["NetworkSettings"]["Ports"] = {"123/tcp": deepcopy(PUBLISH["443/tcp"])}
                    result = "HANDOFF_CONFLICTING_PORT_OWNER"
                else:
                    intruder["Mounts"] = [deepcopy(fixture.items[IDS["historical"]]["Mounts"][0])]
                    result = "HANDOFF_CONFLICTING_TLS_WRITER"
                with self.assertRaisesRegex(Stop, result):
                    fixture.probe().snapshot()

    def test_different_ingress_address_or_changed_backend_blocks_patch(self):
        for kind in ("address", "backend"):
            with self.subTest(kind=kind):
                fixture = TopologyFixture()
                topology = fixture.probe()
                prepared = topology.snapshot()
                fixture.move()
                if kind == "address":
                    fixture.attach("edge", "ingress", "172.31.199.11", "9")
                    result = "HANDOFF_MOVED_OWNERSHIP_MISMATCH"
                else:
                    fixture.items[IDS["backend"]]["State"]["Pid"] += 1
                    result = "HANDOFF_BLUE_OR_POSTGRES_IDENTITY_CHANGED"
                with self.assertRaisesRegex(Stop, result):
                    topology.verify_moved(prepared)

    def test_unexpected_frontend_alias_on_moved_edge_blocks_patch(self):
        fixture = TopologyFixture()
        topology = fixture.probe()
        prepared = topology.snapshot()
        fixture.move()
        fixture.items[IDS["edge"]]["NetworkSettings"]["Networks"]["proxy"]["Aliases"].append("frontend")
        with self.assertRaisesRegex(Stop, "HANDOFF_TRANSFER_ALIAS_COLLISION"):
            topology.verify_moved(prepared)

    def test_only_expected_preview_allocation_release_can_change_network_metadata(self):
        for field in ("unexpected_counter", "unexpected_state_field", "ingress_counter", "ipam_config"):
            with self.subTest(field=field):
                fixture = TopologyFixture()
                topology = fixture.probe()
                prepared = topology.snapshot()
                fixture.move()
                proxy = fixture.networks["proxy"]
                if field == "unexpected_counter":
                    proxy["Status"]["IPAM"]["Subnets"]["172.30.250.0/24"]["IPsInUse"] = 5
                elif field == "unexpected_state_field":
                    proxy["Status"]["unexpected"] = "anything"
                elif field == "ingress_counter":
                    stats = fixture.networks["ingress"]["Status"]["IPAM"]["Subnets"]["172.31.199.0/24"]
                    stats.update(IPsInUse=4, DynamicIPsAvailable=252)
                else:
                    proxy["IPAM"]["Config"][0]["Gateway"] = "172.30.250.11"
                with self.assertRaisesRegex(Stop, "HANDOFF_UNRELATED_NETWORK_DRIFT"):
                    topology.verify_moved(prepared)


if __name__ == "__main__":
    unittest.main()
