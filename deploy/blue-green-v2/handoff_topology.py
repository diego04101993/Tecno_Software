"""Observe the exact local Docker ownership needed by the initial BLUE handoff."""

from copy import deepcopy
from ipaddress import IPv4Address, ip_network
import re

from docker_mounts import canonicalize_mounts
from docker_networks import address
from gates import hex_digest
from release_state import Stop, digest, require


PORTS = {"80/tcp", "443/tcp"}
HEALTHY_ROLES = {"backend", "frontend", "postgres"}


def _docker_map(raw):
    require(raw is None or type(raw) is dict, "HANDOFF_TOPOLOGY_EVIDENCE_INVALID")
    return {} if raw is None else raw


def _ports(table):
    result = {}
    for key, rows in _docker_map(table).items():
        require(type(key) is str and (rows is None or type(rows) is list), "HANDOFF_PORT_BINDINGS_INVALID")
        if rows:
            require(all(type(row) is dict and type(row["HostIp"]) is str and type(row["HostPort"]) is str
                        for row in rows), "HANDOFF_PORT_BINDINGS_INVALID")
            result[key] = sorted(rows, key=lambda row: (row["HostIp"], row["HostPort"]))
    return result


def _matches_socket_binding(requested, published):
    return requested["HostPort"] == published["HostPort"] and (
        requested["HostIp"] == published["HostIp"]
        or (requested["HostIp"] == "" and published["HostIp"] in ("0.0.0.0", "::")))


def _published_sockets(ports):
    return {(key.rsplit("/", 1)[-1], row["HostPort"])
            for key, rows in ports.items() for row in rows}


def _process(item, running):
    state = item["State"]
    require(state["Running"] is running
            and state["Status"] == ("running" if running else "exited")
            and all(state[field] is False for field in ("OOMKilled", "Dead", "Paused", "Restarting"))
            and type(state["Pid"]) is int and (state["Pid"] > 0 if running else state["Pid"] == 0)
            and type(item["RestartCount"]) is int and item["RestartCount"] >= 0,
            "HANDOFF_CONTAINER_STATE_CHANGED")
    return {"id": item["Id"], "name": item["Name"], "image": item["Image"], "created": item["Created"],
            "pid": state["Pid"], "started_at": state["StartedAt"], "restart_count": item["RestartCount"]}


def _network(data):
    # Retain engine network configuration separately from endpoint membership.
    return {key: value for key, value in data.items() if key != "Containers"}


def _same_network_after_transfer(expected, observed, *, released_preview_ip=None):
    if expected == observed:
        return True
    if released_preview_ip is None or expected["IPAM"]["Driver"] != "default":
        return False
    # Docker's default IPAM reports allocation counters. All unrelated peers
    # and the transferred addresses are checked independently. Removing the
    # approved preview IPv4 frees exactly one allocation in its default pool.
    preview = IPv4Address(released_preview_ip)
    pools = [entry for entry in expected["IPAM"]["Config"]
             if preview in ip_network(entry["Subnet"])]
    if len(pools) != 1 or pools[0].get("IPRange"):
        return False
    planned = deepcopy(expected)
    stats = planned["Status"]["IPAM"]["Subnets"][pools[0]["Subnet"]]
    used, available = stats["IPsInUse"], stats["DynamicIPsAvailable"]
    if type(used) is not int or used < 1 or type(available) is not int or available < 0:
        return False
    stats.update(IPsInUse=used - 1, DynamicIPsAvailable=available + 1)
    return planned == observed


