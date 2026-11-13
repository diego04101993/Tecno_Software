"""Exercise V2.1 boot admission with real isolated Caddy HTTP/TLS and a killed writer."""

import hashlib
import json
import multiprocessing
from pathlib import Path
import secrets
import tempfile

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, LocalDocker, PROJECT_DIRECTORY, RELEASES, wait_until
from qa_edge_recovery import FaultWire, baseline, expect_stop, prepare_green, synthetic_identity
from qa_http import API_HOST, QA_CADDY_CA, QA_CLIENT_CA
from qa_topology import binding, mount_volume
from edge_boot import BOOTSTRAP, ColdStart
from gates import PlayerGate
from release_state import StateStore, canonical, digest, write_private_atomic
from routectl import BLUE_PAIR, Controller


def configured_controller(store, wire, key):
    return Controller(store, wire, synthetic_identity,
                      lambda receipt, context: digest(receipt) == context["identity_hash"], PlayerGate(key))


def die_before_admission(root, wire, key, reached):
    def paused(_):
        reached.set()
        multiprocessing.Event().wait(30)

    wire.open_ingress = paused
    ColdStart(configured_controller(StateStore(root), wire, key)).recover()


def supervise_boots(root, wire, key):
    ColdStart(configured_controller(StateStore(root), wire, key)).supervise()


class Fixture:
    def __init__(self, docker):
        self.docker = docker
        ingress = docker.create_network("boot-ingress", internal=True)
        proxy = docker.create_network("boot-proxy", internal=True)
        config, runtime, tls = [docker.create_volume("boot-" + name) for name in ("config", "run", "tls")]
        source = binding(DEPLOY_DIRECTORY, "/tooling")
        shared = ("--mount", mount_volume(config, "/config"), "--mount", mount_volume(runtime, "/run/bgv2"))
        self.relay = docker.create_container("boot-relay", RELEASES["GREEN"]["images"]["api"], "sleep", "600",
            args=("--network", "none", "--mount", source, *shared), env={"PYTHONPATH": "/tooling"}, cpus="0")
        docker.run("start", self.relay)
        docker.read(self.relay, "sh", "-ec", "mkdir -p /config/caddy; chmod 700 /config /config/caddy /run/bgv2")
        for slot in ("BLUE", "GREEN"):
            upstream = docker.create_container("boot-" + slot.lower(), RELEASES["GREEN"]["images"]["api"],
                "sh", "-ec", f"mkdir /site; printf '%s' {slot} > /site/index.html; "
                "exec python -B -m http.server 8000 --directory /site",
                args=("--network", proxy, "--network-alias", "backend-" + slot.lower()), cpus="0")
            docker.run("start", upstream)
        self.edge = docker.create_container("boot-edge", CADDY_IMAGE, "sh", "/opt/bgv2/edge-entrypoint.sh",
            args=("--network", ingress, "--mount", binding(DEPLOY_DIRECTORY, "/opt/bgv2"), *shared,
                  "--mount", mount_volume(tls, "/data")), cpus="0")
        docker.run("network", "connect", proxy, self.edge)
        self.client = docker.create_container("boot-client", CADDY_IMAGE, "sleep", "600",
                                               args=("--network", ingress), cpus="0")
        docker.run("start", self.edge, self.client)
        self.wire = FaultWire(docker, self.relay, self.edge)
        self.wire.boot_token = self.boot_token
        self.ingress = ingress
        self.wait_boot()
        adapted = docker.read(self.edge, "caddy", "adapt", "--config", "/opt/bgv2/Caddyfile.edge")
        docker.read(self.relay, "sh", "-ec", "mkdir -m 700 /sealed")
        docker.read(self.relay, "python", "-B", "/tooling/edge_config.py", "--config-dir", "/config",
                    "--baseline", "/sealed/blue.json", input=adapted.encode())
        self.config = json.loads(docker.read(self.relay, "cat", "/sealed/blue.json"))

    def wait_boot(self):
        wait_until(lambda: self.docker.execute(self.edge, "test", "-S", "/run/bgv2/admin.sock").returncode == 0)
        assert self.wire.raw_effective()[0] == BOOTSTRAP
        self.address = self.docker.inspect("container", self.edge)["NetworkSettings"]["Networks"][self.ingress]["IPAddress"]
        self.require_no_app(no_listener=True)

    def restart(self):
        self.docker.run("restart", self.edge)
        self.wait_boot()

    def boot_token(self):
        return self.docker.read(self.relay, "cat", "/run/bgv2/boot-id").encode() + b"\n"

    def put_autosave(self, raw):
        self.docker.read(self.relay, "python", "-B", "-c", """
import sys
from pathlib import Path
from release_state import write_private_atomic
path=Path('/config/caddy/autosave.json')
data=sys.stdin.buffer.read()
if data:
    write_private_atomic(path,data)
else:
    path.unlink(missing_ok=True)
""", input=raw)

    def install_ca(self):
        ca = (self.docker.read(self.edge, "cat", QA_CADDY_CA) + "\n").encode()
        self.docker.read(self.client, "sh", "-ec", 'umask 077; cat > "$1"', "qa-ca", QA_CLIENT_CA, input=ca)

    def http(self, port):
        protocol = "https" if port == 443 else "http"
        result = self.docker.execute(self.client, "curl", "-sS", "--max-time", "2", "--noproxy", "*",
            "--cacert", QA_CLIENT_CA, "--connect-to", f"{API_HOST}:{port}:{self.address}:{port}",
            "-w", "\n%{http_code}", f"{protocol}://{API_HOST}/")
        body, _, status = result.stdout.decode().rpartition("\n")
        return int(status or "0"), body

    def require_no_app(self, no_listener=False):
        expected = {0} if no_listener else {0, 503}
        for port in (80, 443):
            status, body = self.http(port)
            assert status in expected and body not in ("BLUE", "GREEN"), (port, status)


