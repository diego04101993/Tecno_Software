"""Preserve Docker mount objects while ignoring their collection order."""

from copy import deepcopy
import json

from release_state import canonical, decode, digest, require


def canonicalize_mounts(mounts):
    require(type(mounts) is list, "INVALID_DOCKER_MOUNTS")
    rows = []
    destinations = set()
    for mount in mounts:
        require(type(mount) is dict, "INVALID_DOCKER_MOUNT")
        destination = mount.get("Destination")
        require(type(destination) is str and destination, "INVALID_DOCKER_MOUNT_DESTINATION")
        require(destination not in destinations, "DUPLICATE_MOUNT_DESTINATION")
        destinations.add(destination)
        rows.append((destination, canonical(mount)))
    return [decode(encoded) for _, encoded in sorted(rows)]


def mount_evidence(mounts):
    normalized = canonicalize_mounts(mounts)
    return {
        "raw_mounts": deepcopy(mounts),
        "canonical_mounts": normalized,
        "canonical_mounts_sha256": digest(normalized),
    }


def encode_mount_evidence(evidence):
    return (json.dumps(evidence, separators=(",", ":"), allow_nan=False) + "\n").encode()
