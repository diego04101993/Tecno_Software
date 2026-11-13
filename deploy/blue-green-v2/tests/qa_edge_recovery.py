"""Opt-in Caddy wire recovery QA with no network, database or product traffic.

Run with python -B deploy/blue-green-v2/tests/qa_edge_recovery.py.
"""

import json
import secrets
import sys
import tempfile
from pathlib import Path

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, LocalDocker, PROJECT_DIRECTORY, RELEASES
from qa_topology import QAEdge, binding, mount_volume, wait_edge_bootstrap

from gates import PlayerGate, sign_receipt
from release_state import StateStore, Stop, digest, now
from routectl import BLUE_PAIR, Controller, upstream


class FaultWire(QAEdge):
    """Inject a one-shot transport failure while using the real Unix socket."""

    fault = None

    def request(self, method, etag=None, config=None):
        if method != "PATCH" or self.fault is None:
            return super().request(method, etag, config)
        fault, self.fault = self.fault, None
        if fault == "unapplied":
            raise TimeoutError()
        status, _, _ = super().request(method, etag, config)
        assert status == 200, "The real mutation must apply before losing its response"
        raise TimeoutError()


def expect_stop(code, operation):
    try:
        operation()
    except Stop as error:
        assert str(error) == code, str(error)
    else:
        raise AssertionError(f"Expected {code}")


def start_wire(docker):
    config, run, sealed, tls = [docker.create_volume(role) for role in ("config", "run", "sealed", "tls")]
    socket_mount = mount_volume(run, "/run/bgv2")
    config_mount = mount_volume(config, "/config")
    source_mount = binding(DEPLOY_DIRECTORY, "/tooling")
    isolated = ("--network", "none")
    cpus = docker.run("info", "--format", "{{.NCPU}}")
    cpus = "4" if int(cpus) > 4 else "0"
    caddy = docker.create_container(
        "edge", CADDY_IMAGE, "sh", "/opt/bgv2/edge-entrypoint.sh",
        args=(*isolated, "--mount", binding(DEPLOY_DIRECTORY, "/opt/bgv2"), "--mount", config_mount,
              "--mount", socket_mount, "--mount", mount_volume(tls, "/data")),
        cpus=cpus,
    )
    relay = docker.create_container(
        "relay", RELEASES["GREEN"]["images"]["api"], "sleep", "300",
        args=(*isolated, "--mount", source_mount, "--mount", config_mount,
              "--mount", socket_mount, "--mount", mount_volume(sealed, "/sealed")),
        env={"PYTHONPATH": "/tooling", "PYTHONDONTWRITEBYTECODE": "1"},
        cpus=cpus,
    )
    docker.run("start", relay)
    docker.read(relay, "sh", "-ec", "mkdir -p /config/caddy; chmod 700 /run/bgv2 /config /config/caddy /sealed")
    docker.run("start", caddy)
    adapted = docker.read(caddy, "caddy", "adapt", "--config", "/opt/bgv2/Caddyfile.edge",
                          "--adapter", "caddyfile")
    docker.read(relay, "chmod", "700", "/sealed")
    docker.read(relay, "python", "-B", "/tooling/edge_config.py", "--config-dir", "/config",
                       "--baseline", "/sealed/baseline.json", input=adapted.encode())
    wire = FaultWire(docker, relay, caddy)
    wait_bootstrap(wire)
    wire.baseline = json.loads(docker.read(relay, "cat", "/sealed/baseline.json"))
    for container in (caddy, relay):
        actual = docker.inspect("container", container)
        assert actual["HostConfig"]["NetworkMode"] == "none" and not actual["HostConfig"]["PortBindings"]
    return wire


def wait_bootstrap(wire):
    wait_edge_bootstrap(wire.docker, wire.edge)


