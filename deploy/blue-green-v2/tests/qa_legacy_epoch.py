"""Rehearse boot epoch decisions with isolated Docker processes and real Edge TLS."""

import json
from pathlib import Path
import tempfile
from uuid import uuid4

from qa_docker import LocalDocker, PROJECT_DIRECTORY, wait_until
from qa_http import API_HOST, QA_CLIENT_CA
from qa_initial_handoff import Handoff, PUBLIC_IP, Timeline, first_http, handoff, prepare, prepare_trial, reverse
from qa_topology import wait_edge_bootstrap

from edge_boot import BOOTSTRAP, ColdStart
from release_state import Stop


def restart_roles(fixture, *roles):
    containers = [getattr(fixture, role) for role in roles]
    fixture.docker.run("stop", "--time", "3", *containers)
    fixture.docker.run("start", *containers)
    for container in containers:
        wait_until(lambda: fixture.docker.inspect("container", container)["State"]["Health"]["Status"] == "healthy")


def restart_closed_edge(fixture):
    fixture.docker.run("restart", "--time", "3", fixture.edge)
    wait_edge_bootstrap(fixture.docker, fixture.edge)
    assert fixture.wire.raw_effective()[0] == BOOTSTRAP
    assert not fixture.wire.ingress_open()
    # No HTTP or HTTPS listener exists on Edge itself. Desktop's published
    # port forwarder can accept and reset a TCP connection to a closed port.
    for port in (80, 443):
        direct = fixture.docker.execute(fixture.client, "nc", "-z", "-w", "1", PUBLIC_IP, str(port))
        assert direct.returncode != 0, f"Cold Edge unexpectedly listened on {port}"
    result = fixture.docker.execute(fixture.client, "curl", "--noproxy", "*", "--silent", "--fail",
                                    "--max-time", "1", "--cacert", QA_CLIENT_CA, "--connect-to",
                                    f"{API_HOST}:443:host.docker.internal:{fixture.ports['443/tcp']}",
                                    "--write-out", "%{http_code}", f"https://{API_HOST}/")
    assert result.returncode in (7, 28, 35, 56) and result.stdout == b"000", (
        f"Closed public Edge returned unexpected curl status {result.returncode}")


def verify_admitted(fixture):
    assert fixture.wire.ingress_open()
    cold = ColdStart(fixture.controller)
    before = fixture.store.load()
    postgres_before = fixture.docker.identity(fixture.db.pg)

    # Docker restarts the real disposable containers. Only the kernel boot
    # observation is injected so the QA host and its Docker Engine stay up.
    new_boot = str(uuid4())
    fixture.post_probe._host_boot_id = lambda *_: new_boot
    fixture.post_probe.legacy_engine_boot_id = lambda: new_boot
    restart_roles(fixture, "api", "frontend")
    restart_closed_edge(fixture)
    result = cold.recover()
    assert result["legacy_blue_reattest"] == "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT"
    first_http(fixture.published_http, edge=True)
    renewed = fixture.store.load()
    assert renewed["routing"] == before["routing"]
    for role in ("api", "frontend"):
        old = before["slots"]["BLUE"]["ready"][role]["identity"]
        current = renewed["slots"]["BLUE"]["ready"][role]["identity"]
        assert current["container_id"] == old["container_id"] and current["started_at"] != old["started_at"]
        assert renewed["legacy_blue"]["runtime_epochs"][role]["boot_id"] == new_boot

    restart_roles(fixture, "api")
    try:
        cold.monitor_legacy_blue()
    except Stop as exc:
        assert str(exc) == "STOP_REATTEST_REQUIRED"
    else:
        raise AssertionError("A second restart in the same boot should have closed ingress")
    assert not fixture.wire.ingress_open()
    restart_closed_edge(fixture)
    try:
        cold.recover()
    except Stop as exc:
        assert str(exc) == "STOP_REATTEST_REQUIRED"
    else:
        raise AssertionError("A second restart in the same boot should have blocked cold recovery")
    assert not fixture.wire.ingress_open() and fixture.store.load() == renewed
    assert fixture.docker.identity(fixture.db.pg) == postgres_before

    # Update the rollback comparison snapshots for these intentional restarts.
    fixture.backend_identity = fixture.docker.identity(fixture.api)
    fixture.frontend_identity = fixture.docker.identity(fixture.frontend)
    return {"result": "PASS", "scope": "REAL_DISPOSABLE_DOCKER_RESTARTS_WITH_INJECTED_HOST_BOOT_ID",
            "host_reboot_epoch_renewal": "PASS", "same_boot_backend_restart": "STOP_REATTEST_REQUIRED",
            "postgres_preserved": True}


def run(fixture):
    prepare(fixture)
    prepare_trial(fixture)
    handoff(fixture, Timeline())
    result = verify_admitted(fixture)
    reverse(fixture, Timeline())
    return {**result, "historical_frontend_restored": True}


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires Python assertions enabled")
    docker = LocalDocker()
    try:
        with tempfile.TemporaryDirectory(prefix="bgv22-epoch-", dir=PROJECT_DIRECTORY / ".tmp") as directory:
            print(json.dumps(run(Handoff(docker, Path(directory))), sort_keys=True))
    finally:
        docker.cleanup()


if __name__ == "__main__":
    main()
