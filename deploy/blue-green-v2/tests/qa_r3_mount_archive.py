"""Replay both R3 mount orders against the independently sealed archive offline."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from qa_docker import DEPLOY_DIRECTORY, PROJECT_DIRECTORY

sys.path.insert(0, str(DEPLOY_DIRECTORY))

import legacy_frontend_blue as legacy
from qualify_r3 import qualify_role
from release_state import canonical, decode, digest, require


ARCHIVE_SHA256 = "0209e0f9513fd63f6689235fca982fe7fbc6a5572a66c0a697bc5c9498e22204"
PAYLOAD_SHA256 = "8e98a306fa07c09802a13b703171d6046c7cb22422ae988ca9a885cee897282f"
EXPECTED_RAW_SHA256 = {
    "12192366a334ec55abbcc04f577ac6ec0223c9f73ca42b1208277c63507e13e0",
    "0e17ef01b3daff9e0cb1d276850fa4037cd7c16f7313c995d29cc70f01f71978",
}


def replay(data):
    require(legacy.sha(data) == ARCHIVE_SHA256, "R3_ARCHIVE_BYTES_CHANGED")
    manifest = decode(data)
    payload = legacy.verify_manifest(manifest, PAYLOAD_SHA256)
    evidence = payload["historical"]
    orders = [evidence["mounts"], evidence["mounts"][::-1]]
    require({digest(order) for order in orders} == EXPECTED_RAW_SHA256, "R3_ARCHIVE_MOUNT_VARIANTS_CHANGED")

    command = legacy.HISTORICAL_COMMAND
    image_projection = payload["image"]
    image_config = {"Cmd": command}
    image = {"Id": image_projection["id"], "Config": image_config, "Os": image_projection["os"],
             "Architecture": image_projection["architecture"], "RepoDigests": image_projection["repo_digests"],
             "RootFS": {"Type": "layers", "Layers": image_projection["rootfs_layers"]}}
    container_config = {**image_config, "Image": image["Id"],
                        "Labels": {**evidence["oci_labels"], "com.docker.compose.project": evidence["compose_project"],
                                   "com.docker.compose.service": evidence["compose_service"]}}
    host_config = {}
    container = {"Id": evidence["container_id"], "Name": "/" + evidence["name"], "Image": image["Id"],
                 "Config": container_config, "HostConfig": host_config, "Path": command[0], "Args": command[1:],
                 "Created": evidence["created_at"], "RestartCount": 0,
                 "NetworkSettings": {"Ports": evidence["ports"], "Networks": evidence["networks"]},
                 "State": {"Running": True, "Status": "running", "OOMKilled": False, "Dead": False,
                           "Paused": False, "Restarting": False, "Pid": 123, "StartedAt": evidence["created_at"]}}
    contract = payload["private_contract"]
    proxy = {"Name": contract["network_name"], "Id": contract["network_id"], "Driver": "bridge", "Internal": True,
             "IPAM": {"Config": [{"Subnet": str(legacy.R3_PROXY_SUBNET), "Gateway": "172.30.250.1"}]}, "Containers": {}}
    caddyfile = subprocess.check_output(
        ["git", "show", payload["release"]["revision"] + ":frontend/Caddyfile.production"], cwd=PROJECT_DIRECTORY)

    # The archive seals these measurements without exposing the underlying live config or media bytes.
    # Keep mount hashes, manifest verification and both identity comparisons real during the replay.
    def captured_digest(value):
        if value is container_config:
            return evidence["container_config_sha256"]
        if value is host_config:
            return evidence["host_config_sha256"]
        return digest(value)

    rollback = payload["rollback"]["reference"].encode()

    def read_rollback(path):
        require(path == payload["rollback"]["source"], "R3_ARCHIVE_UNEXPECTED_ROLLBACK_PATH")
        return SimpleNamespace(read_bytes=lambda: rollback)

    def qualify(before, after):
        def read(*args):
            if args == ("network", "inspect", contract["network_name"]):
                return canonical([proxy])
            if args == ("container", "inspect", container["Id"]):
                return canonical([before])
            require(args == ("exec", container["Id"], "cat", "/proc/1/cmdline"), "R3_ARCHIVE_UNEXPECTED_READ")
            return ("\0".join(command) + "\0").encode()

        return qualify_role("frontend", payload["release"], container["Id"], before, after, image, command, caddyfile,
                            legacy_frontend_manifest=manifest, legacy_frontend_sha256=PAYLOAD_SHA256, read=read)

    variants = [{**container, "Mounts": order} for order in orders]
    with (patch.object(legacy, "image_evidence", return_value=image_projection),
          patch.object(legacy, "measure_static_content", side_effect=lambda *args: deepcopy(payload["content"])),
          patch.object(legacy, "digest", side_effect=captured_digest),
          patch.object(legacy, "Path", side_effect=read_rollback),
          patch.object(legacy, "sha", side_effect=lambda value: payload["rollback"]["source_sha256"]
                       if value is rollback else legacy.hashlib.sha256(value).hexdigest())):
        first = qualify(*variants)
        second = qualify(*variants[::-1])

    first_mounts = first.pop("mount_evidence")
    second_mounts = second.pop("mount_evidence")
    require(first == second and first["frontend_classifier"] == legacy.LEGACY_FRONTEND_IMAGE_EXACT,
            "R3_ARCHIVE_QUALIFICATION_CHANGED")
    require(first_mounts["before"]["raw_mounts"] == second_mounts["after"]["raw_mounts"] == orders[0]
            and first_mounts["after"]["raw_mounts"] == second_mounts["before"]["raw_mounts"] == orders[1],
            "R3_ARCHIVE_RAW_MOUNT_EVIDENCE_CHANGED")
    return {"result": "PASS", "scope": "SEALED_R3_OFFLINE_MOUNT_REPLAY",
            "classification": first["frontend_classifier"], "payload_sha256": PAYLOAD_SHA256,
            "raw_orders": [[row["Destination"] for row in order] for order in orders],
            "canonical_mounts_sha256": first["runtime"]["mounts_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    print(json.dumps(replay(parser.parse_args().manifest.read_bytes()), sort_keys=True))
