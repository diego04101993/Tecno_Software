"""Manifest evidence for the one-time adoption of the exact historical R3 frontend."""

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
from html.parser import HTMLParser
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys

from docker_config import parse_optional_config
from docker_mounts import canonicalize_mounts
from docker_networks import canonicalize_network_attachment
from release_state import Stop, canonical, decode, digest, read_private, require, write_private_atomic


LEGACY_FRONTEND_IMAGE_EXACT = "LEGACY_FRONTEND_IMAGE_EXACT"
READY_ADOPTION = "INITIAL_LEGACY_FRONTEND_BLUE_ADOPTION"
SCOPE = {"release": "R3", "role": "INITIAL_FRONTEND_BLUE", "phase": "PHASE3_ADOPTION"}
HISTORICAL_CONFIG = "/etc/caddy/Caddyfile"
PRIVATE_CONFIG = "/etc/caddy/Caddyfile.bgv2"
HISTORICAL_COMMAND = ["caddy", "run", "--config", HISTORICAL_CONFIG, "--adapter", "caddyfile"]
PRIVATE_COMMAND = ["caddy", "run", "--config", PRIVATE_CONFIG, "--adapter", "caddyfile"]
PRIVATE_ALIAS = "frontend-blue"
PRIVATE_PORT = 80
PRIVATE_TMPFS = {"/config": "ro", "/data": "ro"}
REVISION = "org.opencontainers.image.revision"
TREE = "io.tecnocontrol.git.tree"
HEX = re.compile(r"[0-9a-f]{64}")
ASSET = re.compile(r"/assets/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+")
REPO = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
R3_IMAGE = "sha256:b2a1ae4a8515e9d1db4cb14744bba356311317c819a3a16b37c791d0257e905a"
R3_PROXY_ID = "0a7155a042aa45ea91ada4f3f39493cbe03ae65e8727ab7153bfe66426b90c88"
R3_PROXY_SUBNET = IPv4Network("172.30.250.0/24")
R3_RESERVED_IPS = {R3_PROXY_SUBNET.network_address, R3_PROXY_SUBNET.broadcast_address,
                   *(IPv4Address(f"172.30.250.{host}") for host in (1, 2, 3))}


@dataclass(frozen=True)
class LegacyFrontendPolicy:
    revision: str
    tree: str
    backend_image_id: str
    image_id: str
    repo_digests: tuple
    image_config_sha256: str
    historical_container_id: str
    historical_config_sha256: str
    historical_caddyfile_sha256: str
    index_sha256: str
    caddy_version: str
    historical_mounts_sha256: str
    historical_ports_sha256: str
    historical_networks: tuple
    private_network_name: str
    private_network_id: str
    slot_caddyfile_sha256: str
    rollback_source: str
    rollback_source_sha256: str
    rollback_reference: str
    historical_name: str = "tecnocontrol-frontend-1"
    historical_project: str = "tecnocontrol"
    historical_srv_tree_sha256: str | None = None

    @property
    def release(self):
        return {"revision": self.revision, "tree": self.tree,
                "images": {"api": self.backend_image_id, "frontend": self.image_id}}


R3_POLICY = LegacyFrontendPolicy(
    revision="0a7d5854bee17a64f6dba868dc44f2c0bbceed3d",
    tree="bd8d9a8969545cf98199579d0106d5982ff95453",
    backend_image_id="sha256:951cd0b55ce3b461c361cb9b9d83c29d08e32f938cff4eefbbfb842f58ab0832",
    image_id=R3_IMAGE,
    repo_digests=(f"tecnocontrol-frontend@{R3_IMAGE}", f"tecnocontrol/frontend@{R3_IMAGE}"),
    image_config_sha256="f25cde3c66575f939867daa24cef14fc485afcab79e6b5488001d1eb109d8eeb",
    historical_container_id="f9c354977c4fa52593044cc7904fe6760192b7235e3bbcbf1d67479162ed44d8",
    historical_config_sha256="f88d886132ec73965afdee7036ca7fcecc7e2c2376a4937c6e84128052571596",
    historical_caddyfile_sha256="de1843bb8670c5d1c2fe9f67dbd987f2c8ce908e37863e51b54d7c8c38df862a",
    index_sha256="86657705a1e9a96187955cff88b67b4cd339e3184cc8a68a78440cabd6b55388",
    caddy_version="v2.11.4 h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=",
    historical_mounts_sha256="12192366a334ec55abbcc04f577ac6ec0223c9f73ca42b1208277c63507e13e0",
    historical_ports_sha256="9e839fac501a46e385732bf7eb0d65ec71a47c815578503d3929425325e935b2",
    historical_networks=(
        ("tecnocontrol_edge", "937ccae5465b13a502ab6fe081778c7cc01abd67d43456810623df93e1ca9a99", "172.19.0.2"),
        ("tecnocontrol_proxy_net", R3_PROXY_ID, "172.30.250.2"),
    ),
    private_network_name="tecnocontrol_proxy_net",
    private_network_id=R3_PROXY_ID,
    slot_caddyfile_sha256="40cd7622b0df8fcc4bb29e908e96f0376c33446eb38e0fdf9bcb54531bd1be43",
    rollback_source="/root/tecnocontrol-stage3-controller-r2-5f8ed57-20261109T014723Z-kovkyhps/stage3.rollback-images.yml",
    rollback_source_sha256="700423bdb8651d05ee355c217c0c4e0fc7916adb2a362653974775b32bfde19a",
    rollback_reference=f"tecnocontrol/frontend@{R3_IMAGE}",
    historical_srv_tree_sha256="e7cadd1e11557627fca3123983608bfcc723020b370a7019f447b20eb930fb2a",
)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def hex_hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def is_image_id(value):
    return isinstance(value, str) and value.startswith("sha256:") and hex_hash(value[7:])