def verify(root, fixture):
    wire = fixture.wire
    key = secrets.token_bytes(48)
    store = StateStore(root)
    controller = configured_controller(store, wire, key)
    boot = ColdStart(controller)
    boot.preview(fixture.config)
    fixture.require_no_app(no_listener=True)
    boot.initialize(fixture.config, RELEASES["BLUE"], {role: synthetic_identity(role, "BLUE") for role in BLUE_PAIR},
                    digest(fixture.config))
    fixture.require_no_app(no_listener=True)
    fixture.install_ca()
    assert boot.recover()["generation"] == 0
    assert fixture.http(443) == (200, "BLUE")
    assert fixture.http(80)[0] in (301, 308)
    blue = wire.read_autosave()
    prepare_green(controller)
    prior_state = store.path.read_bytes()
    assert controller.switch_api("GREEN", baseline(controller.status(), key))["routing"]["generation"] == 1
    green = wire.read_autosave()
    committed = store.path.read_bytes()

    fixture.restart()
    assert boot.recover()["result"] == "COMMITTED" and fixture.http(443) == (200, "GREEN")
    fixture.put_autosave(blue)
    fixture.restart()
    assert boot.recover()["result"] == "RESTORED_COMMITTED" and fixture.http(443) == (200, "GREEN")

    write_private_atomic(store.path, prior_state)
    fixture.restart()
    expect_stop("STOP_AMBIGUOUS", boot.recover)
    fixture.require_no_app(no_listener=True)
    fixture.put_autosave(blue)
    assert boot.recover()["generation"] == 0
    wire.fault = "applied"
    expect_stop("EDGE_MUTATION_OUTCOME_UNKNOWN", lambda: controller.switch_api("GREEN", baseline(controller.status(), key)))
    assert store.load()["pending"]["stage"] == "DISPATCHED"
    fixture.restart()
    assert boot.recover()["result"] == "COMMIT" and fixture.http(443) == (200, "GREEN")
    assert store.load()["pending"] is None and store.load()["routing"]["generation"] == 1
    fixture.restart()
    assert boot.recover()["result"] == "COMMITTED" and len(store.load()["history"]) == 1

    write_private_atomic(store.path, b"{")
    fixture.restart()
    expect_stop("INVALID_JSON", boot.recover)
    fixture.require_no_app(no_listener=True)
    write_private_atomic(store.path, committed)
    for broken in (b"{", b""):
        fixture.put_autosave(broken)
        fixture.restart()
        assert boot.recover()["generation"] == 1 and fixture.http(443) == (200, "GREEN")
    store.path.unlink()
    fixture.put_autosave(b"")
    fixture.restart()
    expect_stop("RELEASE_STATE_MISSING", boot.recover)
    fixture.require_no_app(no_listener=True)
    write_private_atomic(store.path, committed)
    fixture.put_autosave(green)

    real_patch = wire.patch

    def drifted_load(config, etag):
        changed = json.loads(canonical(config))
        changed["logging"]["logs"]["default"]["level"] = "WARN"
        return real_patch(changed, etag)

    wire.patch = drifted_load
    expect_stop("EDGE_BOOT_EFFECTIVE_MISMATCH", boot.recover)
    fixture.require_no_app()
    wire.patch = real_patch
    fixture.put_autosave(green)
    fixture.restart()
    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    child = context.Process(target=die_before_admission, args=(store.directory, wire, key, reached))
    child.start()
    try:
        assert reached.wait(30), "Reconciler did not reach the verified pre-admission checkpoint"
        fixture.require_no_app()
    finally:
        child.kill()
        child.join(10)
    assert child.exitcode is not None
    fixture.require_no_app()
    assert boot.recover()["generation"] == 1 and fixture.http(443) == (200, "GREEN")
    return_code = exercise_supervisor(root, fixture, key)
    return {"committed_boot": "PASS", "stale_autosave": "PASS", "stale_route_served": False,
            "stale_state": "STOP_AMBIGUOUS_NO_HTTP", "dispatched": "COMMIT_ONCE",
            "corrupt_state": "PASS", "missing_state": "PASS", "corrupt_autosave": "PASS",
            "missing_autosave": "PASS", "effective_mismatch": "PASS_HTTP_HELD",
            "reconciler_killed": "PASS_HTTP_HELD", "supervisor_reboot": return_code,
            "scope": "LOCAL_REAL_CADDY_HTTP_TLS"}


def exercise_supervisor(root, fixture, key):
    context = multiprocessing.get_context("spawn")
    writer = context.Process(target=supervise_boots, args=(root, fixture.wire, key))
    writer.start()
    try:
        fixture.docker.run("restart", fixture.edge)
        wait_until(lambda: fixture.http(443) == (200, "GREEN"), timeout=30)
        assert writer.is_alive()
    finally:
        writer.kill()
        writer.join(10)
    return "PASS_AUTOMATIC_AFTER_EDGE_RESTART"


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires Python assertions enabled")
    docker = LocalDocker()
    scratch = PROJECT_DIRECTORY / ".tmp"
    try:
        with tempfile.TemporaryDirectory(prefix="bgv2-edge-boot-", dir=scratch) as directory:
            report = verify(Path(directory) / "state", Fixture(docker))
    finally:
        docker.cleanup()
    path = scratch / f"bgv21-edge-boot-{docker.nonce}.json"
    write_private_atomic(path, canonical(report) + b"\n", replace=False)
    print(json.dumps(report, sort_keys=True))
    print(f"RECEIPT={path}")
    print(f"RECEIPT_SHA256={hashlib.sha256(path.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