def synthetic_identity(role, slot):
    """The fault scenario tests Edge/state only. Product QA supplies real identities."""
    release = RELEASES[slot]
    return {
        "container_id": digest([role, slot]), "pid": 2, "start_ticks": 1, "started_at": "2026-01-01T00:00:00+00:00",
        "image_id": release["images"][role], "revision": release["revision"], "tree": release["tree"],
        "restart_count": 0, "oom_killed": False, "upstream": upstream(role, slot),
        "runtime_hash": digest(role), "mounts_hash": digest(slot), "network_hash": digest([slot, role]),
    }


def prepare_green(controller):
    controller.prepare("GREEN", RELEASES["GREEN"])
    controller.start("GREEN")
    for role in BLUE_PAIR:
        controller.register_ready(role, "GREEN", synthetic_identity(role, "GREEN"))


def baseline(state, key):
    payload = {
        "slot": "GREEN", "prior_slot": "BLUE", "previous_generation": state["routing"]["generation"],
        "identity_hash": digest(synthetic_identity("api", "GREEN")), "release": RELEASES["GREEN"],
        "baseline_at": now(), "baseline_refs": [],
    }
    return sign_receipt("BASELINE", payload, key)


def verify(root, wire):
    from edge_boot import ColdStart

    key = secrets.token_bytes(48)
    controller = Controller(StateStore(root), wire, synthetic_identity,
                            lambda receipt, context: digest(receipt) == context["identity_hash"], PlayerGate(key))
    startup = ColdStart(controller)
    startup.preview(wire.baseline)
    startup.initialize(wire.baseline, RELEASES["BLUE"], {role: synthetic_identity(role, "BLUE") for role in BLUE_PAIR},
                       digest(wire.baseline))
    startup.recover()
    initial, etag = wire.effective()
    writes_before_switch = wire.patch_count
    prepare_green(controller)
    wire.fault = "applied"
    expect_stop("EDGE_MUTATION_OUTCOME_UNKNOWN",
                lambda: controller.switch_api("GREEN", baseline(controller.status(), key)))
    assert wire.patch_count == writes_before_switch + 1
    applied = controller.reconcile()
    assert applied["result"] == "COMMIT" and wire.patch_count == writes_before_switch + 1
    split, _ = wire.effective()
    assert split.active == {"api": "GREEN", "frontend": "BLUE"}
    foreign = initial.changed({"api": "GREEN", "frontend": "GREEN"}, 2)
    assert wire.patch(foreign.config, etag) == 412 and wire.effective()[0].hash == split.hash
    assert wire.persisted().hash == split.hash
    wire.docker.run("restart", wire.edge)
    wait_bootstrap(wire)
    startup.recover()
    assert controller.reconcile()["result"] == "IN_SYNC"
    assert wire.effective()[0].hash == wire.persisted().hash == split.hash
    assert controller.rollback()["routing"]["active"] == BLUE_PAIR
    prepare_green(controller)
    wire.fault = "unapplied"
    before, _ = wire.effective()
    writes_before_failure = wire.patch_count
    expect_stop("EDGE_MUTATION_OUTCOME_UNKNOWN",
                lambda: controller.switch_api("GREEN", baseline(controller.status(), key)))
    expect_stop("STOP_AMBIGUOUS", controller.reconcile)
    assert wire.effective()[0].hash == wire.persisted().hash == before.hash
    assert wire.patch_count == writes_before_failure
    return {"etag_412": "PASS", "lost_response_applied_reconcile": "PASS",
            "lost_response_unapplied_fail_closed": "PASS", "edge_restart": "PASS",
            "route_rollback": "PASS", "scope": "DISPOSABLE_CADDY_UNIX_SOCKET_ONLY"}


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires Python assertions enabled")
    docker = LocalDocker()
    scratch = PROJECT_DIRECTORY / ".tmp"
    scratch.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="bgv2-recovery-", dir=scratch) as directory:
            result = verify(Path(directory) / "state", start_wire(docker))
    finally:
        docker.cleanup()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