def utc_timestamp(value):
    return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() == timedelta(0)


def safe_asset_path(value):
    return isinstance(value, str) and ASSET.fullmatch(value) is not None and not any(
        segment in (".", "..") for segment in value.split("/")
    )


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value is None or key not in ("src", "href", "poster", "srcset", "imagesrcset", "data"):
                continue
            references = (part.split()[0] for part in value.split(",") if part.split()) if key.endswith("srcset") else (value,)
            for reference in references:
                # Fail closed for relative, escaped or external asset paths.
                if "assets/" in reference.lower():
                    require(safe_asset_path(reference), "LEGACY_FRONTEND_ASSET_PATH_INVALID")
                    self.paths.add(reference)


def asset_paths(index):
    require(type(index) is bytes and 0 < len(index) <= 1_048_576 and b"\0" not in index,
            "LEGACY_FRONTEND_INDEX_INVALID")
    parser = AssetReferences()
    parser.feed(index.decode("utf-8"))
    parser.close()
    require(0 < len(parser.paths) <= 128 and any(path.endswith(".js") for path in parser.paths),
            "LEGACY_FRONTEND_ASSETS_INVALID")
    return sorted(parser.paths)


def measure_static_content(read, container_id, *, private=False):
    """Hash every canonical asset referenced by the exact index via read-only exec."""
    require(hex_hash(container_id), "LEGACY_FRONTEND_CONTAINER_ID_INVALID")
    snapshot = read("exec", container_id, "sh", "-ec", """
        for file in /etc/caddy/Caddyfile /srv/index.html; do
            test -f "$file" && test "$(readlink -f "$file")" = "$file" || exit 1
        done
        caddy version
        sha256sum /etc/caddy/Caddyfile
        if [ "$1" = private ]; then
            file=/etc/caddy/Caddyfile.bgv2
            test -f "$file" && test "$(readlink -f "$file")" = "$file" || exit 1
            sha256sum "$file"
        fi
        test "$(wc -c </srv/index.html)" -le 1048576
        cat /srv/index.html
    """, "legacy-frontend-static", "private" if private else "historical")
    lines = snapshot.split(b"\n", 3 if private else 2)
    require(len(lines) == (4 if private else 3), "LEGACY_FRONTEND_STATIC_READ_INVALID")
    version, caddyfile = lines[0].decode(), lines[1].decode()
    historical = re.fullmatch(r"([0-9a-f]{64})  /etc/caddy/Caddyfile", caddyfile)
    require(historical is not None and re.fullmatch(r"v[0-9][^\r\n]*", version), "LEGACY_FRONTEND_CADDY_EVIDENCE_INVALID")
    index = lines[-1]
    paths = asset_paths(index)
    asset_files = ["/srv" + path for path in paths]
    checksums = read("exec", container_id, "sh", "-ec", """
        for file do
            test -s "$file" && test -f "$file" && test "$(readlink -f "$file")" = "$file" || exit 1
        done
        sha256sum "$@"
    """, "legacy-frontend-assets", *asset_files).decode().splitlines()
    require(len(checksums) == len(paths), "LEGACY_FRONTEND_ASSETS_MISSING")
    assets = {}
    for path, filename, row in zip(paths, asset_files, checksums):
        match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(filename), row)
        require(match is not None, "LEGACY_FRONTEND_ASSET_HASH_INVALID")
        assets[path] = match[1]
    result = {"caddy_version": version, "caddyfile_sha256": historical[1], "index_sha256": sha(index), "asset_sha256": assets}
    if private:
        active = re.fullmatch(r"([0-9a-f]{64})  /etc/caddy/Caddyfile.bgv2", lines[2].decode())
        require(active is not None, "LEGACY_FRONTEND_SLOT_CADDYFILE_HASH_INVALID")
        result["slot_caddyfile_sha256"] = active[1]
    return result