def _member(table, endpoints, name, container_id, container_name):
    endpoint, peer = endpoints[name], table[name]["members"][container_id]
    ipv6 = endpoint["GlobalIPv6Address"]
    require(endpoint["NetworkID"] == table[name]["network"]["Id"]
            and peer["Name"] == container_name.lstrip("/")
            and peer["EndpointID"] == endpoint["EndpointID"] and hex_digest(peer["EndpointID"])
            and peer["MacAddress"] == endpoint["MacAddress"]
            and peer["IPv4Address"] == f'{endpoint["IPAddress"]}/{endpoint["IPPrefixLen"]}'
            and peer["IPv6Address"] == (f'{ipv6}/{endpoint["GlobalIPv6PrefixLen"]}' if ipv6 else ""),
            "HANDOFF_NETWORK_MEMBERSHIP_CHANGED")
    return endpoint


def _endpoint_names(endpoint):
    names = set()
    for key in ("Aliases", "DNSNames"):
        values = endpoint.get(key)
        require(values is None or (type(values) is list and all(type(value) is str for value in values)),
                "HANDOFF_NETWORK_MEMBERSHIP_CHANGED")
        names.update(values or ())
    return names


class HandoffTopology:
    """Pin sockets, TLS ownership, live processes and relevant network attachments.

    Before the move, Edge may mount /data read-write as a standby only while the
    caller proves the exact isolated preview configuration and closed ingress.
    """

    def __init__(self, read_json, *, list_running, historical_id, backend_id, frontend_id, edge_id,
                 postgres_id, proxy_network, ingress_network, tls_volume, trusted_ip="172.30.250.2"):
        self.read_json = read_json
        self.list_running = list_running
        self.roles = {"historical": historical_id, "backend": backend_id, "frontend": frontend_id,
                      "edge": edge_id, "postgres": postgres_id}
        require(all(hex_digest(value) for value in self.roles.values())
                and len(set(self.roles.values())) == len(self.roles), "HANDOFF_TOPOLOGY_IDS_INVALID")
        require(all(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
                    for value in (proxy_network, ingress_network, tls_volume))
                and proxy_network != ingress_network, "HANDOFF_TOPOLOGY_BINDINGS_INVALID")
        require(address(trusted_ip, IPv4Address), "HANDOFF_TRUSTED_PROXY_ADDRESS_INVALID")
        self.proxy, self.ingress = proxy_network, ingress_network
        self.tls_volume, self.trusted_ip = tls_volume, trusted_ip

    def _capture(self, *, moved):
        running = self.list_running()
        require(type(running) is list and all(hex_digest(value) for value in running)
                and len(set(running)) == len(running), "HANDOFF_RUNNING_SET_INVALID")
        historical, edge = self.roles["historical"], self.roles["edge"]
        active_roles = set(self.roles.values()) - ({historical} if moved else set())
        require(active_roles <= set(running) and (historical in running) == (not moved),
                "HANDOFF_ROLE_NOT_RUNNING")
        ids = sorted(set(running) | {historical})
        items = self.read_json("container", "inspect", *ids)
        require(len(items) == len(ids) and {row["Id"] for row in items} == set(ids),
                "HANDOFF_INSPECT_SET_CHANGED")
        containers = {row["Id"]: row for row in items}
        require(all(containers[key]["State"]["Running"] is True for key in running)
                and containers[historical]["State"]["Running"] is (not moved), "HANDOFF_RUNNING_SET_CHANGED")
        nets = self.read_json("network", "inspect", self.proxy, self.ingress)
        require(len(nets) == 2 and {net["Name"] for net in nets} == {self.proxy, self.ingress}
                and all(hex_digest(net["Id"]) and net["Driver"] == "bridge" and net["Scope"] == "local" for net in nets)
                and len({net["Id"] for net in nets}) == 2, "HANDOFF_NETWORK_CONFIG_CHANGED")
        networks = {row["Name"]: {"network": _network(row), "members": _docker_map(row["Containers"])} for row in nets}
        require(networks[self.proxy]["network"]["Internal"] is True
                and networks[self.ingress]["network"]["Internal"] is False, "HANDOFF_NETWORK_ISOLATION_CHANGED")
        roles = {}
        for role, container_id in self.roles.items():
            item = containers[container_id]
            state = item["State"]
            require(role not in HEALTHY_ROLES or state["Health"]["Status"] == "healthy",
                    "HANDOFF_BLUE_OR_POSTGRES_NOT_HEALTHY")
            role_data = {"process": _process(item, container_id in running),
                         "attachments": item["NetworkSettings"]["Networks"],
                         "mounts": canonicalize_mounts(item["Mounts"])}
            if role in ("historical", "edge"):
                role_data["published"] = _ports(item["NetworkSettings"]["Ports"])
                role_data["requested"] = _ports(item["HostConfig"]["PortBindings"])
            if not moved:
                role_data["static_sha256"] = digest({"config": item["Config"], "host": item["HostConfig"]})
            roles[role] = role_data
        owner_id = edge if moved else historical
        owner, standby = (roles["edge"], roles["historical"]) if moved else (roles["historical"], roles["edge"])
        published = owner["published"]
        require(set(published) == PORTS and not standby["published"], "HANDOFF_PORT_OWNER_CHANGED")
        # Include wildcard listeners represented by the active engine. An Edge
        # host-ip placeholder is allowed only for those wildcard publications.
        for role in ("historical", "edge"):
            requested = roles[role]["requested"]
            require(set(requested) == PORTS and all(
                all(any(_matches_socket_binding(expected, actual) for actual in published[key])
                    for expected in requested[key])
                and all(any(_matches_socket_binding(expected, actual) for expected in requested[key])
                        for actual in published[key])
                for key in PORTS), "HANDOFF_PORT_PLAN_CHANGED")
        active_sockets = _published_sockets(published)
        require(all(not active_sockets & _published_sockets(_ports(containers[key]["NetworkSettings"]["Ports"]))
                    for key in running if key != owner_id), "HANDOFF_CONFLICTING_PORT_OWNER")

        tls = []
        for role in ("historical", "edge"):
            mount, = [row for row in roles[role]["mounts"] if row["Destination"] == "/data"]
            require(mount["Name"] == self.tls_volume and mount["Type"] == "volume" and mount["RW"] is True,
                    "HANDOFF_TLS_MOUNT_CHANGED")
            tls.append(mount)
        require(tls[0]["Source"] == tls[1]["Source"], "HANDOFF_TLS_SOURCE_CHANGED")
        configs = [{entry["Source"] for entry in roles[role]["mounts"] if entry["Destination"] == "/config"}
                   for role in ("historical", "edge")]
        require(all(len(config) == 1 for config in configs) and not configs[0] & configs[1],
                "HANDOFF_SHARED_CONFIG_STORAGE")
        writers = {key for key in running if any(
            row["RW"] is True and (row.get("Name") == self.tls_volume or row["Source"] == tls[0]["Source"])
            for row in containers[key]["Mounts"])}
        require(writers == ({edge} if moved else {historical, edge}), "HANDOFF_CONFLICTING_TLS_WRITER")

        for role in ("backend", "frontend"):
            current = roles[role]
            _member(networks, current["attachments"], self.proxy, current["process"]["id"], current["process"]["name"])
        owner_name = owner["process"]["name"]
        trusted = _member(networks, owner["attachments"], self.proxy, owner_id, owner_name)
        ingress = _member(networks, owner["attachments"], self.ingress, owner_id, owner_name)
        require(trusted["IPAddress"] == self.trusted_ip and ingress["IPAddress"], "HANDOFF_TRUSTED_IP_OWNER_CHANGED")
        for name, ip in ((self.proxy, self.trusted_ip), (self.ingress, ingress["IPAddress"])):
            members = networks[name]["members"]
            require([key for key, row in members.items() if row["IPv4Address"].split("/")[0] == ip] == [owner_id],
                    "HANDOFF_ADDRESS_OWNER_CHANGED")
        if moved:
            require({self.proxy, self.ingress}.isdisjoint(standby["attachments"])
                    and all(historical not in entry["members"] for entry in networks.values()),
                    "HANDOFF_HISTORICAL_ATTACHMENT_REMAINS")
            require(set(owner["attachments"]) == {self.proxy, self.ingress}, "HANDOFF_EDGE_ATTACHMENTS_CHANGED")
            protected_names = (_endpoint_names(roles["frontend"]["attachments"][self.proxy])
                               | _endpoint_names(roles["backend"]["attachments"][self.proxy]))
            require(not protected_names & _endpoint_names(trusted), "HANDOFF_TRANSFER_ALIAS_COLLISION")
        else:
            require(set(standby["attachments"]) == {self.proxy} and edge not in networks[self.ingress]["members"],
                    "HANDOFF_EDGE_NOT_PRIVATE")
            _member(networks, standby["attachments"], self.proxy, edge, standby["process"]["name"])
        return {"roles": roles, "networks": networks, "ports_owner": owner_id, "tls_writer": owner_id,
                "ingress_ip": ingress["IPAddress"]}

    def snapshot(self):
        """Capture preparation and fresh, pre-stop validation with one inspect per kind."""
        try:
            return self._capture(moved=False)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise Stop("HANDOFF_TOPOLOGY_EVIDENCE_INVALID") from None

    def verify_moved(self, prepared):
        """Verify only the expected ownership transition and existing process epochs."""
        try:
            moved = self._capture(moved=True)
            edge, historical = self.roles["edge"], self.roles["historical"]
            require(prepared["tls_writer"] == prepared["ports_owner"] == historical
                    and moved["tls_writer"] == moved["ports_owner"] == edge
                    and moved["ingress_ip"] == prepared["ingress_ip"], "HANDOFF_MOVED_OWNERSHIP_MISMATCH")
            before, after = prepared["roles"], moved["roles"]
            for role in self.roles:
                retained = {key: value for key, value in before[role].items() if key != "static_sha256"}
                if role == "historical":
                    retained["process"] = {key: value for key, value in retained["process"].items() if key != "pid"}
                    new_values = {**after[role], "process": {key: value for key, value in after[role]["process"].items()
                                                            if key != "pid"}}
                    require(retained["process"] == new_values["process"] and retained["mounts"] == new_values["mounts"]
                            and retained["requested"] == new_values["requested"], "HANDOFF_HISTORICAL_IDENTITY_CHANGED")
                elif role == "edge":
                    require(all(retained[key] == after[role][key] for key in ("process", "mounts", "requested"))
                            and after[role]["published"] == before["historical"]["published"],
                            "HANDOFF_EDGE_IDENTITY_CHANGED")
                else:
                    require(retained == after[role], "HANDOFF_BLUE_OR_POSTGRES_IDENTITY_CHANGED")
            for name in (self.proxy, self.ingress):
                old, new = prepared["networks"][name], moved["networks"][name]
                unchanged = lambda members: {key: value for key, value in members.items() if key not in (edge, historical)}
                preview_ip = before["edge"]["attachments"][self.proxy]["IPAddress"] if name == self.proxy else None
                require(unchanged(old["members"]) == unchanged(new["members"])
                        and _same_network_after_transfer(old["network"], new["network"],
                                                         released_preview_ip=preview_ip),
                        "HANDOFF_UNRELATED_NETWORK_DRIFT")
                old_attach, new_attach = before["historical"]["attachments"][name], after["edge"]["attachments"][name]
                require(all(old_attach.get(key) == new_attach.get(key)
                            for key in ("NetworkID", "IPAddress", "IPPrefixLen", "Gateway", "GlobalIPv6Address",
                                        "GlobalIPv6PrefixLen", "IPv6Gateway")),
                        "HANDOFF_TRANSFERRED_ATTACHMENT_DRIFT")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise Stop("HANDOFF_TOPOLOGY_EVIDENCE_INVALID") from None
