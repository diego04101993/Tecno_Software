"""Replay the preserved R3 rollback and the same restored epoch, entirely offline."""

import argparse
import json
from pathlib import Path
import sys

from qa_docker import DEPLOY_DIRECTORY

sys.path.insert(0, str(DEPLOY_DIRECTORY))

from docker_networks import VOLATILE_ENDPOINT_FIELDS
from legacy_frontend_blue import (
    attest_reconnected_projection, historical_reconnect_policy, require_historical_ownership, sha, verify_manifest,
)
from release_state import canonical, decode, require


ARCHIVE_SHA256 = "0209e0f9513fd63f6689235fca982fe7fbc6a5572a66c0a697bc5c9498e22204"
PAYLOAD_SHA256 = "8e98a306fa07c09802a13b703171d6046c7cb22422ae988ca9a885cee897282f"
ROLLBACK_SHA256 = "c8e8a25e8157a7fdc2c3b4c963ef8a0a9b243649960b2a53312ace62d550dac0"
RESTORED_SHA256 = "9861e32762c8965681f5d769f32c5fb36059b65f7d337d14818a9340c4f936ba"


def approved(path, checksum):
    raw = path.read_bytes()
    require(sha(raw) == checksum, "ROLLBACK_REPLAY_ARCHIVE_BYTES_CHANGED")
    return decode(raw)


def replay(manifest_path, rollback_path, restored_path):
    manifest = verify_manifest(approved(manifest_path, ARCHIVE_SHA256), PAYLOAD_SHA256)
    rollback = approved(rollback_path, ROLLBACK_SHA256)
    restored = approved(restored_path, RESTORED_SHA256)
    before, after = restored["historical_before"], restored["historical_after"]
    require(canonical(before) == canonical({key: manifest[key] for key in ("historical", "image", "content")}),
            "ROLLBACK_REPLAY_EXPECTED_PROJECTION_CHANGED")
    historical, roles = after["historical"], restored["live_roles"]
    old = rollback["containers"][historical["name"]]
    backend = rollback["containers"]["tecnocontrol-backend-1"]
    require(old["id"] == historical["container_id"] == roles["historical_container"]
            and old["image"] == after["image"]["id"] and old["config_hash"] == historical["container_config_sha256"]
            and old["host_config_hash"] == historical["host_config_sha256"]
            and old["status"] == "running" and old["restart_count"] == 0
            and old["pid"] == roles["historical_pid"] and old["started_at"] == roles["historical_started_at"]
            and backend["id"] == roles["backend_container"] and backend["pid"] == roles["backend_pid"]
            and backend["started_at"] == roles["backend_started_at"] and backend["health"] == "healthy",
            "ROLLBACK_REPLAY_RESTORED_EPOCH_CHANGED")
    observed = historical["networks"]
    require(set(old["networks"]) == set(observed)
            and all(canonical(row) == canonical({key: observed[name][key] for key in row})
                    for name, row in old["networks"].items()), "ROLLBACK_REPLAY_NETWORK_EVIDENCE_CHANGED")
    planned = {name: row["IPAddress"] for name, row in before["historical"]["networks"].items()}
    proof = restored["compose_proof"]
    reconnect = historical_reconnect_policy(before["historical"]["networks"], proof, planned)
    attestation = attest_reconnected_projection(before, after, reconnect=reconnect,
                                               configured_mac=proof["container_declared_mac"])
    require_historical_ownership(historical, restored["network_membership"], roles["port_owner"], roles["tls_writer"])
    require(roles["proxy_owner"] == historical["name"]
            and observed["tecnocontrol_proxy_net"]["IPAddress"] == "172.30.250.2"
            and roles["edge_stopped"] is True and roles["edge_ingress_ready"] is False
            and all(roles["public_smokes"][host] == {"status": 200, "historical_certificate_exact": True}
                    for host in ("api.tecnocontrolcloud.com", "app.tecnocontrolcloud.com")),
            "ROLLBACK_REPLAY_PUBLIC_OR_PROXY_DRIFT")
    changes = {field for row in attestation["volatile_endpoint_evidence"].values() for field in VOLATILE_ENDPOINT_FIELDS
               if canonical(row["approved"].get(field)) != canonical(row["observed"].get(field))}
    require(changes == set(VOLATILE_ENDPOINT_FIELDS), "ROLLBACK_REPLAY_OBSERVED_CHANGES_CHANGED")
    return {"result": "PASS", "scope": "SEALED_R3_ROLLBACK_TOPOLOGY_AND_RESTORED_EPOCH_REPLAY",
            "rollback_topology_attestation": "PASS", "stable_network_sha256": attestation["stable_network_sha256"],
            "volatile_fields_changed": sorted(changes), "semantic_network_contract_changed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "rollback", "restored"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.manifest, args.rollback, args.restored), sort_keys=True))