def historical_mounts(item):
    return canonicalize_mounts(item["Mounts"])


def historical_ports(item):
    return {key: sorted(rows, key=lambda row: (row["HostIp"], row["HostPort"]))
            for key, rows in item["NetworkSettings"]["Ports"].items() if rows}


def network_endpoints(networks):
    return tuple(sorted((name, row["NetworkID"], row["IPAddress"]) for name, row in networks.items()))


def label_evidence(labels, policy):
    expected = {REVISION: policy.revision, TREE: policy.tree}
    require(all(key not in labels or labels[key] == value for key, value in expected.items()),
            "LEGACY_FRONTEND_CONTRADICTORY_OCI_LABEL")
    return {key: labels[key] for key in expected if key in labels}


def image_evidence(image, policy):
    cfg = image.get("Config")
    optional = parse_optional_config(cfg)
    labels = label_evidence(optional["Labels"], policy)
    repos = image.get("RepoDigests")
    require(repos is None or type(repos) is list and all(isinstance(value, str) and REPO.fullmatch(value) for value in repos),
            "LEGACY_FRONTEND_REPODIGEST_INVALID")
    require(image["Id"] == policy.image_id and image["Os"] == "linux" and image["Architecture"] == "amd64"
            and tuple(sorted(repos or [])) == policy.repo_digests and digest(cfg) == policy.image_config_sha256
            and cfg.get("Entrypoint") is None and optional["Cmd"] == HISTORICAL_COMMAND,
            "LEGACY_FRONTEND_IMAGE_UNKNOWN_OR_DRIFTED")
    layers = image["RootFS"]["Layers"]
    require(image["RootFS"]["Type"] == "layers" and type(layers) is list and layers and all(is_image_id(layer) for layer in layers),
            "LEGACY_FRONTEND_LAYERS_INVALID")
    return {"id": image["Id"], "repo_digests": sorted(repos or []), "config_sha256": digest(cfg), "os": image["Os"],
            "architecture": image["Architecture"], "entrypoint": cfg.get("Entrypoint"), "cmd": optional["Cmd"],
            "rootfs_layers": layers, "oci_labels": labels}


def rollback_record(policy):
    return {"source": policy.rollback_source, "source_sha256": policy.rollback_source_sha256,
            "reference": policy.rollback_reference}


def historical_evidence(item, image, read, *, policy=R3_POLICY, read_host=None):
    """Project the running historical instance, before the historical frontend is stopped."""
    cfg, state = item["Config"], item["State"]
    optional = parse_optional_config(cfg)
    labels = optional["Labels"]
    oci = label_evidence(labels, policy)
    require(item["Id"] == policy.historical_container_id and item["Name"] == "/" + policy.historical_name
            and item["Image"] == policy.image_id and cfg["Image"] == policy.image_id
            and digest(cfg) == policy.historical_config_sha256
            and labels.get("com.docker.compose.project") == policy.historical_project
            and labels.get("com.docker.compose.service") == "frontend"
            and "io.tecnocontrol.bgv2.slot" not in labels and "io.tecnocontrol.bgv2.role" not in labels
            and state["Running"] is True and state["Status"] == "running" and type(state["Pid"]) is int and state["Pid"] > 0
            and all(state[key] is False for key in ("OOMKilled", "Dead", "Paused", "Restarting"))
            and type(item["RestartCount"]) is int and item["RestartCount"] == 0
            and utc_timestamp(item["Created"])
            and cfg.get("Entrypoint") is None and optional["Cmd"] == [item["Path"], *item["Args"]] == HISTORICAL_COMMAND,
            "LEGACY_FRONTEND_HISTORICAL_UNKNOWN_OR_DRIFTED")
    measured_image = image_evidence(image, policy)
    mounts, ports = historical_mounts(item), historical_ports(item)
    networks = item["NetworkSettings"]["Networks"]
    require(digest(mounts) == policy.historical_mounts_sha256 and digest(ports) == policy.historical_ports_sha256
            and network_endpoints(networks) == policy.historical_networks, "LEGACY_FRONTEND_HISTORICAL_CONTRACT_DRIFTED")
    proc = read("exec", item["Id"], "cat", "/proc/1/cmdline")
    require(proc == ("\0".join(HISTORICAL_COMMAND) + "\0").encode(), "LEGACY_FRONTEND_HISTORICAL_COMMAND_DRIFTED")
    rollback = rollback_record(policy)
    source = read_host(rollback["source"]) if read_host is not None else Path(rollback["source"]).read_bytes()
    require(type(source) is bytes and sha(source) == rollback["source_sha256"]
            and (policy.image_id.encode() in source or rollback["reference"].encode() in source),
            "LEGACY_FRONTEND_ROLLBACK_PROVENANCE_INVALID")
    content = measure_static_content(read, item["Id"])
    require(content["caddy_version"] == policy.caddy_version and content["caddyfile_sha256"] == policy.historical_caddyfile_sha256
            and content["index_sha256"] == policy.index_sha256, "LEGACY_FRONTEND_HISTORICAL_CONTENT_DRIFTED")
    return {"image": measured_image, "historical": {"container_id": item["Id"], "name": policy.historical_name,
            "compose_project": policy.historical_project, "compose_service": "frontend", "created_at": item["Created"],
            "container_config_sha256": digest(cfg), "host_config_sha256": digest(item["HostConfig"]), "oci_labels": oci,
            "mounts": mounts, "networks": networks, "ports": ports}, "content": content}


