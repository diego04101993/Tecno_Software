"""Network attachment evidence for an explicitly authorized Docker reconnect."""

from copy import deepcopy
from ipaddress import IPv4Address, IPv6Address, ip_address
import re

from release_state import require


VOLATILE_ENDPOINT_FIELDS = ("EndpointID", "MacAddress", "DriverOpts", "IPAMConfig")
DOCKER_ID = re.compile(r"[0-9a-f]{64}")
MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


def address(value, address_type):
    if type(value) is not str:
        return False
    try:
        return isinstance(ip_address(value), address_type)
    except ValueError:
        return False


def canonicalize_network_attachment(
    network_name, endpoint, *, allow_generated_mac=False, allow_empty_driver_opts=False, reconnected_ipv4=None
):
    """Compare reconnects without dropping configuration or rewriting raw evidence.

    The caller supplies permissions independently of the endpoint being inspected.
    Use existing exact checks when no reconnect was authorized.
    """
    require(type(network_name) is str and network_name and type(endpoint) is dict,
            "INVALID_NETWORK_ATTACHMENT")
    require(type(allow_generated_mac) is bool and type(allow_empty_driver_opts) is bool,
            "INVALID_NETWORK_RECONNECT_POLICY")
    for field in ("NetworkID", "EndpointID"):
        value = endpoint.get(field)
        require(type(value) is str and DOCKER_ID.fullmatch(value), "INVALID_NETWORK_ATTACHMENT_ID")
    mac = endpoint.get("MacAddress")
    require(type(mac) is str and MAC.fullmatch(mac), "INVALID_NETWORK_ATTACHMENT_MAC")
    ipv4 = endpoint.get("IPAddress")
    prefix = endpoint.get("IPPrefixLen")
    require(address(ipv4, IPv4Address) and type(prefix) is int and 0 <= prefix <= 32,
            "INVALID_NETWORK_ATTACHMENT_IPV4")
    for field, family in (("Gateway", IPv4Address), ("GlobalIPv6Address", IPv6Address),
                          ("IPv6Gateway", IPv6Address)):
        if field in endpoint:
            value = endpoint[field]
            require(value == "" or address(value, family), "INVALID_NETWORK_ATTACHMENT_ADDRESS")
    if "GlobalIPv6PrefixLen" in endpoint:
        prefix6 = endpoint["GlobalIPv6PrefixLen"]
        require(type(prefix6) is int and 0 <= prefix6 <= 128, "INVALID_NETWORK_ATTACHMENT_IPV6_PREFIX")

    stable = deepcopy(endpoint)
    volatile = {field: deepcopy(endpoint[field]) for field in VOLATILE_ENDPOINT_FIELDS if field in endpoint}
    del stable["EndpointID"]
    if allow_generated_mac:
        del stable["MacAddress"]
    if "Aliases" in endpoint:
        aliases = endpoint["Aliases"]
        require(aliases is None or type(aliases) is list and all(type(alias) is str and alias for alias in aliases),
                "INVALID_NETWORK_ATTACHMENT_ALIASES")
        if aliases is not None:
            stable["Aliases"] = sorted(set(aliases))

    opts = endpoint.get("DriverOpts")
    require(opts is None or type(opts) is dict and all(type(k) is str and type(v) is str for k, v in opts.items()),
            "INVALID_NETWORK_ATTACHMENT_DRIVER_OPTS")
    if allow_empty_driver_opts and not opts:
        stable.pop("DriverOpts", None)

    ipam = endpoint.get("IPAMConfig")
    require(ipam is None or type(ipam) is dict, "INVALID_NETWORK_ATTACHMENT_IPAM")
    if ipam is not None:
        if "IPv4Address" in ipam:
            require(ipam["IPv4Address"] == ipv4, "NETWORK_ATTACHMENT_IPAM_IPV4_MISMATCH")
        if "IPv6Address" in ipam:
            require(address(ipam["IPv6Address"], IPv6Address) and ipam["IPv6Address"] == endpoint.get("GlobalIPv6Address"),
                    "NETWORK_ATTACHMENT_IPAM_IPV6_MISMATCH")
        if "LinkLocalIPs" in ipam:
            values = ipam["LinkLocalIPs"]
            require(type(values) is list and all(address(value, (IPv4Address, IPv6Address)) for value in values),
                    "INVALID_NETWORK_ATTACHMENT_LINK_LOCAL_IPS")
    if reconnected_ipv4 is not None:
        require(address(reconnected_ipv4, IPv4Address) and reconnected_ipv4 == ipv4,
                "NETWORK_ATTACHMENT_RECONNECT_IPV4_MISMATCH")
        intent = stable.pop("IPAMConfig", None)
        if intent is not None:
            intent.pop("IPv4Address", None)
            if intent:
                stable["IPAMConfig"] = intent

    return {"network_name": network_name, "endpoint": stable}, volatile
