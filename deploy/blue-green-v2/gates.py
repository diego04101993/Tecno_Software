"""Read-only slot identity and authenticated release-gate evidence.

Only the trusted QA runner or passive observer signs evidence. An HMAC binds
the receipt to a release, process and route; it does not replace observation.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic
from uuid import UUID

from docker_config import parse_optional_config
from docker_mounts import canonicalize_mounts, mount_evidence
from legacy_frontend_blue import (
    LEGACY_FRONTEND_IMAGE_EXACT, R3_POLICY, READY_ADOPTION, attest_reconnected_historical,
    classify_historical, historical_reconnect_policy, measure_static_content, require_historical_ownership,
    verify_historical_stable_projection, verify_manifest, verify_private_frontend,
)
from legacy_frontend_inventory import measure_srv_tree
from release_state import EPOCH_IDENTITY_FIELDS, IDENTITY_FIELDS, Stop, canonical, digest, now, same_runtime_epoch, validate_runtime_epoch


class GateError(Stop):
    pass


def require(condition, code):
    if not condition:
        raise GateError(code)


def utc(value):
    require(isinstance(value, str), "INVALID_TIMESTAMP")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise GateError("INVALID_TIMESTAMP") from None
    require(result.utcoffset() == timedelta(0), "INVALID_TIMESTAMP")
    return result


def number(value, minimum=0):
    return type(value) is int and value >= minimum


def identifier(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def hex_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def scope_name(role, slot):
    require(role in ("api", "frontend") and slot in ("BLUE", "GREEN"), "INVALID_SLOT")
    return f"{'backend' if role == 'api' else 'frontend'}-{slot.lower()}"


BACKEND_ARGV = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                "--proxy-headers", "--workers", "1", "--lifespan", "off"]
FRONTEND_CONFIG = "/etc/caddy/Caddyfile.bgv2"
FRONTEND_ARGV = ["caddy", "run", "--config", FRONTEND_CONFIG, "--adapter", "caddyfile"]
REVISION_LABEL = "org.opencontainers.image.revision"
TREE_LABEL = "io.tecnocontrol.git.tree"
DNS_FIELDS = ("Dns", "DnsOptions", "DnsSearch")
LEGACY_BLUE_CLASS = "LIVE_BACKEND_IS_ADOPTED_LEGACY_BLUE_EXACT"
PRIVATE_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def legacy_blue_endpoint(value):
    """A legacy endpoint is an exact RFC1918 address, never a DNS alias."""
    require(isinstance(value, str), "LEGACY_BLUE_ENDPOINT_INVALID")
    host, _, port = value.rpartition(":")
    try:
        ip = IPv4Address(host)
    except ValueError:
        raise GateError("LEGACY_BLUE_ENDPOINT_INVALID") from None
    require(value == f"{ip}:8000" and port == "8000"
            and any(ip in network for network in PRIVATE_NETWORKS), "LEGACY_BLUE_ENDPOINT_INVALID")
    return value


def comparison_host_config(raw):
    """The OBS6 DNS exception only normalizes an explicit unset value."""
    result = dict(raw)
    for field in DNS_FIELDS:
        if field in raw:
            value = raw[field]
            require(value is None or (type(value) is list and all(type(x) is str for x in value)),
                    "INVALID_DNS_FIELD")
            if value is None:
                result[field] = []
    return result


def effective_comparison_host_config(raw):
    """Compare OOM-killer defaults without changing historical identity hashes."""
    require(type(raw) is dict, "INVALID_HOSTCONFIG")
    result = comparison_host_config(raw)
    value = raw.get("OomKillDisable")
    require(value is None or type(value) is bool, "INVALID_OOM_KILL_DISABLE")
    result["OomKillDisable"] = False if value is None else value
    return result


def live_identity(item, image, network, peers, binding, role, slot, argv, ticks,
                  frontend_config_hash=None, raw_argv=None, network_namespace=None, *, legacy_compose=None,
                  allow_restarted=False, legacy_frontend_proof=None, legacy_frontend_policy=R3_POLICY):
    """Project Docker evidence without persisting credentials or raw config."""
    if legacy_frontend_proof is not None:
        release = legacy_frontend_policy.release
        require((role, slot) == ("frontend", "BLUE") and legacy_compose is None and legacy_frontend_proof == {
            "classification": LEGACY_FRONTEND_IMAGE_EXACT, "container_id": item["Id"], "image_id": image["Id"],
        } and image["Id"] == release["images"]["frontend"]
                and all(binding[field] == release[field] for field in ("revision", "tree")), "LEGACY_FRONTEND_SCOPE_INVALID")
    alias = scope_name(role, slot)
    if legacy_compose is None:
        expected_upstream = f"{alias}:{8000 if role == 'api' else 80}"
    else:
        require((role, slot) == ("api", "BLUE"), "LEGACY_BLUE_SCOPE_INVALID")
        expected_upstream = legacy_blue_endpoint(f"{binding['ip']}:8000")
    require(binding["upstream"] == expected_upstream, "UNEXPECTED_UPSTREAM")
    state = item["State"]
    require(state["Running"] is True and state["Status"] == "running"
            and state["OOMKilled"] is False and state.get("Paused") is False
            and state.get("Restarting") is False and state.get("Dead") is False
            and number(item["RestartCount"]) and (allow_restarted or item["RestartCount"] == 0)
            and number(state["Pid"], 1)
            and number(ticks, 1) and state["Health"]["Status"] == "healthy",
            "SLOT_UNHEALTHY_OR_RESTARTED")
    require(item["Name"] == "/" + binding["name"] and hex_digest(item["Id"]), "WRONG_CONTAINER")
    config = item["Config"]
    optional = parse_optional_config(config, validate=require)
    labels = optional["Labels"]
    if legacy_compose is None:
        require(labels.get("io.tecnocontrol.bgv2.slot") == slot
                and labels.get("io.tecnocontrol.bgv2.role") == role, "WRONG_SLOT_LABELS")
    else:
        require(labels.get("com.docker.compose.project") == legacy_compose["project"]
                and labels.get("com.docker.compose.service") == legacy_compose["service"]
                and "io.tecnocontrol.bgv2.slot" not in labels
                and "io.tecnocontrol.bgv2.role" not in labels, "LEGACY_BLUE_COMPOSE_IDENTITY_CHANGED")
    require(item["Image"] == image["Id"] == binding["image_id"]
            and image["Id"].startswith("sha256:") and hex_digest(image["Id"][7:]), "WRONG_IMAGE_ID")
    image_labels = parse_optional_config(image.get("Config"), validate=require)["Labels"]
    permits_missing_oci = legacy_compose is not None or legacy_frontend_proof is not None
    for key, pin in ((REVISION_LABEL, "revision"), (TREE_LABEL, "tree")):
        require(re.fullmatch(r"[0-9a-f]{40}", binding[pin]) and (
            image_labels.get(key) == labels.get(key) == binding[pin] or (
                permits_missing_oci and key not in image_labels and key not in labels
            )), "WRONG_OCI_LABELS")
    settings = item["NetworkSettings"]
    require(not item["HostConfig"].get("PortBindings")
            and item["HostConfig"].get("PublishAllPorts") is False
            and not any(settings.get("Ports", {}).values()), "SLOT_PUBLISHED_PORT")
    endpoint = settings["Networks"][binding["network"]]
    require(str(IPv4Address(binding["ip"])) == endpoint["IPAddress"]
            and (legacy_compose is not None or alias in (endpoint["Aliases"] or []))
            and endpoint["NetworkID"] == network["Id"] and network["Name"] == binding["network"]
            and network["Driver"] == "bridge" and network["Internal"] is True
            and peers == [item["Id"]], "SLOT_NETWORK_NOT_EXCLUSIVE")
    mounts = canonicalize_mounts(item["Mounts"])
    if role == "api":
        env_entries = optional["Env"]
        require(all("=" in value for value in env_entries), "UNSAFE_BACKEND_RUNTIME")
        env = dict(value.split("=", 1) for value in env_entries)
        require(len(env) == len(env_entries) and env.get("AUTO_SEED") == "false"
                and env.get("APP_ENV", "production") == "production" and env.get("WEB_CONCURRENCY", "1") == "1"
                and str(IPv4Address(binding["proxy_ip"])) == env.get("FORWARDED_ALLOW_IPS")
                and env.get("MEDIA_ROOT") == "/app/storage/uploads" and argv == BACKEND_ARGV,
                "UNSAFE_BACKEND_RUNTIME")
        require(any(mount["Type"] == "volume" and mount["Name"] == binding["uploads_volume"]
                    and mount["Destination"] == "/app/storage/uploads" and mount["RW"] is True
                    for mount in mounts), "WRONG_UPLOADS_MOUNT")
    else:
        require(argv == FRONTEND_ARGV and hex_digest(frontend_config_hash)
                and frontend_config_hash == binding["frontend_config_sha256"]
                and len(settings["Networks"]) == 1
                and all(mount["Destination"] not in ("/config", "/data") or mount["RW"] is False
                        for mount in mounts)
                and any(mount["Destination"] == FRONTEND_CONFIG and mount["RW"] is False
                        for mount in mounts), "UNSAFE_FRONTEND_RUNTIME")
    utc(state["StartedAt"])
    return {
        "container_id": item["Id"], "pid": state["Pid"], "start_ticks": ticks, "started_at": state["StartedAt"],
        "image_id": image["Id"], "revision": binding["revision"], "tree": binding["tree"],
        "restart_count": item["RestartCount"], "oom_killed": False, "upstream": expected_upstream,
        "runtime_hash": digest({"config": config, "argv": argv if raw_argv is None else raw_argv,
                                "host": comparison_host_config(item["HostConfig"]),
                                "frontend_config_sha256": frontend_config_hash}),
        "mounts_hash": digest(mounts),
        "network_hash": digest({"endpoints": settings["Networks"], "namespace": network_namespace}),
    }


def generated_mac_networks(value):
    require(type(value) is list and all(type(name) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name)
                                        for name in value) and value == sorted(set(value)),
            "INVALID_GENERATED_MAC_NETWORKS")
    return value


def stable_slot_identity(item, image, identity, raw_argv, binding):
    networks = item["NetworkSettings"]["Networks"]
    require(all(hex_digest(value.get("EndpointID")) and hex_digest(value.get("NetworkID"))
                for value in networks.values()), "SLOT_ENDPOINT_ID_INVALID")
    require(utc(item["Created"]) <= utc(identity["started_at"]), "SLOT_RUNTIME_TIME_INVALID")
    generated = generated_mac_networks(binding.get("generated_mac_networks", []))
    require(set(generated) <= set(networks), "GENERATED_MAC_NETWORK_NOT_PRESENT")
    if generated:
        configured_mac = item["Config"].get("MacAddress")
        require(configured_mac in (None, "") or item["HostConfig"]["NetworkMode"] not in generated,
                "GENERATED_MAC_NETWORK_HAS_CONFIGURED_MAC")
        require(all(type(networks[name].get("MacAddress")) is str
                    and re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", networks[name]["MacAddress"])
                    for name in generated), "GENERATED_MAC_EVIDENCE_INVALID")
    # A new sandbox after reboot can assign a new Docker EndpointID and
    # namespace inode. The runtime epoch also pins operational MAC addresses.
    # Only explicitly reviewed networks may renew their generated MAC.
    stable_networks = {name: {key: value for key, value in endpoint.items()
                              if key != "EndpointID" and (key != "MacAddress" or name not in generated)}
                       for name, endpoint in networks.items()}
    return {**{key: value for key, value in identity.items() if key not in EPOCH_IDENTITY_FIELDS},
            "created_at": item["Created"], "image_config_hash": digest(image["Config"]),
            "command": {"argv": raw_argv, "path": item["Path"], "args": item["Args"]},
            "semantic_network_hash": digest(stable_networks), "generated_mac_networks": generated.copy(),
            "health_contract_hash": digest(item["Config"].get("Healthcheck")), "health": "healthy"}


def runtime_epoch(identity, boot_id):
    epoch = {**{field: identity[field] for field in EPOCH_IDENTITY_FIELDS},
             "boot_id": boot_id, "attested_at": now()}
    validate_runtime_epoch(epoch)
    return epoch


def legacy_blue_payload(item, image, network, peers, binding, argv, ticks, raw_argv, namespace, compose, boot_id):
    identity = live_identity(item, image, network, peers, binding, "api", "BLUE", argv, ticks,
                             raw_argv=raw_argv, network_namespace=namespace, legacy_compose=compose,
                             allow_restarted=True)
    endpoint = item["NetworkSettings"]["Networks"][binding["network"]]
    require(hex_digest(endpoint["EndpointID"]) and hex_digest(endpoint["NetworkID"]),
            "LEGACY_BLUE_ENDPOINT_ID_INVALID")
    require(item.get("Path") == BACKEND_ARGV[0] and item.get("Args") == BACKEND_ARGV[1:]
            and item["Config"].get("Cmd") == BACKEND_ARGV
            and item["Config"].get("Entrypoint") in (None, []), "LEGACY_BLUE_COMMAND_CHANGED")
    return {
        "version": 2, "role": "api", "slot": "BLUE", "name": binding["name"],
        "compose": dict(compose), "stable_identity": stable_slot_identity(item, image, identity, list(raw_argv), binding),
        "runtime_epoch": runtime_epoch(identity, boot_id),
        "endpoint": {"network": binding["network"], "network_id": endpoint["NetworkID"],
                     "ip": binding["ip"], "proxy_ip": binding["proxy_ip"]},
        "uploads_volume": binding["uploads_volume"], "postgres_id": binding["postgres_id"],
    }


def verify_legacy_blue_manifest(manifest, expected_sha256):
    require(isinstance(manifest, dict) and set(manifest) == {"sha256", "payload"}
            and hex_digest(expected_sha256) and manifest["sha256"] == expected_sha256
            and hmac.compare_digest(digest(manifest["payload"]), expected_sha256), "LEGACY_BLUE_MANIFEST_CHECKSUM")
    data = manifest["payload"]
    require(isinstance(data, dict) and set(data) == {
        "version", "role", "slot", "name", "compose", "stable_identity", "runtime_epoch", "endpoint",
        "uploads_volume", "postgres_id",
    } and type(data["version"]) is int and data["version"] == 2 and (data["role"], data["slot"]) == ("api", "BLUE")
            and hex_digest(data["postgres_id"]), "LEGACY_BLUE_MANIFEST_INVALID")
    identity, endpoint, compose = data["stable_identity"], data["endpoint"], data["compose"]
    extra = {"created_at", "image_config_hash", "command", "semantic_network_hash", "health_contract_hash", "health",
             "generated_mac_networks"}
    require(isinstance(identity, dict) and set(identity) == (set(IDENTITY_FIELDS) - set(EPOCH_IDENTITY_FIELDS)) | extra
            and all(hex_digest(identity[field]) for field in ("container_id", "runtime_hash", "mounts_hash",
                                                             "image_config_hash", "semantic_network_hash", "health_contract_hash"))
            and isinstance(identity["image_id"], str) and identity["image_id"].startswith("sha256:")
            and hex_digest(identity["image_id"][7:]) and identity["oom_killed"] is False and identity["health"] == "healthy"
            and all(isinstance(identity[field], str) and re.fullmatch(r"[0-9a-f]{40}", identity[field])
                    for field in ("revision", "tree")), "LEGACY_BLUE_MANIFEST_INVALID")
    generated_mac_networks(identity["generated_mac_networks"])
    validate_runtime_epoch(data["runtime_epoch"])
    require(utc(identity["created_at"]) <= utc(data["runtime_epoch"]["started_at"]),
            "LEGACY_BLUE_RUNTIME_TIME_INVALID")
    require(data["runtime_epoch"]["restart_count"] == 0, "LEGACY_BLUE_INITIAL_RESTART_COUNT")
    require(isinstance(compose, dict) and set(compose) == {"project", "service"}
            and isinstance(compose["project"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", compose["project"])
            and compose["service"] == "backend", "LEGACY_BLUE_MANIFEST_INVALID")
    require(isinstance(endpoint, dict) and set(endpoint) == {"network", "network_id", "ip", "proxy_ip"}
            and hex_digest(endpoint["network_id"])
            and endpoint["proxy_ip"] != endpoint["ip"]
            and legacy_blue_endpoint(f"{endpoint['ip']}:8000") == identity["upstream"]
            and legacy_blue_endpoint(f"{endpoint['proxy_ip']}:8000"), "LEGACY_BLUE_MANIFEST_INVALID")
    command = identity["command"]
    require(all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", value)
                for value in (data["name"], data["uploads_volume"], endpoint["network"]))
            and type(command) is dict and set(command) == {"argv", "path", "args"}
            and command["path"] == BACKEND_ARGV[0] and command["args"] == BACKEND_ARGV[1:]
            and type(command["argv"]) is list and all(type(part) is str for part in command["argv"])
            and backend_argv(command["argv"]) == BACKEND_ARGV, "LEGACY_BLUE_MANIFEST_INVALID")
    return deepcopy(data)


def backend_argv(raw_argv):
    # A Python console script appears as interpreter + script in /proc.
    if (len(raw_argv) == len(BACKEND_ARGV) + 1
            and raw_argv[0] in ("/usr/local/bin/python", "/usr/local/bin/python3.12")
            and raw_argv[1] == "/usr/local/bin/uvicorn"):
        return ["uvicorn", *raw_argv[2:]]
    return raw_argv


class DockerProbe:
    """Read-only Docker commands against an explicitly selected local Engine."""

    def __init__(self, bindings, context="default", edge_name=None, legacy_blue=None, legacy_blue_sha256=None, *,
                 legacy_frontend_blue=None, legacy_frontend_blue_sha256=None, legacy_frontend_policy=R3_POLICY,
                 mount_observations=None):
        require(isinstance(context, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", context), "INVALID_DOCKER_CONTEXT")
        self.bindings = bindings
        self.context = context
        self.mount_observations = mount_observations
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
        self.edge_name = edge_name
        require(edge_name is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", edge_name), "INVALID_EDGE_NAME")
        require((legacy_blue is None) == (legacy_blue_sha256 is None), "LEGACY_BLUE_MANIFEST_APPROVAL_REQUIRED")
        self.legacy_blue = None
        self.legacy_blue_endpoint = None
        if legacy_blue is not None:
            try:
                self.legacy_blue = verify_legacy_blue_manifest(legacy_blue, legacy_blue_sha256)
                self.legacy_blue_endpoint = self.legacy_blue["stable_identity"]["upstream"]
                binding = bindings["api:BLUE"]
                require(edge_name is not None and binding["upstream"] == self.legacy_blue_endpoint
                        and binding["name"] == self.legacy_blue["name"]
                        and binding["postgres_id"] == self.legacy_blue["postgres_id"]
                        and generated_mac_networks(binding.get("generated_mac_networks", []))
                        == self.legacy_blue["stable_identity"]["generated_mac_networks"], "LEGACY_BLUE_BINDING_MISMATCH")
            except (ValueError, KeyError, TypeError, AttributeError):
                raise GateError("LEGACY_BLUE_MANIFEST_INVALID") from None
        self.legacy_blue_sha256 = legacy_blue_sha256
        self.legacy_frontend_blue = None
        self.legacy_frontend_blue_sha256 = None
        self.legacy_frontend_source_sha256 = None
        self.legacy_frontend_compose_proof = None
        self.legacy_frontend_policy = legacy_frontend_policy
        frontend_binding = bindings.get("frontend:BLUE", {})
        require(type(frontend_binding) is dict, "LEGACY_FRONTEND_BINDING_INVALID")
        stable_pin = None
        if "legacy_frontend_adoption" in frontend_binding:
            approved = frontend_binding["legacy_frontend_adoption"]
            manifest_fields = {"manifest", "sha256"}
            stable_fields = manifest_fields | {"stable_pin", "compose_proof"}
            require(legacy_frontend_blue is None and legacy_frontend_blue_sha256 is None
                    and type(approved) is dict and set(approved) in (manifest_fields, stable_fields)
                    and approved["manifest"] is not None and approved["sha256"] is not None,
                    "LEGACY_FRONTEND_MANIFEST_SOURCE_INVALID")
            legacy_frontend_blue, legacy_frontend_blue_sha256 = approved["manifest"], approved["sha256"]
            if set(approved) == stable_fields:
                stable_pin = approved["stable_pin"]
                self.legacy_frontend_compose_proof = deepcopy(approved["compose_proof"])
                require(hex_digest(stable_pin) and type(self.legacy_frontend_compose_proof) is dict,
                        "LEGACY_FRONTEND_STABLE_APPROVAL_REQUIRED")
        require((legacy_frontend_blue is None) == (legacy_frontend_blue_sha256 is None),
                "LEGACY_FRONTEND_MANIFEST_APPROVAL_REQUIRED")
        if legacy_frontend_blue is not None:
            require(self.legacy_blue is not None, "LEGACY_FRONTEND_SCOPE_INVALID")
            payload = verify_manifest(legacy_frontend_blue, legacy_frontend_blue_sha256, policy=legacy_frontend_policy)
            release = payload["release"]
            backend = self.legacy_blue["stable_identity"]
            require(all(frontend_binding.get(field) == backend[field] == release[field]
                        for field in ("revision", "tree")) and frontend_binding.get("image_id") == release["images"]["frontend"]
                    and backend["image_id"] == release["images"]["api"], "LEGACY_FRONTEND_RELEASE_MISMATCH")
            if stable_pin is not None:
                approved_projection = {key: payload[key] for key in ("historical", "image", "content")}
                proof = verify_historical_stable_projection(
                    legacy_frontend_blue, legacy_frontend_blue_sha256, approved_projection,
                    compose_proof=self.legacy_frontend_compose_proof,
                    observed_srv_tree_sha256=legacy_frontend_policy.historical_srv_tree_sha256,
                    policy=legacy_frontend_policy,
                )
                require(proof["legacy_frontend_stable_pin"] == stable_pin, "LEGACY_FRONTEND_STABLE_PIN_MISMATCH")
            self.legacy_frontend_blue = payload
            self.legacy_frontend_source_sha256 = legacy_frontend_blue_sha256
            self.legacy_frontend_blue_sha256 = stable_pin if stable_pin is not None else legacy_frontend_blue_sha256
        endpoint = self._json("context", "inspect", context)[0]["Endpoints"]["docker"]["Host"]
        require(endpoint.startswith(("unix:///", "npipe:////./pipe/")), "NONLOCAL_DOCKER_CONTEXT")

    def _run(self, *args, timeout=15):
        try:
            result = subprocess.run(["docker", "--context", self.context, *args], check=True,
                                    capture_output=True, timeout=timeout, env=self.environment)
        except (OSError, subprocess.SubprocessError):
            raise GateError("SLOT_READ_ONLY_INSPECTION_FAILED") from None
        return result.stdout

    def _json(self, *args):
        return json.loads(self._run(*args))

    def _observe_mounts(self, label, item):
        if self.mount_observations is not None:
            observation = {"inspection": label, "container_id": item["Id"]}
            try:
                observation.update(mount_evidence(item["Mounts"]))
            except Stop as error:
                observation.update(raw_mounts=deepcopy(item["Mounts"]), canonical_mounts=None,
                                   canonical_mounts_sha256=None, error=str(error))
                self.mount_observations.append(observation)
                raise
            self.mount_observations.append(observation)

    def _host_boot_id(self, container, observed=None):
        if observed is None:
            observed = self._run("exec", container, "cat", "/proc/sys/kernel/random/boot_id").decode().strip()
        require(identifier(observed), "INVALID_LEGACY_HOST_BOOT_ID")
        if os.name == "posix":
            require(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == observed,
                    "LEGACY_ENGINE_HOST_BOOT_ID_MISMATCH")
        return observed

    def legacy_engine_boot_id(self):
        require(self.legacy_blue is not None, "LEGACY_BLUE_MANIFEST_APPROVAL_REQUIRED")
        if os.name == "posix":
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            require(identifier(boot_id), "INVALID_LEGACY_HOST_BOOT_ID")
            return boot_id
        # Docker Desktop QA runs the Engine in its local Linux VM.
        return self._host_boot_id(self.legacy_blue["stable_identity"]["container_id"])

    def _postgres(self):
        postgres = self.bindings["api:BLUE"]["postgres_id"]
        require(hex_digest(postgres), "LEGACY_POSTGRES_ID_INVALID")
        item, = self._json("container", "inspect", postgres)
        state = item["State"]
        require(item["Id"] == postgres and state["Running"] is True and state["Status"] == "running"
                and state["OOMKilled"] is False and state.get("Paused") is False
                and state.get("Restarting") is False and state.get("Dead") is False
                and state["Health"]["Status"] == "healthy", "LEGACY_POSTGRES_NOT_EXACT_HEALTHY")
        return postgres

    def _probe(self, role, slot, legacy_compose=None, *, with_epoch=False, legacy_frontend=False):
        if legacy_frontend:
            require((role, slot) == ("frontend", "BLUE") and with_epoch and self.legacy_frontend_blue is not None,
                    "LEGACY_FRONTEND_SCOPE_INVALID")
        alias = scope_name(role, slot)
        binding = self.bindings[f"{role}:{slot}"]
        name = binding["name"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name), "INVALID_CONTAINER_NAME")
        first = self._json("container", "inspect", name)[0]
        self._observe_mounts("slot_before", first)
        container = first["Id"]
        require(hex_digest(container), "WRONG_CONTAINER")
        image_id = first["Image"]
        require(image_id.startswith("sha256:") and hex_digest(image_id[7:]), "WRONG_IMAGE_ID")
        image = self._json("image", "inspect", image_id)[0]
        network_name = binding["network"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", network_name), "INVALID_NETWORK_NAME")
        network = self._json("network", "inspect", network_name)[0]
        member_ids = list((network.get("Containers") or {}).keys())
        require(member_ids and all(hex_digest(entry) for entry in member_ids), "WRONG_NETWORK_MEMBERSHIP")
        peers = []
        edge_peers = []
        for member in self._json("container", "inspect", *member_ids):
            endpoint = member["NetworkSettings"]["Networks"][network_name]
            names = set(endpoint.get("Aliases") or []) | set(endpoint.get("DNSNames") or [])
            names.add(member["Name"].lstrip("/"))
            if legacy_compose is None and alias in names:
                peers.append(member["Id"])
            if legacy_compose is not None and endpoint["IPAddress"] == binding["ip"]:
                peers.append(member["Id"])
            if role == "api" and endpoint["IPAddress"] == binding["proxy_ip"]:
                edge_peers.append(member)
        if role == "api" and self.edge_name is not None:
            require(len(edge_peers) == 1 and edge_peers[0]["Name"] == "/" + self.edge_name
                    and edge_peers[0]["State"]["Running"] is True
                    and edge_peers[0]["NetworkSettings"]["Networks"][network_name]["NetworkID"] == network["Id"],
                    "UNTRUSTED_EDGE_PEER")
        # One exec keeps the cutover verification bounded. NUL separators are
        # external to /proc/1/cmdline, which itself uses a single NUL separator.
        evidence = self._run("exec", container, "sh", "-ec", """
            cat /proc/1/cmdline
            printf '\\000'
            cat /proc/1/stat
            readlink /proc/1/ns/net
            cat /proc/sys/kernel/random/boot_id
            if [ "$1" = frontend ]; then cat /etc/caddy/Caddyfile.bgv2; fi
        """, "slot-inspect", role)
        command, separator, remainder = evidence.partition(b"\0\0")
        require(separator == b"\0\0" and b"\0" not in remainder, "INVALID_SLOT_PROC_EVIDENCE")
        stat, namespace_raw, host_boot_raw, *config_lines = remainder.split(b"\n")
        raw_argv = command.decode().split("\0")
        argv = backend_argv(raw_argv) if role == "api" else raw_argv
        ticks = int(stat.decode().rsplit(")", 1)[1].split()[19])
        namespace = namespace_raw.decode()
        require(re.fullmatch(r"net:\[[0-9]+\]", namespace), "INVALID_SLOT_NAMESPACE")
        config_hash = None
        if role == "frontend":
            config_hash = hashlib.sha256(b"\n".join(config_lines)).hexdigest()
        proof = None
        if legacy_frontend:
            proof = verify_private_frontend(first, image, network, self._run, self.legacy_frontend_blue, binding,
                                            policy=self.legacy_frontend_policy,
                                            **({"verify_srv_tree": True} if self.legacy_frontend_compose_proof is not None else {}))
            require(proof["classification"] == LEGACY_FRONTEND_IMAGE_EXACT and proof["container_id"] == container
                    and proof["image_id"] == image_id, "LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED")
        last = self._json("container", "inspect", name)[0]
        self._observe_mounts("slot_after", last)
        require(first["Id"] == last["Id"] and first["State"]["StartedAt"] == last["State"]["StartedAt"]
                and first["State"]["Pid"] == last["State"]["Pid"], "SLOT_CHANGED_DURING_INSPECTION")
        if legacy_compose is not None:
            return legacy_blue_payload(last, image, network, peers, binding, argv, ticks, raw_argv,
                                       namespace, legacy_compose, self._host_boot_id(container, host_boot_raw.decode()))
        if legacy_frontend:
            require(all(first[field] == last[field] for field in ("Image", "Config", "HostConfig", "NetworkSettings"))
                    and canonical(canonicalize_mounts(first["Mounts"])) == canonical(canonicalize_mounts(last["Mounts"])),
                    "LEGACY_FRONTEND_CHANGED_DURING_INSPECTION")
        identity = live_identity(last, image, network, peers, binding, role, slot, argv, ticks,
                                 config_hash, raw_argv, namespace, allow_restarted=with_epoch,
                                 legacy_frontend_proof=None if proof is None else {
                                     field: proof[field] for field in ("classification", "container_id", "image_id")
                                 }, legacy_frontend_policy=getattr(self, "legacy_frontend_policy", R3_POLICY))
        if legacy_frontend:
            identity["runtime_hash"] = digest({"runtime_hash": identity["runtime_hash"],
                                               "approved_manifest": self.legacy_frontend_blue_sha256})
        if with_epoch:
            return {"identity": identity, "stable_identity": stable_slot_identity(last, image, identity, raw_argv, binding),
                    "runtime_epoch": runtime_epoch(identity, self._host_boot_id(container, host_boot_raw.decode()))}
        return identity

    def capture_legacy_blue(self, *, expected_project, expected_service):
        """Collect candidate evidence read-only. Approval pins its checksum separately."""
        require(self.edge_name is not None and self.legacy_blue is None
                and isinstance(expected_project, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", expected_project)
                and expected_service == "backend", "LEGACY_BLUE_CAPTURE_SCOPE_INVALID")
        payload = self._probe("api", "BLUE", {"project": expected_project, "service": expected_service})
        require(self._postgres() == payload["postgres_id"], "LEGACY_POSTGRES_ID_MISMATCH")
        manifest = {"sha256": digest(payload), "payload": payload}
        verify_legacy_blue_manifest(manifest, manifest["sha256"])
        return manifest

    def legacy_api_snapshot(self):
        require(self.legacy_blue is not None, "LEGACY_BLUE_MANIFEST_APPROVAL_REQUIRED")
        try:
            current = self._probe("api", "BLUE", self.legacy_blue["compose"])
            require({key: value for key, value in current.items() if key != "runtime_epoch"}
                    == {key: value for key, value in self.legacy_blue.items() if key != "runtime_epoch"},
                    "LEGACY_BLUE_UNKNOWN_OR_DRIFTED")
        except (GateError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GateError("LEGACY_BLUE_UNKNOWN_OR_DRIFTED") from None
        epoch = current["runtime_epoch"]
        identity = {field: (epoch if field in EPOCH_IDENTITY_FIELDS else current["stable_identity"])[field]
                    for field in IDENTITY_FIELDS}
        identity["runtime_hash"] = digest({"runtime_hash": identity["runtime_hash"],
                                           "approved_manifest": self.legacy_blue_sha256})
        return {"identity": identity, "runtime_epoch": epoch}

    def classify_legacy_blue(self, approved_epoch=None):
        current = self.legacy_api_snapshot()
        expected = self.legacy_blue["runtime_epoch"] if approved_epoch is None else approved_epoch
        require(same_runtime_epoch(current["runtime_epoch"], expected),
                "STOP_REATTEST_REQUIRED" if current["runtime_epoch"]["boot_id"] == expected["boot_id"]
                else "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT_REQUIRED")
        return LEGACY_BLUE_CLASS, current["identity"]

    def legacy_frontend_snapshot(self):
        require(self.legacy_blue is not None, "LEGACY_BLUE_MANIFEST_APPROVAL_REQUIRED")
        try:
            if getattr(self, "legacy_frontend_blue", None) is not None:
                return self._probe("frontend", "BLUE", with_epoch=True, legacy_frontend=True)
            return self._probe("frontend", "BLUE", with_epoch=True)
        except (GateError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GateError("LEGACY_BLUE_FRONTEND_UNKNOWN_OR_DRIFTED") from None

    def initial_legacy_frontend_identity(self):
        require(self.legacy_frontend_blue is not None, "LEGACY_FRONTEND_MANIFEST_APPROVAL_REQUIRED")
        snapshot = self.legacy_frontend_snapshot()
        require(snapshot["runtime_epoch"]["restart_count"] == 0, "LEGACY_FRONTEND_INITIAL_RESTART_COUNT")
        return snapshot["identity"]

    def verify_initial_legacy_frontend_source(self):
        """Require the original R3 ingress to be present before first adoption."""
        self._verify_historical_frontend_source()

    def verify_reconnected_legacy_frontend_source(self, *, compose_proof, reconnected_ipv4):
        """Read-only rollback source attestation for explicitly reattached endpoints."""
        return self._verify_historical_frontend_source(compose_proof=compose_proof, reconnected_ipv4=reconnected_ipv4)

    def _verify_historical_frontend_source(self, *, compose_proof=None, reconnected_ipv4=None):
        require(self.legacy_frontend_blue is not None, "LEGACY_FRONTEND_MANIFEST_APPROVAL_REQUIRED")
        payload = self.legacy_frontend_blue
        try:
            historical_id = payload["historical"]["container_id"]
            item, = self._json("container", "inspect", historical_id)
            self._observe_mounts("historical_frontend_before", item)
            image, = self._json("image", "inspect", payload["release"]["images"]["frontend"])
            require(item["Name"] == "/" + self.edge_name and item["State"]["Running"] is True,
                    "LEGACY_FRONTEND_HISTORICAL_SOURCE_NOT_ACTIVE")
            pin = getattr(self, "legacy_frontend_source_sha256", self.legacy_frontend_blue_sha256)
            manifest = {"sha256": pin, "payload": payload}
            evidence = None
            stable_compose = getattr(self, "legacy_frontend_compose_proof", None)
            if stable_compose is not None:
                require(classify_historical(item, image, self._run, manifest, pin, policy=self.legacy_frontend_policy,
                                            stable_pin=self.legacy_frontend_blue_sha256, compose_proof=stable_compose)
                        == LEGACY_FRONTEND_IMAGE_EXACT, "LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED")
            if compose_proof is None and reconnected_ipv4 is None:
                if stable_compose is None:
                    require(classify_historical(item, image, self._run, manifest, pin, policy=self.legacy_frontend_policy)
                            == LEGACY_FRONTEND_IMAGE_EXACT, "LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED")
            else:
                historical = payload["historical"]
                reconnect = historical_reconnect_policy(historical["networks"], compose_proof, reconnected_ipv4)
                require(compose_proof["container_create_hash"] == item["Config"]["Labels"].get("com.docker.compose.config-hash")
                        and compose_proof["container_declared_mac"] == item["Config"].get("MacAddress")
                        and compose_proof.get("container_network_mode") == item["HostConfig"]["NetworkMode"],
                        "LEGACY_FRONTEND_RECONNECT_COMPOSE_CONTAINER_DRIFT")
                evidence = attest_reconnected_historical(item, image, self._run, manifest, pin, reconnect=reconnect,
                                                         policy=self.legacy_frontend_policy)
                self._verify_reconnected_ownership(item, historical)
                require(self.classify_legacy_blue()[0] == LEGACY_BLUE_CLASS, "LEGACY_FRONTEND_RECONNECTED_PROXY_DRIFT")
                address, port = self.legacy_blue_endpoint.split(":")
                self._run("exec", historical_id, "nc", "-z", "-w", "2", address, port)
            after, = self._json("container", "inspect", historical_id)
            self._observe_mounts("historical_frontend_after", after)
            require(all(after[field] == item[field] for field in ("Id", "Image", "Config", "HostConfig", "NetworkSettings"))
                    and canonical(canonicalize_mounts(after["Mounts"])) == canonical(canonicalize_mounts(item["Mounts"]))
                    and after["State"]["Running"] is True
                    and all(after["State"][field] == item["State"][field] for field in ("Pid", "StartedAt")),
                    "LEGACY_FRONTEND_CHANGED_DURING_INSPECTION")
            return evidence
        except (Stop, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            if isinstance(exc, Stop) and str(exc) == "DUPLICATE_MOUNT_DESTINATION":
                raise
            raise GateError("LEGACY_FRONTEND_HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED") from None

    def _verify_reconnected_ownership(self, frontend, historical):
        members = {}
        for name, endpoint in frontend["NetworkSettings"]["Networks"].items():
            row, = self._json("network", "inspect", name)
            attached = row["Containers"] or {}
            current = attached.get(frontend["Id"], {})
            ipv6 = endpoint["GlobalIPv6Address"]
            require(row["Name"] == name and row["Id"] == endpoint["NetworkID"]
                    and current.get("Name") == historical["name"]
                    and current.get("EndpointID") == endpoint["EndpointID"]
                    and current.get("MacAddress") == endpoint["MacAddress"]
                    and current.get("IPv4Address") == f'{endpoint["IPAddress"]}/{endpoint["IPPrefixLen"]}'
                    and current.get("IPv6Address") == (f'{ipv6}/{endpoint["GlobalIPv6PrefixLen"]}' if ipv6 else ""),
                    "LEGACY_FRONTEND_RECONNECTED_DOCKER_MEMBERSHIP_DRIFT")
            members[name] = sum(item["Name"] == historical["name"] for item in attached.values())
        containers = self._run("ps", "--quiet", "--no-trunc").decode().splitlines()
        require(containers and all(hex_digest(item) for item in containers) and len(containers) == len(set(containers)),
                "LEGACY_FRONTEND_RECONNECTED_OWNERS_INVALID")
        running = self._json("container", "inspect", *containers)
        require(len(running) == len(containers) and {item["Id"] for item in running} == set(containers)
                and all(item["State"]["Running"] is True for item in running), "LEGACY_FRONTEND_RECONNECTED_OWNERS_CHANGED")
        port_owners = {item["Name"].lstrip("/") for item in running
                       if any(row["HostPort"] in ("80", "443") for rows in item["NetworkSettings"]["Ports"].values()
                              for row in (rows or []))}
        tls, = [mount for mount in historical["mounts"] if mount["Destination"] == "/data"]
        require(tls["Type"] == "volume" and type(tls["Name"]) is str and bool(tls["Name"])
                and tls["RW"] is True, "LEGACY_FRONTEND_RECONNECTED_TLS_MOUNT_UNPROVEN")
        tls_writers = {item["Name"].lstrip("/") for item in running
                       if any(mount["RW"] is True and (mount.get("Name") == tls["Name"] or mount["Source"] == tls["Source"])
                              for mount in item["Mounts"])}
        require_historical_ownership(historical, members, next(iter(port_owners)) if len(port_owners) == 1 else None,
                                     next(iter(tls_writers)) if len(tls_writers) == 1 else None)

    def legacy_postgres_id(self):
        try:
            postgres = self._postgres()
            require(postgres == self.legacy_blue["postgres_id"], "LEGACY_POSTGRES_ID_MISMATCH")
            return postgres
        except (GateError, ValueError, KeyError, TypeError, AttributeError):
            raise GateError("LEGACY_POSTGRES_NOT_EXACT_HEALTHY") from None

    def verify_legacy_runtime_running(self, ready, epoch):
        """Check the admitted BLUE processes without touching their containers."""
        expected = {entry["identity"]["container_id"]: entry["identity"] for entry in ready.values()}
        require(len(expected) == 2 and all(hex_digest(container) for container in expected), "LEGACY_BLUE_DRIFT")
        try:
            require(self.legacy_engine_boot_id() == epoch["boot_id"], "STOP_REATTEST_REQUIRED")
            current = self._json("container", "inspect", *expected)
            require(len(current) == len(expected) and {item["Id"] for item in current} == set(expected),
                    "LEGACY_BLUE_DRIFT")
            for item in current:
                state = item["State"]
                pinned = expected[item["Id"]]
                require(item["Image"] == pinned["image_id"], "LEGACY_BLUE_DRIFT")
                require((state["Pid"], state["StartedAt"], item["RestartCount"])
                        == (pinned["pid"], pinned["started_at"], pinned["restart_count"]),
                        "STOP_REATTEST_REQUIRED")
                require(state["Running"] is True and state["Status"] == "running"
                        and state["OOMKilled"] is False and state.get("Paused") is False
                        and state.get("Restarting") is False and state.get("Dead") is False
                        and state["Health"]["Status"] == "healthy", "LEGACY_BLUE_DRIFT")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GateError("LEGACY_BLUE_DRIFT") from None

    def verify_handoff_sources(self, state, topology):
        """Tie the compact Docker snapshot to the approved live files and BLUE epoch."""
        roles = topology["roles"]
        ready = state["slots"]["BLUE"]["ready"]
        legacy = state["legacy_blue"]
        for role, service in (("api", "backend"), ("frontend", "frontend")):
            approved = ready[role]["identity"]
            observed = roles[service]["process"]
            require(all(observed[field] == approved[pinned] for field, pinned in (
                ("id", "container_id"), ("image", "image_id"), ("pid", "pid"),
                ("started_at", "started_at"), ("restart_count", "restart_count"),
            )), "INVALIDATE_HANDOFF_PREPARED_BLUE_IDENTITY")
        historical = roles["historical"]["process"]
        require(historical["name"] == "/" + self.edge_name
                and roles["postgres"]["process"]["id"] == legacy["postgres_id"]
                and self.legacy_engine_boot_id() == legacy["runtime_epochs"]["api"]["boot_id"],
                "INVALIDATE_HANDOFF_PREPARED_RUNTIME_SCOPE")
        frontend = roles["frontend"]["process"]["id"]
        binding = self.bindings["frontend:BLUE"]
        manifest = self.legacy_frontend_blue
        if manifest is None:
            raw = self._run("exec", frontend, "sha256sum", FRONTEND_CONFIG)
            expected = f'{binding["frontend_config_sha256"]}  {FRONTEND_CONFIG}\n'.encode()
            require(raw == expected, "INVALIDATE_HANDOFF_PREPARED_FRONTEND_CONFIG")
        else:
            require(historical["id"] == manifest["historical"]["container_id"],
                    "INVALIDATE_HANDOFF_PREPARED_HISTORICAL_IDENTITY")
            content = measure_static_content(self._run, frontend, private=True)
            config = content.pop("slot_caddyfile_sha256")
            require(config == binding["frontend_config_sha256"] and content == manifest["content"]
                    and measure_static_content(self._run, historical["id"]) == manifest["content"],
                    "INVALIDATE_HANDOFF_PREPARED_FRONTEND_CONTENT")
            if getattr(self, "legacy_frontend_compose_proof", None) is not None:
                private, = self._json("container", "inspect", frontend)
                historical_item, = self._json("container", "inspect", historical["id"])
                tree = self.legacy_frontend_policy.historical_srv_tree_sha256
                require(measure_srv_tree(private, self._run) == measure_srv_tree(historical_item, self._run) == tree,
                        "INVALIDATE_HANDOFF_PREPARED_FRONTEND_SRV_TREE")

    def require_legacy_reboot_health_ready(self, ready, postgres_id):
        expected = {entry["identity"]["container_id"]: entry["identity"]["image_id"] for entry in ready.values()}
        require(len(expected) == 2 and postgres_id not in expected and hex_digest(postgres_id)
                and all(hex_digest(container) for container in expected), "LEGACY_BLUE_DRIFT")
        expected[postgres_id] = None
        try:
            current = self._json("container", "inspect", *expected)
            require(len(current) == len(expected) and {item["Id"] for item in current} == set(expected),
                    "LEGACY_BLUE_DRIFT")
            for item in current:
                state = item["State"]
                image = expected[item["Id"]]
                require((image is None or image == item["Image"]) and state["OOMKilled"] is False
                        and state.get("Paused") is False and state.get("Dead") is False, "LEGACY_BLUE_DRIFT")
                require(state["Running"] is True and (state.get("Health") or {}).get("Status") == "healthy",
                        "LEGACY_BLUE_REBOOT_SERVICES_STARTING")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GateError("LEGACY_BLUE_DRIFT") from None

    def __call__(self, role, slot):
        try:
            if (role, slot) == ("api", "BLUE") and self.legacy_blue is not None:
                return self.classify_legacy_blue()[1]
            return self._probe(role, slot)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GateError("INVALID_SLOT_INSPECTION_EVIDENCE") from None

    def frontend_listener(self, slot, expected, *, initial_legacy=False):
        """Prove the same Caddy process owns a ready TCP path without a GET."""
        require(not initial_legacy or slot == "BLUE", "LEGACY_FRONTEND_SCOPE_INVALID")
        require(hex_digest(expected["container_id"]), "WRONG_CONTAINER")
        started = monotonic()
        self._run("exec", expected["container_id"], "sh", "-ec", """
            awk '$2 ~ /:0050$/ && $4 == "0A" { ready=1 } END { exit !ready }' /proc/1/net/tcp /proc/1/net/tcp6
            nc -z -w 1 127.0.0.1 80
            nc -z -w 1 127.0.0.1 80
        """, timeout=5)
        require(monotonic() - started <= 5, "FRONTEND_LISTENER_DEADLINE")
        observed = self.initial_legacy_frontend_identity() if initial_legacy else self("frontend", slot)
        require(observed == expected, "FRONTEND_IDENTITY_CHANGED_DURING_READINESS")


def sign_receipt(kind, payload, key):
    require(kind in ("READY", "BASELINE", "PLAYER", "PHASE3_BASELINE", "PHASE3_PLAYER", "PROMOTION_BASELINE_CLOCK")
            and isinstance(key, bytes) and len(key) >= 32,
            "INVALID_RECEIPT_KEY")
    record = {"kind": kind, "payload": payload}
    return {**record, "hmac_sha256": hmac.digest(key, canonical(record), "sha256").hex()}


def verified_payload(kind, receipt, key, context, keys):
    require(isinstance(receipt, dict) and set(receipt) == {"kind", "payload", "hmac_sha256"}
            and receipt["kind"] == kind and hex_digest(receipt["hmac_sha256"]), "INVALID_RECEIPT")
    payload = receipt["payload"]
    require(isinstance(payload, dict) and set(payload) == set(context) | keys
            and all(payload[field] == value for field, value in context.items()), "RECEIPT_CONTEXT_MISMATCH")
    expected = sign_receipt(kind, payload, key)["hmac_sha256"]
    require(hmac.compare_digest(receipt["hmac_sha256"], expected), "UNAUTHENTICATED_RECEIPT")
    return payload


PHASE3_PRODUCT_CONTRACTS = "UNCHANGED_PRODUCT_NOT_REQUALIFIED"
PHASE3_REAL_PLAYER_CONTINUITY_GATE = "REQUIRED_AFTER_HANDOFF"


class ReadyGate:
    def __init__(self, key, now=lambda: datetime.now(timezone.utc)):
        self.key = key
        self.now = now

    def __call__(self, receipt, context):
        try:
            data = verified_payload("READY", receipt, self.key, context,
                                    {"healthy_at", "ready_at", "private_route", "checks"})
            started, healthy, ready = (utc(data[field]) for field in ("started_at", "healthy_at", "ready_at"))
            require(started <= healthy <= ready <= self.now() and data["private_route"] is True,
                    "INVALID_READINESS_TIMELINE")
            checks = data["checks"]
            if context["role"] == "frontend":
                if "adoption" in context:
                    require(context["adoption"] == READY_ADOPTION and context.get("slot") == "BLUE",
                            "LEGACY_FRONTEND_SCOPE_INVALID")
                exact = {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}
                return canonical(checks) == canonical(exact)
            require(context["role"] == "api", "INVALID_READY_ROLE")
            exact = {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                     "runtime_serial_ok": 36, "runtime_concurrent_ok": 16, "runtime_clients": 4,
                     "runtime_transport_timeout_s": 10}
            if context.get("adoption") == "LEGACY_BLUE":
                require(context.get("slot") == "BLUE", "LEGACY_BLUE_SCOPE_INVALID")
                exact.update(phase3_infra_capacity=True, schema_unchanged=True, media_unchanged=True,
                             phase3_product_contracts=PHASE3_PRODUCT_CONTRACTS,
                             phase3_real_player_continuity_gate=PHASE3_REAL_PLAYER_CONTINUITY_GATE)
            else:
                exact.update(dual_slot_schema=True, media_cross_slot=True, dual_slot_capacity=True,
                             contracts={"windows_legacy": True, "android_normal": True, "android_node_slice": True})
            return (set(checks) == set(exact) | {"channels_elapsed_ns", "pool"}
                    and all(canonical(checks[field]) == canonical(value) for field, value in exact.items())
                    and number(checks["channels_elapsed_ns"], 1) and checks["channels_elapsed_ns"] <= 3_000_000_000
                    and checks["pool"] in ("BALANCED", "ISOLATED_NOT_APPLICABLE"))
        except (GateError, TypeError, ValueError, KeyError):
            return False


def device_ref(device_id, key):
    require(identifier(device_id), "INVALID_DEVICE_ID")
    return hmac.digest(key, b"device\0" + device_id.encode(), "sha256").hex()


def attribute_heartbeats(edge_acks, db_rows, key, generation, slot, upstream):
    """Join trusted GREEN ACK metadata to exactly one committed heartbeat.

    The observer supplies redacted EDGE routing/HTTP metadata and the two ACK
    fields returned by the actual upstream. Raw values exist only in memory.
    """
    matches = {}
    for row in db_rows:
        require(identifier(row["channel_id"]), "INVALID_HEARTBEAT_CHANNEL")
        match_key = (row["channel_id"], utc(row["received_at"]))
        matches.setdefault(match_key, []).append(row["player_device_id"])
    evidence = []
    used = set()
    for ack in edge_acks:
        require(ack["public"] is True and type(ack["generation"]) is int and ack["generation"] == generation
                and ack["slot"] == slot and ack["upstream"] == upstream and ack["status"] == 200
                and ack["path_class"] == "heartbeat" and identifier(ack["trace"])
                and identifier(ack["channel_id"]) and ack["trace"] not in used, "WRONG_HEARTBEAT_ORIGIN")
        received = utc(ack["received_at"])
        match_key = (ack["channel_id"], received)
        devices = matches.pop(match_key, [])
        require(len(devices) == 1, "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")
        used.add(ack["trace"])
        evidence.append({"device_ref": device_ref(devices[0], key), "received_at": received.isoformat(),
                         "trace_ref": hmac.digest(key, b"trace\0" + ack["trace"].encode(), "sha256").hex()})
    return evidence


class PlayerGate:
    def __init__(self, key, now=lambda: datetime.now(timezone.utc), *, require_monotonic=False):
        self.key = key
        self.now = now
        self.require_monotonic = require_monotonic

    @staticmethod
    def baseline_hash(data):
        refs = data["baseline_refs"]
        require(isinstance(refs, list) and all(hex_digest(ref) for ref in refs)
                and refs == sorted(set(refs)), "PLAYER_BASELINE_INVALID")
        return digest({"baseline_at": data["baseline_at"], "baseline_refs": refs})

    def capture_baseline(self, receipt, context):
        try:
            data = verified_payload("BASELINE", receipt, self.key, context, {"baseline_at", "baseline_refs"})
            timestamp, now = utc(data["baseline_at"]), self.now()
            require(now - timedelta(seconds=120) <= timestamp <= now, "PLAYER_BASELINE_STALE")
            return self.baseline_hash(data)
        except (GateError, TypeError, ValueError, KeyError):
            return None

    def baseline_clock(self, baseline, clock_receipt, *, expected_edge_config_revision=0, expected_edge_config_hash=None):
        require(number(expected_edge_config_revision), "PLAYER_PROMOTION_BASELINE_CONFIG_INVALID")
        context = {"baseline_sha256": digest(baseline)}
        revised = expected_edge_config_revision != 0 or expected_edge_config_hash is not None
        if revised:
            require(number(expected_edge_config_revision, 1) and hex_digest(expected_edge_config_hash),
                    "PLAYER_PROMOTION_BASELINE_CONFIG_INVALID")
            context.update(edge_config_revision=expected_edge_config_revision, edge_config_hash=expected_edge_config_hash)
        clock = verified_payload("PROMOTION_BASELINE_CLOCK", clock_receipt, self.key, context,
                                 {"baseline_monotonic_ns", "time_namespace", "collector_id", "collector_pid",
                                  "collector_started_at", "edge_id", "postgres_id"})
        require(number(clock["baseline_monotonic_ns"], 1) and number(clock["collector_pid"], 1)
                and type(clock["time_namespace"]) is str and re.fullmatch(r"time:\[[1-9][0-9]*\]", clock["time_namespace"])
                and all(hex_digest(clock[key]) for key in ("collector_id", "edge_id", "postgres_id"))
                and (not revised or number(clock["edge_config_revision"], 1))
                and baseline["payload"]["baseline_refs"], "PLAYER_PROMOTION_BASELINE_CLOCK_INVALID")
        utc(clock["collector_started_at"])
        return clock

    @staticmethod
    def _validate_monotonic_observation(observation, routing):
        expected = {"OBSERVER_MODE": "PROMOTION_MONOTONIC", "EXPECTED_GENERATION": routing["generation"],
                    "EXPECTED_SLOT": routing["slot"], "EXPECTED_UPSTREAM": routing["upstream"],
                    "EXPECTED_BACKEND_CONTAINER": routing["backend_container"],
                    "ROUTING_CONTEXT_SHA256": digest(routing), "TIME_NAMESPACE_MATCH": "YES"}
        if "edge_config_revision" in routing:
            revision = routing["edge_config_revision"]
            require(number(revision, 1) and isinstance(observation, dict)
                    and number(observation.get("EXPECTED_EDGE_CONFIG_REVISION"), 1),
                    "PLAYER_PROMOTION_OBSERVATION_CONTEXT_INVALID")
            expected["EXPECTED_EDGE_CONFIG_REVISION"] = revision
        window_keys = ("WINDOW_OPENED_MONOTONIC", "WINDOW_START_MONOTONIC", "WINDOW_FINISH_MONOTONIC",
                       "TERMINAL_DEADLINE_MONOTONIC")
        require(type(observation) is dict and set(observation) == set(expected) | set(window_keys) |
                {"TIME_NAMESPACE", "PLAYER_OBSERVED_COUNT", "PLAYER_MISSING_COUNT", "REQUEST_EVIDENCE_SHA256"}
                and type(observation["EXPECTED_GENERATION"]) is int
                and all(observation[key] == value for key, value in expected.items())
                and all(number(observation[key], 1) for key in window_keys)
                and hex_digest(observation["REQUEST_EVIDENCE_SHA256"])
                and type(observation["TIME_NAMESPACE"]) is str
                and re.fullmatch(r"time:\[[1-9][0-9]*\]", observation["TIME_NAMESPACE"]),
                "PLAYER_PROMOTION_OBSERVATION_CONTEXT_INVALID")
        opened, start, finish, deadline = (observation[key] for key in window_keys)
        require(opened <= start and finish - start >= 120_000_000_000 and finish <= deadline,
                "PLAYER_OBSERVATION_WINDOW_INVALID")

    def __call__(self, receipt, context, *, routing_context=None):
        try:
            monotonic = routing_context is not None
            require(monotonic or not self.require_monotonic, "PLAYER_PROMOTION_MONOTONIC_RECEIPT_REQUIRED")
            keys = {
                "baseline_at", "started_at", "finished_at", "baseline_refs", "heartbeats", "source",
                "access_complete", "http_409", "http_5xx", "incompatible", "playback_201", "playback_evidence",
            }
            if monotonic:
                keys.add("observation")
            data = verified_payload("PLAYER", receipt, self.key, context, keys)
            switched, baseline, started, finished = (utc(value) for value in (
                context["switched_at"], data["baseline_at"], data["started_at"], data["finished_at"]))
            require(switched - timedelta(seconds=120) <= baseline <= switched, "PLAYER_OBSERVATION_WINDOW_INVALID")
            if monotonic:
                self._validate_monotonic_observation(data["observation"], routing_context)
                require(data["baseline_refs"], "PLAYER_PROMOTION_EMPTY_BASELINE")
            else:
                require(switched <= started <= finished <= self.now() and finished - started >= timedelta(seconds=120),
                        "PLAYER_OBSERVATION_WINDOW_INVALID")
            require(data["source"] == "EDGE_ACK_EXACT_DB_JOIN" and data["access_complete"] is True
                    and all(number(data[key]) and data[key] == 0 for key in ("http_409", "http_5xx", "incompatible"))
                    and number(data["playback_201"])
                    and data["playback_evidence"] == ("PASS" if data["playback_201"] else "UNVERIFIED"),
                    "PLAYER_ACCESS_GATE_FAILED")
            require(self.baseline_hash(data) == context["baseline_hash"], "PLAYER_BASELINE_CHANGED")
            refs = data["baseline_refs"]
            observations = data["heartbeats"]
            require(isinstance(observations, list), "PLAYER_HEARTBEATS_INVALID")
            traces = set()
            observed = set()
            for entry in observations:
                require(set(entry) == {"device_ref", "trace_ref", "received_at"}
                        and hex_digest(entry["device_ref"]) and hex_digest(entry["trace_ref"])
                        and entry["trace_ref"] not in traces, "PLAYER_HEARTBEAT_INVALID")
                received = utc(entry["received_at"])
                require(monotonic or started <= received <= finished, "PLAYER_HEARTBEAT_INVALID")
                traces.add(entry["trace_ref"])
                observed.add(entry["device_ref"])
            if monotonic:
                count = len(set(refs) & observed)
                require(number(data["observation"]["PLAYER_OBSERVED_COUNT"])
                        and number(data["observation"]["PLAYER_MISSING_COUNT"])
                        and data["observation"]["PLAYER_OBSERVED_COUNT"] == count
                        and data["observation"]["PLAYER_MISSING_COUNT"] == len(refs) - count,
                        "PLAYER_PROMOTION_OBSERVATION_COUNT_INVALID")
            return set(refs) <= observed
        except (GateError, TypeError, ValueError, KeyError):
            return False