def validated_contract(contract, policy):
    fields = {"network_name", "network_id", "ipv4", "slot_caddyfile_sha256", "slot_caddyfile_source"}
    require(type(contract) is dict and set(contract) == fields, "LEGACY_FRONTEND_PRIVATE_CONTRACT_INVALID")
    source = contract["slot_caddyfile_source"]
    require(type(source) is str and source and not any(c in source for c in ("\0", "\r", "\n")),
            "LEGACY_FRONTEND_PRIVATE_SOURCE_INVALID")
    path = PurePosixPath(source)
    if not path.is_absolute() and policy != R3_POLICY:
        path = PureWindowsPath(source)
    require(path.is_absolute() and ".." not in path.parts and path.name == "Caddyfile.slot",
            "LEGACY_FRONTEND_PRIVATE_SOURCE_INVALID")
    address = IPv4Address(contract["ipv4"])
    require(str(address) == contract["ipv4"] and address.is_private and not address.is_unspecified
            and not address.is_loopback and not address.is_link_local
            and (policy != R3_POLICY or address in R3_PROXY_SUBNET and address not in R3_RESERVED_IPS)
            and address not in {IPv4Address(ip) for _, _, ip in policy.historical_networks}
            and contract["network_name"] == policy.private_network_name and contract["network_id"] == policy.private_network_id
            and contract["slot_caddyfile_sha256"] == policy.slot_caddyfile_sha256,
            "LEGACY_FRONTEND_PRIVATE_CONTRACT_INVALID")
    return {**contract, "alias": PRIVATE_ALIAS, "port": PRIVATE_PORT, "tls_public": False, "published_ports": [],
            "readonly_tmpfs": dict(PRIVATE_TMPFS), "mount_destination": PRIVATE_CONFIG}


def validate_payload(payload, policy):
    require(type(payload) is dict and set(payload) == {"version", "scope", "release", "image", "historical", "content",
                                                       "private_contract", "rollback"}
            and type(payload["version"]) is int and payload["version"] == 1 and payload["scope"] == SCOPE
            and payload["release"] == policy.release and payload["rollback"] == rollback_record(policy),
            "LEGACY_FRONTEND_MANIFEST_SCOPE_INVALID")
    image, old, content, contract = (payload[key] for key in ("image", "historical", "content", "private_contract"))
    require(type(image) is dict and set(image) == {"id", "repo_digests", "config_sha256", "os", "architecture", "entrypoint",
                                                  "cmd", "rootfs_layers", "oci_labels"}
            and image["id"] == policy.image_id and image["repo_digests"] == list(policy.repo_digests)
            and image["config_sha256"] == policy.image_config_sha256 and image["os"] == "linux" and image["architecture"] == "amd64"
            and image["entrypoint"] is None and image["cmd"] == HISTORICAL_COMMAND and type(image["rootfs_layers"]) is list
            and image["rootfs_layers"] and all(is_image_id(layer) for layer in image["rootfs_layers"]),
            "LEGACY_FRONTEND_MANIFEST_IMAGE_INVALID")
    require(type(old) is dict and set(old) == {"container_id", "name", "compose_project", "compose_service", "created_at",
            "container_config_sha256", "host_config_sha256", "oci_labels", "mounts", "networks", "ports"}
            and old["container_id"] == policy.historical_container_id and old["name"] == policy.historical_name
            and old["compose_project"] == policy.historical_project and old["compose_service"] == "frontend"
            and old["container_config_sha256"] == policy.historical_config_sha256 and hex_hash(old["host_config_sha256"])
            and utc_timestamp(old["created_at"])
            and digest(canonicalize_mounts(old["mounts"])) == policy.historical_mounts_sha256
            and digest(old["ports"]) == policy.historical_ports_sha256 and network_endpoints(old["networks"]) == policy.historical_networks,
            "LEGACY_FRONTEND_MANIFEST_HISTORICAL_INVALID")
    for row in (image, old):
        labels = row["oci_labels"]
        require(type(labels) is dict and label_evidence(labels, policy) == labels, "LEGACY_FRONTEND_MANIFEST_OCI_INVALID")
    require(type(content) is dict and set(content) == {"caddy_version", "caddyfile_sha256", "index_sha256", "asset_sha256"}
            and content["caddy_version"] == policy.caddy_version and content["caddyfile_sha256"] == policy.historical_caddyfile_sha256
            and content["index_sha256"] == policy.index_sha256 and type(content["asset_sha256"]) is dict
            and 0 < len(content["asset_sha256"]) <= 128
            and all(safe_asset_path(path) and hex_hash(checksum) for path, checksum in content["asset_sha256"].items())
            and any(path.endswith(".js") for path in content["asset_sha256"]), "LEGACY_FRONTEND_MANIFEST_CONTENT_INVALID")
    keys = ("network_name", "network_id", "ipv4", "slot_caddyfile_sha256", "slot_caddyfile_source")
    require(type(contract) is dict and validated_contract({key: contract[key] for key in keys}, policy) == contract,
            "LEGACY_FRONTEND_MANIFEST_PRIVATE_INVALID")


