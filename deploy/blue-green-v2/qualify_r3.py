"""Read-only qualification of the two exact R3 images for Phase 3."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from gates import BACKEND_ARGV, REVISION_LABEL, TREE_LABEL, hex_digest, utc
from release_state import Stop, decode, digest, read_private, require, validate_release


R3_IMAGES = {
    "api": "sha256:951cd0b55ce3b461c361cb9b9d83c29d08e32f938cff4eefbbfb842f58ab0832",
    "frontend": "sha256:b2a1ae4a8515e9d1db4cb14744bba356311317c819a3a16b37c791d0257e905a",
}
R3_CADDY_ARGV = ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
R3_CADDYFILE_SHA256 = "de1843bb8670c5d1c2fe9f67dbd987f2c8ce908e37863e51b54d7c8c38df862a"
REPO_DIGEST = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")


def is_image_digest(value):
    return isinstance(value, str) and value.startswith("sha256:") and hex_digest(value[7:])


def runtime_snapshot(container):
    state = container["State"]
    require(state["Running"] is True and state["Status"] == "running" and state["OOMKilled"] is False
            and all(state[field] is False for field in ("Dead", "Paused", "Restarting"))
            and type(state["Pid"]) is int and state["Pid"] > 0 and type(container["RestartCount"]) is int
            and container["RestartCount"] >= 0, "R3_RUNTIME_UNAVAILABLE")
    utc(container["Created"])
    utc(state["StartedAt"])
    require(utc(container["Created"]) <= utc(state["StartedAt"]), "R3_RUNTIME_TIME_INVALID")
    return {"container_id": container["Id"], "created": container["Created"], "pid": state["Pid"],
            "started_at": state["StartedAt"], "restart_count": container["RestartCount"],
            "oom_killed": state["OOMKilled"], "health": (state.get("Health") or {}).get("Status"),
            "container_config_sha256": digest(container["Config"]), "host_config_sha256": digest(container["HostConfig"]),
            "mounts_sha256": digest(container["Mounts"]),
            "networks_sha256": digest(container["NetworkSettings"]["Networks"])}


def qualify_role(role, release, expected_container, before, after, image, raw_argv, caddyfile=None):
    service = {"api": "backend", "frontend": "frontend"}[role]
    approved_image = release["images"][role]
    require(hex_digest(expected_container) and before["Id"] == after["Id"] == expected_container
            and after["Name"] == "/tecnocontrol-" + service + "-1", "R3_CONTAINER_IDENTITY_MISMATCH")
    require(before["Image"] == after["Image"] == image["Id"] == approved_image,
            "R3_IMAGE_DIGEST_MISMATCH")
    require(image["Os"] == "linux" and image["Architecture"] == "amd64", "R3_IMAGE_PLATFORM_MISMATCH")
    require(before["Path"] == after["Path"] and before["Args"] == after["Args"], "R3_RUNTIME_COMMAND_CHANGED")
    runtime = runtime_snapshot(after)
    require(runtime == runtime_snapshot(before) and (role != "api" or runtime["health"] == "healthy"),
            "R3_RUNTIME_IDENTITY_CHANGED_OR_UNHEALTHY")

    config = image["Config"]
    container_config = after["Config"]
    labels = config.get("Labels") or {}
    container_labels = container_config.get("Labels") or {}
    require(container_labels.get("com.docker.compose.project") == "tecnocontrol"
            and container_labels.get("com.docker.compose.service") == service, "R3_COMPOSE_IDENTITY_MISMATCH")
    for key, field in ((REVISION_LABEL, "revision"), (TREE_LABEL, "tree")):
        require(labels.get(key) == container_labels.get(key)
                and (labels.get(key) == release[field] or role == "api" and key not in labels and key not in container_labels),
                "R3_OCI_PROVENANCE_MISMATCH")

    image_reference = container_config["Image"]
    require(isinstance(image_reference, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image_reference)
            and before["Config"]["Image"] == image_reference, "R3_CONTAINER_IMAGE_REFERENCE_INVALID")
    repo_digests = image.get("RepoDigests")
    if repo_digests is None:
        repo_digests = []
    require(type(repo_digests) is list and all(isinstance(entry, str) and REPO_DIGEST.fullmatch(entry)
            for entry in repo_digests) and len(repo_digests) == len(set(repo_digests)), "R3_REPODIGEST_INVALID")
    rootfs = image["RootFS"]
    require(rootfs["Type"] == "layers" and type(rootfs["Layers"]) is list and rootfs["Layers"]
            and all(is_image_digest(layer) for layer in rootfs["Layers"]), "R3_ROOTFS_INVALID")

    command = BACKEND_ARGV if role == "api" else R3_CADDY_ARGV
    image_commands = [command]
    if role == "api":
        image_commands.extend([command[:-2], [command[0], "app.main", *command[2:-2]]])
    require(config.get("Entrypoint") in (None, []) and config["Cmd"] in image_commands
            and container_config.get("Entrypoint") in (None, []) and container_config["Cmd"] == command
            and [after["Path"], *after["Args"]] == command, "R3_COMMAND_MISMATCH")
    if role == "api" and raw_argv[:2] in (["/usr/local/bin/python", "/usr/local/bin/uvicorn"],
                                         ["/usr/local/bin/python3.12", "/usr/local/bin/uvicorn"]):
        raw_argv = ["uvicorn", *raw_argv[2:]]
    require(raw_argv == command, "R3_LIVE_COMMAND_MISMATCH")
    if role == "frontend":
        require(hashlib.sha256(caddyfile).hexdigest() == R3_CADDYFILE_SHA256, "R3_CADDYFILE_MISMATCH")
        runtime["historical_caddyfile_sha256"] = R3_CADDYFILE_SHA256
    return {"image_id": image["Id"], "os": image["Os"], "architecture": image["Architecture"],
            "repo_digests": sorted(repo_digests), "container_image_reference": image_reference,
            "image_oci_revision": labels.get(REVISION_LABEL), "image_oci_tree": labels.get(TREE_LABEL),
            "image_labels_sha256": digest(labels), "image_config_sha256": digest(config),
            "entrypoint": config.get("Entrypoint"), "cmd": config["Cmd"], "rootfs_layers": rootfs["Layers"],
            "runtime_command": command, "compose_project": "tecnocontrol", "compose_service": service,
            "runtime": runtime}


def one(result):
    require(type(result) is list and len(result) == 1 and type(result[0]) is dict, "R3_INSPECT_RESULT_INVALID")
    return result[0]


def qualify(release, container_ids, read):
    validate_release(release)
    require(release["images"] == R3_IMAGES, "R3_RELEASE_IMAGES_NOT_APPROVED")
    require(set(container_ids) == set(R3_IMAGES) and all(hex_digest(value) for value in container_ids.values())
            and len(set(container_ids.values())) == len(R3_IMAGES), "R3_CONTAINER_IDENTITY_MISMATCH")
    result = {}
    for role, identifier in container_ids.items():
        before = one(decode(read("container", "inspect", identifier)))
        require(before.get("Id") == identifier and before.get("Image") == R3_IMAGES[role],
                "R3_CONTAINER_IMAGE_IDENTITY_MISMATCH")
        image = one(decode(read("image", "inspect", R3_IMAGES[role])))
        raw = read("exec", identifier, "cat", "/proc/1/cmdline")
        require(raw.endswith(b"\0"), "R3_LIVE_COMMAND_INVALID")
        argv = raw[:-1].decode().split("\0")
        caddyfile = read("exec", identifier, "cat", "/etc/caddy/Caddyfile") if role == "frontend" else None
        after = one(decode(read("container", "inspect", identifier)))
        result[role] = qualify_role(role, release, identifier, before, after, image, argv, caddyfile)
    return {"scope": "R3_EXACT_IMAGE_QUALIFICATION", "status": "PASS", "release": release, "roles": result}


def docker_read(*args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
    result = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *args], env=env, input=input,
                            capture_output=True, timeout=15, check=False)
    require(result.returncode == 0, "R3_READ_ONLY_INSPECT_FAILED")
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-release", required=True, type=Path)
    parser.add_argument("--backend-id", required=True)
    parser.add_argument("--frontend-id", required=True)
    args = parser.parse_args(argv)
    try:
        require(sys.platform.startswith("linux"), "R3_QUALIFICATION_REQUIRES_LOCAL_LINUX")
        result = qualify(decode(read_private(args.approved_release)),
                         {"api": args.backend_id, "frontend": args.frontend_id}, docker_read)
    except (Stop, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, Stop) else "R3_QUALIFICATION_INPUT_OR_INSPECTION_INVALID", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
