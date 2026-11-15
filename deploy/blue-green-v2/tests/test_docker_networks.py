from copy import deepcopy
from unittest import TestCase

from docker_networks import canonicalize_network_attachment
from release_state import Stop, digest


NETWORK = "tecnocontrol_proxy_net"
ADDRESS = "172.30.250.2"


def attachment():
    return {
        "Aliases": ["tecnocontrol-frontend-1", "frontend"],
        "DNSNames": ["tecnocontrol-frontend-1", "frontend", "a" * 12],
        "DriverOpts": None,
        "EndpointID": "1" * 64,
        "Gateway": "",
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "GwPriority": 0,
        "IPAMConfig": None,
        "IPAddress": ADDRESS,
        "IPPrefixLen": 24,
        "IPv6Gateway": "",
        "Links": None,
        "MacAddress": "02:00:00:00:00:01",
        "NetworkID": "f" * 64,
    }


class NetworkAttachmentTests(TestCase):
    def classify(self, endpoint, *, name=NETWORK, **policy):
        return canonicalize_network_attachment(name, endpoint, **policy)

    def identity(self, endpoint, *, name=NETWORK, **policy):
        stable, _ = self.classify(endpoint, name=name, **policy)
        return digest(stable)

    def test_reconnect_preserves_stable_contract_and_keeps_raw_fields(self):
        before = attachment()
        after = {
            **before,
            "Aliases": before["Aliases"][::-1],
            "EndpointID": "2" * 64,
            "MacAddress": "02:00:00:00:00:02",
            "DriverOpts": {},
            "IPAMConfig": {"IPv4Address": ADDRESS},
        }
        policy = {
            "allow_generated_mac": True,
            "allow_empty_driver_opts": True,
            "reconnected_ipv4": ADDRESS,
        }
        original = deepcopy(after)
        old, old_raw = self.classify(before, **policy)
        stable, raw = self.classify(after, **policy)
        self.assertEqual(digest(old), digest(stable))
        self.assertEqual(digest(old["endpoint"]), digest(stable["endpoint"]))
        self.assertEqual(set(raw), {"EndpointID", "MacAddress", "DriverOpts", "IPAMConfig"})
        self.assertTrue(all(raw[key] != old_raw[key] for key in raw))
        self.assertEqual(after, original)
        raw["IPAMConfig"]["IPv4Address"] = "172.30.250.8"
        stable["endpoint"]["Aliases"].append("another")
        self.assertEqual(after, original)

    def test_network_name_is_exact(self):
        old = attachment()
        self.assertNotEqual(self.identity(old), self.identity(old, name="another_network"))

    def test_network_id_is_exact(self):
        old = attachment()
        self.assertNotEqual(self.identity(old), self.identity({**old, "NetworkID": "e" * 64}))

    def test_effective_ipv4_is_exact(self):
        old = attachment()
        new = {**old, "IPAddress": "172.30.250.8"}
        self.assertNotEqual(self.identity(old), self.identity(new))
        with self.assertRaisesRegex(Stop, "NETWORK_ATTACHMENT_RECONNECT_IPV4_MISMATCH"):
            self.classify(new, reconnected_ipv4=ADDRESS)

    def test_prefix_and_gateway_are_exact(self):
        old = attachment()
        for changes in ({"IPPrefixLen": 25}, {"Gateway": "172.30.250.1"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.identity(old), self.identity({**old, **changes}))

    def test_aliases_compare_as_a_set(self):
        old = attachment()
        current = {**old, "Aliases": list(reversed(old["Aliases"]))}
        self.assertEqual(self.identity(old), self.identity(current))

    def test_missing_alias_is_drift(self):
        old = attachment()
        self.assertNotEqual(self.identity(old), self.identity({**old, "Aliases": old["Aliases"][:-1]}))

    def test_unexpected_alias_is_drift(self):
        old = attachment()
        current = {**old, "Aliases": old["Aliases"] + ["not-approved"]}
        self.assertNotEqual(self.identity(old), self.identity(current))

    def test_fixed_or_unproven_mac_change_is_drift(self):
        old = attachment()
        current = {**old, "MacAddress": "02:00:00:00:00:02"}
        self.assertNotEqual(self.identity(old), self.identity(current))
        self.assertEqual(self.identity(old, allow_generated_mac=True),
                         self.identity(current, allow_generated_mac=True))

    def test_empty_driver_options_need_explicit_normalization(self):
        old = attachment()
        empty = {**old, "DriverOpts": {}}
        missing = {key: value for key, value in old.items() if key != "DriverOpts"}
        self.assertEqual(len({self.identity(row) for row in (old, empty, missing)}), 3)
        self.assertEqual(len({self.identity(row, allow_empty_driver_opts=True)
                              for row in (old, empty, missing)}), 1)
        self.assertNotIn("DriverOpts", self.classify(missing, allow_empty_driver_opts=True)[1])

    def test_driver_options_values_are_exact(self):
        old = {**attachment(), "DriverOpts": {"bind": "before", "option": "same"}}
        current = {**old, "DriverOpts": {"option": "same", "bind": "after"}}
        self.assertNotEqual(self.identity(old, allow_empty_driver_opts=True),
                            self.identity(current, allow_empty_driver_opts=True))
        self.assertNotEqual(self.identity(old, allow_empty_driver_opts=True),
                            self.identity({**old, "DriverOpts": {}}, allow_empty_driver_opts=True))

    def test_malformed_driver_options_are_rejected_even_when_empty(self):
        for opts in ([], False, {"invalid": 1}):
            with self.subTest(opts=opts), self.assertRaisesRegex(Stop, "INVALID_NETWORK_ATTACHMENT_DRIVER_OPTS"):
                self.classify({**attachment(), "DriverOpts": opts}, allow_empty_driver_opts=True)

    def test_ipam_materializes_only_the_authorized_ipv4(self):
        old = attachment()
        new = {**old, "IPAMConfig": {"IPv4Address": ADDRESS}}
        self.assertNotEqual(self.identity(old), self.identity(new))
        self.assertEqual(self.identity(old, reconnected_ipv4=ADDRESS), self.identity(new, reconnected_ipv4=ADDRESS))
        with self.assertRaisesRegex(Stop, "NETWORK_ATTACHMENT_IPAM_IPV4_MISMATCH"):
            self.classify({**old, "IPAMConfig": {"IPv4Address": "172.30.250.3"}}, reconnected_ipv4=ADDRESS)

    def test_ipv6_and_link_local_intent_remain_exact(self):
        old = {
            **attachment(),
            "GlobalIPv6Address": "fdd0::2",
            "GlobalIPv6PrefixLen": 64,
            "IPv6Gateway": "fdd0::1",
            "IPAMConfig": {"IPv6Address": "fdd0::2", "LinkLocalIPs": ["fe80::3"]},
        }
        options = {"reconnected_ipv4": ADDRESS}
        old_hash = self.identity(old, **options)
        for patch in ({"GlobalIPv6PrefixLen": 65}, {"IPv6Gateway": "fdd0::9"},
                      {"IPAMConfig": {"IPv6Address": "fdd0::2", "LinkLocalIPs": ["fe80::4"]}},
                      {"GlobalIPv6Address": "fdd0::3", "IPAMConfig": {"IPv6Address": "fdd0::3", "LinkLocalIPs": ["fe80::3"]}}):
            with self.subTest(patch=patch):
                self.assertNotEqual(old_hash, self.identity({**old, **patch}, **options))
        with self.assertRaisesRegex(Stop, "NETWORK_ATTACHMENT_IPAM_IPV6_MISMATCH"):
            self.classify({**old, "IPAMConfig": {"IPv6Address": "fdd0::4"}}, **options)

    def test_unknown_fields_remain_exact_at_both_levels(self):
        old = {**attachment(), "FutureField": True, "IPAMConfig": {"FutureIntent": ["a", "b"]}}
        options = {"allow_empty_driver_opts": True, "reconnected_ipv4": ADDRESS}
        old_hash = self.identity(old, **options)
        self.assertNotEqual(old_hash, self.identity({**old, "FutureField": 1}, **options))
        self.assertNotEqual(old_hash, self.identity({**old, "IPAMConfig": {"FutureIntent": ["b", "a"]}}, **options))