def seal_manifest(evidence, private_contract, *, policy=R3_POLICY):
    require(type(evidence) is dict and set(evidence) == {"image", "historical", "content"}, "LEGACY_FRONTEND_EVIDENCE_INVALID")
    payload = {"version": 1, "scope": dict(SCOPE), "release": policy.release, **deepcopy(evidence),
               "private_contract": validated_contract(private_contract, policy), "rollback": rollback_record(policy)}
    validate_payload(payload, policy)
    return {"sha256": digest(payload), "payload": payload}


def verify_manifest(manifest, expected_sha256, *, policy=R3_POLICY):
    try:
        require(type(manifest) is dict and set(manifest) == {"sha256", "payload"} and hex_hash(expected_sha256)
                and manifest["sha256"] == expected_sha256 and hmac.compare_digest(digest(manifest["payload"]), expected_sha256),
                "LEGACY_FRONTEND_MANIFEST_CHECKSUM")
        validate_payload(manifest["payload"], policy)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Stop("LEGACY_FRONTEND_MANIFEST_INVALID") from None
    return deepcopy(manifest["payload"])


def classify_historical(item, image, read, manifest, expected_sha256, *, policy=R3_POLICY, read_host=None,
                        stable_pin=None, compose_proof=None):
    payload = verify_manifest(manifest, expected_sha256, policy=policy)
    try:
        actual = historical_evidence(item, image, read, policy=policy, read_host=read_host)
        require((stable_pin is None) == (compose_proof is None), "LEGACY_FRONTEND_STABLE_APPROVAL_REQUIRED")
        if stable_pin is None:
            payload["historical"]["mounts"] = canonicalize_mounts(payload["historical"]["mounts"])
            require(all(actual[key] == payload[key] for key in ("image", "historical", "content")),
                    "LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED")
        else:
            attest_historical_stable(item, read, manifest, expected_sha256, actual, stable_pin=stable_pin,
                                     compose_proof=compose_proof, policy=policy)
    except (Stop, KeyError, TypeError, ValueError, OSError, AttributeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, Stop) and str(exc) == "DUPLICATE_MOUNT_DESTINATION":
            raise
        raise Stop("LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED") from None
    return LEGACY_FRONTEND_IMAGE_EXACT


def attest_historical_stable(item, read, manifest, expected_sha256, observed, *, stable_pin, compose_proof,
                             policy=R3_POLICY):
    from legacy_frontend_inventory import measure_srv_tree

    require(hex_hash(stable_pin) and type(compose_proof) is dict
            and compose_proof.get("container_create_hash") == item["Config"]["Labels"].get("com.docker.compose.config-hash")
            and compose_proof.get("container_declared_mac") == item["Config"].get("MacAddress")
            and compose_proof.get("container_network_mode") == item["HostConfig"]["NetworkMode"],
            "LEGACY_FRONTEND_RECONNECT_COMPOSE_CONTAINER_DRIFT")
    proof = verify_historical_stable_projection(manifest, expected_sha256, observed, compose_proof=compose_proof,
                                                observed_srv_tree_sha256=measure_srv_tree(item, read), policy=policy)
    require(hmac.compare_digest(proof["legacy_frontend_stable_pin"], stable_pin),
            "LEGACY_FRONTEND_STABLE_PIN_MISMATCH")
    return proof


