"""Verify the stable attachment contract across 100 owned Docker reconnects."""

import json
import sys

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, LocalDocker, PROJECT_DIRECTORY
from qa_topology import choose_subnet

sys.path.insert(0, str(DEPLOY_DIRECTORY))

from docker_networks import canonicalize_network_attachment
from release_state import canonical, digest


TRIALS = 100
ALIASES = ("historical-frontend", "frontend")


def inspect_attachment(docker, container, network):
    item = docker.inspect("container", container)
    bridge = docker.inspect("network", network)
    endpoint = item["NetworkSettings"]["Networks"][network]
    peers = bridge["Containers"] or {}
    assert item["Id"] == container and item["State"]["Running"]
    assert bridge["Name"] == network and bridge["Driver"] == "bridge"
    assert endpoint["NetworkID"] == bridge["Id"] and set(peers) == {container}
    assert peers[container]["IPv4Address"] == f"{endpoint['IPAddress']}/{endpoint['IPPrefixLen']}"
    assert set(endpoint["Aliases"]) == set(ALIASES)
    return endpoint


def exercise(docker, trials):
    anchor = docker.create_network("reconnect-anchor", internal=True)
    network = docker.create_network("reconnect-subject", subnet=choose_subnet(docker))
    container = docker.create_container("reconnect-frontend", CADDY_IMAGE, "-ec", "exec sleep 900",
                                        args=("--network", anchor, "--entrypoint", "/bin/sh"), cpus="0")
    # The stock image and no user files are the only inputs to this fixture.
    docker.run("start", container)
    original_process = docker.identity(container)
    connect = ("network", "connect")
    aliases = tuple(part for alias in ALIASES for part in ("--alias", alias))
    for number in range(1, TRIALS + 1):
        docker.run(*connect, *aliases, network, container)
        before = inspect_attachment(docker, container, network)
        address = before["IPAddress"]
        docker.run("network", "disconnect", network, container)
        docker.run(*connect, "--ip", address, *aliases, network, container)
        after = inspect_attachment(docker, container, network)
        policy = {"allow_generated_mac": True, "allow_empty_driver_opts": True, "reconnected_ipv4": address}
        stable_before, volatile_before = canonicalize_network_attachment(network, before, **policy)
        stable_after, volatile_after = canonicalize_network_attachment(network, after, **policy)
        if canonical(stable_before) != canonical(stable_after):
            trials.append({"trial": number, "status": "FAIL", "stable_before": stable_before,
                           "stable_after": stable_after, "before": volatile_before, "after": volatile_after})
            raise AssertionError(f"Attachment drift in trial {number}")
        assert before["EndpointID"] != after["EndpointID"], f"Endpoint was not renewed in trial {number}"
        assert docker.identity(container) == original_process
        trials.append({
            "trial": number,
            "stable_sha256": digest(stable_after),
            "before": volatile_before,
            "after": volatile_after,
            "changed_fields": sorted(
                key for key in set(volatile_before) | set(volatile_after)
                if (key in volatile_before) != (key in volatile_after)
                or volatile_before.get(key) != volatile_after.get(key)
            ),
        })
        docker.run("network", "disconnect", network, container)


def main():
    if not __debug__:
        raise RuntimeError("Reconnect QA requires assertions")
    docker = LocalDocker()
    report = {"status": "FAIL", "engine": docker.run("version", "--format", "{{.Server.Version}}"), "trials": []}
    try:
        exercise(docker, report["trials"])
        report["status"] = "PASS"
    finally:
        try:
            docker.cleanup()
            report["owned_cleanup"] = "PASS"
        finally:
            path = PROJECT_DIRECTORY / ".tmp" / f"bgv26-network-reconnect-{docker.nonce}.json"
            with path.open("xb") as evidence:
                evidence.write(canonical(report) + b"\n")
            print(json.dumps({"status": report["status"], "engine": report["engine"], "trials": len(report["trials"]),
                              "owned_cleanup": report.get("owned_cleanup", "FAIL"),
                              "evidence": str(path)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