def attest_reconnected_historical(item, image, read, manifest, expected_sha256, *, reconnect, policy=R3_POLICY,
                                  read_host=None):
    """Compare observed endpoints to the approved contract after a deliberate reconnect."""
    payload = verify_manifest(manifest, expected_sha256, policy=policy)
    actual = historical_evidence(item, image, read, policy=policy, read_host=read_host)
    expected = {key: payload[key] for key in ("historical", "image", "content")}
    return attest_reconnected_projection(expected, actual, reconnect=reconnect,
                                          configured_mac=item["Config"].get("MacAddress"))


def attest_reconnected_projection(expected, actual, *, reconnect, configured_mac=None):
    """Compare measured projections without modifying the sealed or observed data."""
    before, after = deepcopy(expected), deepcopy(actual)
    before["historical"]["mounts"] = canonicalize_mounts(before["historical"]["mounts"])
    after["historical"]["mounts"] = canonicalize_mounts(after["historical"]["mounts"])
    expected_networks = before["historical"].pop("networks")
    actual_networks = after["historical"].pop("networks")
    require(canonical(before) == canonical(after),
            "LEGACY_FRONTEND_RECONNECTED_NON_NETWORK_DRIFT")
    return attest_reconnected_networks(expected_networks, actual_networks, reconnect=reconnect,
                                        configured_mac=configured_mac)


def historical_reconnect_policy(expected, compose_proof, reconnected_ipv4):
    """Use the proven original Compose intent to qualify each deliberate reconnect."""
    require(type(compose_proof) is dict and compose_proof.get("compose_files_match_prestage") is True
            and compose_proof.get("compose_hash_matches_container") is True
            and hex_hash(compose_proof.get("container_create_hash"))
            and "container_declared_mac" in compose_proof
            and (compose_proof["container_declared_mac"] is None or type(compose_proof["container_declared_mac"]) is str)
            and type(compose_proof.get("container_network_mode")) is str and bool(compose_proof["container_network_mode"])
            and type(compose_proof.get("endpoints")) is list,
            "LEGACY_FRONTEND_RECONNECT_COMPOSE_UNPROVEN")
    fields = {"name", "service_mac", "endpoint_mac", "endpoint_driver_opts", "ipv4_address", "ipv6_address", "link_local_ips"}
    endpoints = compose_proof["endpoints"]
    require(all(type(row) is dict and set(row) == fields and type(row["name"]) is str for row in endpoints),
            "LEGACY_FRONTEND_RECONNECT_COMPOSE_ENDPOINT_INVALID")
    declarations = {row["name"]: row for row in endpoints}
    require(len(declarations) == len(endpoints) and set(declarations) == set(expected)
            and type(reconnected_ipv4) is dict and 0 < len(reconnected_ipv4) and set(reconnected_ipv4) <= set(expected),
            "LEGACY_FRONTEND_RECONNECT_COMPOSE_NETWORK_SET_DRIFT")
    rules = {}
    for name, requested in reconnected_ipv4.items():
        declaration = declarations[name]
        approved = expected[name]
        configured = approved.get("IPAMConfig") or {}
        link_local = declaration["link_local_ips"]
        require(all(declaration[field] is None or type(declaration[field]) is str
                    for field in ("service_mac", "endpoint_mac", "ipv4_address", "ipv6_address"))
                and (link_local is None or type(link_local) is list and all(type(ip) is str for ip in link_local)),
                "LEGACY_FRONTEND_RECONNECT_COMPOSE_ENDPOINT_INVALID")
        require(type(requested) is str and requested == approved["IPAddress"]
                and declaration["ipv4_address"] in (None, "", requested)
                and (declaration["ipv6_address"] or "") == (configured.get("IPv6Address") or "")
                and sorted(link_local or []) == sorted(configured.get("LinkLocalIPs") or []),
                "LEGACY_FRONTEND_RECONNECT_ADDRESS_INTENT_DRIFT")
        opts = declaration["endpoint_driver_opts"]
        require(opts is None or type(opts) is dict, "LEGACY_FRONTEND_RECONNECT_DRIVER_INTENT_INVALID")
        require(not opts or canonical(opts) == canonical(approved.get("DriverOpts")),
                "LEGACY_FRONTEND_RECONNECT_DRIVER_INTENT_DRIFT")
        macs = {value for value in (compose_proof["container_declared_mac"], declaration["service_mac"],
                                     declaration["endpoint_mac"]) if value not in (None, "")}
        require(not macs or macs == {approved.get("MacAddress")}, "LEGACY_FRONTEND_RECONNECT_MAC_INTENT_DRIFT")
        rules[name] = {"reconnected_ipv4": requested, "allow_generated_mac": not macs, "allow_empty_driver_opts": not opts}
    return rules


def require_historical_ownership(historical, membership, port_owner, tls_writer):
    """Reject a partial restored attachment or a conflicting ingress/storage owner."""
    require(type(membership) is dict and set(membership) == set(historical["networks"])
            and all(type(count) is int and count == 1 for count in membership.values()),
            "LEGACY_FRONTEND_RECONNECTED_MEMBERSHIP_DRIFT")
    require(port_owner == historical["name"], "LEGACY_FRONTEND_RECONNECTED_PORT_OWNER_DRIFT")
    require(tls_writer == historical["name"], "LEGACY_FRONTEND_RECONNECTED_TLS_WRITER_DRIFT")


def attest_reconnected_networks(expected, actual, *, reconnect, configured_mac=None):
    """Return a stable attachment hash and both original endpoint observations."""
    require(type(expected) is dict and type(actual) is dict and set(expected) == set(actual)
            and type(reconnect) is dict and 0 < len(reconnect) and set(reconnect) <= set(expected),
            "LEGACY_FRONTEND_RECONNECTED_NETWORK_SET_DRIFT")
    stable = {}
    volatile = {}
    for name, observed in actual.items():
        approved = expected[name]
        if name not in reconnect:
            require(canonical(observed) == canonical(approved), "LEGACY_FRONTEND_UNCHANGED_ENDPOINT_DRIFT")
            stable[name] = deepcopy(approved)
            continue
        rule = reconnect[name]
        require(type(rule) is dict and set(rule) == {"allow_generated_mac", "allow_empty_driver_opts", "reconnected_ipv4"}
                and type(rule["allow_generated_mac"]) is bool and type(rule["allow_empty_driver_opts"]) is bool
                and type(rule["reconnected_ipv4"]) is str and rule["reconnected_ipv4"] == approved["IPAddress"],
                "LEGACY_FRONTEND_RECONNECT_POLICY_INVALID")
        require(not rule["allow_generated_mac"] or configured_mac in (None, ""),
                "LEGACY_FRONTEND_RECONNECT_CONFIGURED_MAC")
        before, volatile_before = canonicalize_network_attachment(name, approved, **rule)
        after, volatile_after = canonicalize_network_attachment(name, observed, **rule)
        require(canonical(before) == canonical(after), "LEGACY_FRONTEND_RECONNECTED_NETWORK_DRIFT")
        stable[name] = after
        volatile[name] = {"approved": volatile_before, "observed": volatile_after}
    return {"stable_network_contract": stable, "stable_network_sha256": digest(stable),
            "raw_expected_network_settings": deepcopy(expected), "raw_observed_network_settings": deepcopy(actual),
            "volatile_endpoint_evidence": volatile}


def verify_historical_stable_projection(manifest, expected_sha256, observed, *, compose_proof,
                                        observed_srv_tree_sha256, policy=R3_POLICY):
    """Attest current R3 against the sealed pre-reconnect manifest and full static inventory."""
    payload = verify_manifest(manifest, expected_sha256, policy=policy)
    fields = ("historical", "image", "content")
    require(type(observed) is dict and set(observed) == set(fields), "LEGACY_FRONTEND_EVIDENCE_INVALID")
    require(hex_hash(policy.historical_srv_tree_sha256) and hex_hash(observed_srv_tree_sha256)
            and hmac.compare_digest(observed_srv_tree_sha256, policy.historical_srv_tree_sha256),
            "LEGACY_FRONTEND_SRV_TREE_DRIFT")
    approved = {key: payload[key] for key in fields}
    historical = approved["historical"]
    networks = historical["networks"]
    reconnect = historical_reconnect_policy(networks, compose_proof, {name: row["IPAddress"] for name, row in networks.items()})
    attachment = attest_reconnected_projection(approved, observed, reconnect=reconnect,
                                               configured_mac=compose_proof["container_declared_mac"])
    historical["networks"] = attachment["stable_network_contract"]
    historical["mounts"] = canonicalize_mounts(historical["mounts"])
    tls, = [mount for mount in historical["mounts"] if mount["Destination"] == "/data"]
    require(tls["Type"] == "volume" and type(tls["Name"]) is str and bool(tls["Name"]) and tls["RW"] is True,
            "LEGACY_FRONTEND_RECONNECTED_TLS_MOUNT_UNPROVEN")
    stable = {**approved, "srv_tree_sha256": observed_srv_tree_sha256, "scope": payload["scope"],
              "release": payload["release"], "tls_role": {"writer": historical["name"], "mount": "/data"}}
    return {"classification": LEGACY_FRONTEND_IMAGE_EXACT,
            "legacy_frontend_stable_pin": digest(stable),
            "legacy_frontend_evidence_sha256": digest({"projection": observed, "srv_tree_sha256": observed_srv_tree_sha256}),
            "stable_projection": stable,
            "volatile_endpoint_evidence": attachment["volatile_endpoint_evidence"]}


def verify_private_frontend(item, image, network, read, payload, binding, *, policy=R3_POLICY, verify_srv_tree=False):
    """Check the private replacement against the historically qualified bytes and contract."""
    try:
        validate_payload(payload, policy)
        contract = payload["private_contract"]
        cfg, host = item["Config"], item["HostConfig"]
        optional = parse_optional_config(cfg)
        labels = optional["Labels"]
        label_evidence(labels, policy)
        require(item["Image"] == cfg["Image"] == policy.image_id and hex_hash(item["Id"])
                and item["Id"] != payload["historical"]["container_id"] and item["Name"] == "/" + binding["name"]
                and binding["image_id"] == policy.image_id and binding["revision"] == policy.revision and binding["tree"] == policy.tree
                and labels.get("io.tecnocontrol.bgv2.slot") == "BLUE" and labels.get("io.tecnocontrol.bgv2.role") == "frontend"
                and cfg.get("Entrypoint") is None and optional["Cmd"] == [item["Path"], *item["Args"]] == PRIVATE_COMMAND
                and image_evidence(image, policy) == payload["image"], "LEGACY_FRONTEND_PRIVATE_IMAGE_DRIFTED")
        mount_rows = canonicalize_mounts(item["Mounts"])
        binds = [row for row in mount_rows if row.get("Destination") == PRIVATE_CONFIG]
        require(len(binds) == 1 and binds[0].get("Type") == "bind" and binds[0].get("RW") is False
                and binds[0].get("Source") == contract["slot_caddyfile_source"] and host.get("Tmpfs") == PRIVATE_TMPFS
                and all(row is binds[0] or row.get("Destination") in PRIVATE_TMPFS and row.get("Type") == "tmpfs"
                        and row.get("RW") is False for row in mount_rows)
                and not host.get("PortBindings") and host["PublishAllPorts"] is False
                and not any(item["NetworkSettings"]["Ports"].values()), "LEGACY_FRONTEND_PRIVATE_MOUNT_OR_PORT_DRIFTED")
        name = contract["network_name"]
        networks = item["NetworkSettings"]["Networks"]
        require(set(networks) == {name} and binding["network"] == name and binding["ip"] == contract["ipv4"]
                and binding["upstream"] == f"{PRIVATE_ALIAS}:{PRIVATE_PORT}"
                and binding["frontend_config_sha256"] == contract["slot_caddyfile_sha256"]
                and network["Name"] == name and network["Id"] == contract["network_id"] and network["Internal"] is True
                and network["Driver"] == "bridge" and networks[name]["NetworkID"] == network["Id"]
                and networks[name]["IPAddress"] == contract["ipv4"] and PRIVATE_ALIAS in (networks[name]["Aliases"] or []),
                "LEGACY_FRONTEND_PRIVATE_NETWORK_DRIFTED")
        content = measure_static_content(read, item["Id"], private=True)
        active = content.pop("slot_caddyfile_sha256")
        require(content == payload["content"] and active == contract["slot_caddyfile_sha256"],
                "LEGACY_FRONTEND_PRIVATE_CONTENT_DRIFTED")
        if verify_srv_tree:
            from legacy_frontend_inventory import measure_srv_tree

            require(hex_hash(policy.historical_srv_tree_sha256)
                    and measure_srv_tree(item, read) == policy.historical_srv_tree_sha256,
                    "LEGACY_FRONTEND_PRIVATE_SRV_TREE_DRIFT")
    except (Stop, KeyError, TypeError, ValueError, OSError, AttributeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, Stop) and str(exc) == "DUPLICATE_MOUNT_DESTINATION":
            raise
        raise Stop("LEGACY_FRONTEND_UNKNOWN_OR_DRIFTED") from None
    return {"classification": LEGACY_FRONTEND_IMAGE_EXACT, "container_id": item["Id"], "image_id": image["Id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    seal = actions.add_parser("seal")
    for option in ("evidence", "private-contract", "manifest"):
        seal.add_argument("--" + option, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = decode(read_private(args.evidence))
        contract = decode(read_private(args.private_contract))
        manifest = seal_manifest(evidence, contract)
        data = canonical(manifest) + b"\n"
        write_private_atomic(args.manifest, data, replace=False)
    except (Stop, KeyError, OSError, TypeError, ValueError, AttributeError):
        print("STOP: LEGACY_FRONTEND_MANIFEST_SEAL_FAILED", file=sys.stderr)
        return 1
    print(f"LEGACY_FRONTEND_MANIFEST_PAYLOAD_SHA256={manifest['sha256']}")
    print(f"LEGACY_FRONTEND_MANIFEST_FILE_SHA256={sha(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
