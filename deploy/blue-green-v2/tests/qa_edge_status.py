"""Exercise routectl status with a real Caddy/Edge and competing Linux processes."""

import contextlib
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import sys

from qa_docker import DEPLOY_DIRECTORY, LocalDocker, PROJECT_DIRECTORY, RELEASES, wait_until
from qa_edge_recovery import baseline, prepare_green, start_wire, synthetic_identity
from qa_topology import binding

from edge_boot import ColdStart
from gates import PlayerGate
from release_state import StateStore, canonical, digest, write_private_atomic
from routectl import BLUE_PAIR, CaddyEdge, Controller, bind_legacy_blue
from test_legacy_blue import envelope, legacy_fixture
from test_legacy_epoch import EpochProbe


PRIOR_REVISION = "1920b3b291cfc63ebc430580a8583cbb59289473"
CURRENT_TOOLING = PurePosixPath("/workspace/deploy/blue-green-v2")
PRIOR_TOOLING = PurePosixPath("/prior-tooling")
ROOT = Path("/qa-status")
STATE = ROOT / "state"
SOCKET = Path("/run/bgv2/admin.sock")
AUTOSAVE = Path("/config/caddy/autosave.json")
FIXTURE = ROOT / "fixture"


def legacy_probe_and_release(manifest=None):
    source = legacy_fixture()
    if manifest is None:
        manifest = envelope(source)
    probe = EpochProbe(source, manifest)
    binding = source[-1]
    release = {
        "revision": binding["revision"], "tree": binding["tree"],
        "images": {"api": binding["image_id"], "frontend": probe.frontend_identity["image_id"]},
    }
    probe.frontend_identity.update(revision=release["revision"], tree=release["tree"])
    return probe, manifest, release


def checkpoint(name):
    write_private_atomic(ROOT / name, b"ready\n", replace=False)
    wait_until(lambda: (ROOT / (name + ".continue")).is_file(), timeout=120)


def install_cli_fixture():
    """Replace only external identity input and pause the real writer at QA checkpoints."""
    import gates

    mode = os.environ["BGV266_QA_MODE"]
    action = os.environ["BGV266_QA_ACTION"]
    if mode == "legacy":
        pinned = json.loads((ROOT / "legacy-manifest.json").read_bytes())
        probe, _, _ = legacy_probe_and_release(pinned)
    else:
        assert mode == "transition"
        probe = synthetic_identity
    if action == "supervise-ingress":
        check_runtime = probe.verify_legacy_runtime_running

        def paused_monitor(ready, epoch):
            check_runtime(ready, epoch)
            checkpoint("supervisor-held")

        probe.verify_legacy_runtime_running = paused_monitor
    if mode == "transition" and action == "switch-api":
        save = StateStore.save

        def paused_save(store, state):
            if state["pending"] is None and state["routing"]["generation"] == 1:
                checkpoint("after-patch")
            save(store, state)
            if state["pending"] is not None and state["pending"]["stage"] == "DISPATCHED":
                checkpoint("before-patch")

        StateStore.save = paused_save
    gates.DockerProbe = lambda *_args, **_kwargs: probe
    if action == "status" and os.environ["BGV266_QA_READ_ONLY"] == "1":
        def readonly(event, args):
            if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise RuntimeError("QA_STATUS_ATTEMPTED_WRITE")
            if event in {"fcntl.flock", "fcntl.lockf", "os.mkdir", "os.remove", "os.rename", "os.link",
                         "os.chmod", "os.chown", "os.utime", "os.truncate", "subprocess.Popen"}:
                raise RuntimeError("QA_STATUS_ATTEMPTED_MUTATION")

        def reject_sync(*_args):
            raise RuntimeError("QA_STATUS_ATTEMPTED_SYNC")

        request = HTTPConnection.request

        def get_only(connection, method, *args, **kwargs):
            assert method == "GET", "QA_STATUS_ATTEMPTED_CADDY_MUTATION"
            return request(connection, method, *args, **kwargs)

        sys.addaudithook(readonly)
        os.fsync = reject_sync
        HTTPConnection.request = get_only


def cli(mode, action, *args, tooling=CURRENT_TOOLING, readonly=True):
    env = {
        **os.environ,
        "PYTHONPATH": f"{FIXTURE}:{tooling}:{CURRENT_TOOLING / 'tests'}",
        "BGV266_QA_MODE": mode,
        "BGV266_QA_ACTION": action,
        "BGV266_QA_READ_ONLY": "1" if readonly else "0",
    }
    command = [sys.executable, "-B", str(tooling / "routectl.py"), "--state-dir", str(STATE),
               "--socket", str(SOCKET), "--autosave", str(AUTOSAVE),
               "--bindings", str(ROOT / "bindings.json"), "--key", str(ROOT / "key"),
               "--edge-name", "qa-disposable", action, *map(str, args)]
    return command, env


def invoke(mode, action, *args, **kwargs):
    command, env = cli(mode, action, *args, **kwargs)
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)


@contextlib.contextmanager
def child(mode, action, *args):
    command, env = cli(mode, action, *args)
    with subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def status(mode, expected):
    result = invoke(mode, "status")
    assert result.stdout, (result.returncode, result.stderr)
    report = json.loads(result.stdout)
    assert report["STATUS"] == expected, report
    assert result.returncode == (0 if expected == "PASS" else 1), result.stderr
    if expected != "PASS":
        assert "state" not in report
    return report


def setup(mode):
    ROOT.mkdir(mode=0o700)
    FIXTURE.mkdir(mode=0o700)
    (FIXTURE / "sitecustomize.py").write_text(
        "from qa_edge_status import install_cli_fixture\ninstall_cli_fixture()\n", encoding="utf-8")
    key = secrets.token_bytes(48)
    write_private_atomic(ROOT / "key", key, replace=False)
    write_private_atomic(ROOT / "bindings.json", b"{}", replace=False)
    write_private_atomic(ROOT / "release.json", canonical(RELEASES["GREEN"]), replace=False)
    edge = CaddyEdge(SOCKET, AUTOSAVE)
    store = StateStore(STATE)
    config = json.loads(Path("/sealed/baseline.json").read_bytes())
    if mode == "legacy":
        probe, manifest, release = legacy_probe_and_release()
        write_private_atomic(ROOT / "legacy-manifest.json", canonical(manifest), replace=False)
        config = bind_legacy_blue(config, manifest, manifest["sha256"])
        write_private_atomic(AUTOSAVE, canonical(config))
        ready = {role: True for role in BLUE_PAIR}
        gate = lambda *_: True
    else:
        probe = synthetic_identity
        release = RELEASES["BLUE"]
        ready = {role: probe(role, "BLUE") for role in BLUE_PAIR}
        gate = lambda receipt, ctx: digest(receipt) == ctx["identity_hash"]
    controller = Controller(store, edge, probe, gate, PlayerGate(key))
    boot = ColdStart(controller)
    boot.preview(config)
    boot.initialize(config, release, ready, digest(config))
    boot.recover()
    return controller, key


def artifacts(controller):
    store, edge = controller.store, controller.edge
    paths = [*sorted(STATE.rglob("*")), AUTOSAVE, edge.ingress_marker, edge.ingress_marker.with_name("boot-id"),
             SOCKET.with_name("admin.sock.writer.lock")]
    files = {
        str(path): (path.stat().st_ino, path.stat().st_mtime_ns,
                    None if path.is_dir() else sha256(path.read_bytes()).hexdigest())
        for path in paths
    }
    effective, etag = edge.effective()
    state = store.load()
    return {"files": files, "effective": effective.hash, "etag": etag,
            "generation": state["routing"]["generation"], "pending": state["pending"]}


def stable_supervisor():
    controller, _ = setup("legacy")
    proof = ROOT / "baseline.json"
    write_private_atomic(proof, b"{}", replace=False)
    with child("legacy", "supervise-ingress") as supervisor:
        wait_until(lambda: (ROOT / "supervisor-held").is_file() or supervisor.poll() is not None)
        assert supervisor.poll() is None and (ROOT / "supervisor-held").is_file()
        before = artifacts(controller)
        old = invoke("legacy", "status", tooling=PRIOR_TOOLING, readonly=False)
        assert old.returncode == 1 and old.stderr.strip() == "ROUTECTL_WRITER_LOCKED" and not old.stdout
        samples = []
        for _ in range(100):
            row = status("legacy", "PASS")
            assert row["WRITER_LOCKED"] == "YES" and row["WRITER_OWNER"] == "Edge supervisor", row
            assert row["WRITER_PID"] == supervisor.pid and isinstance(row["WRITER_IDENTITY"], dict), row
            assert row["state"]["pending"] is None and row["state"]["routing"]["generation"] == before["generation"]
            samples.append(row["WRITER_IDENTITY"])
        writers = {
            "route_switch": ("switch-api", "GREEN", proof),
            "rollback": ("rollback",),
            "reconcile_mutativo": ("reconcile",),
            "state_commit": ("prepare", "GREEN", ROOT / "release.json"),
            "config_PATCH": ("recover-ingress",),
        }
        for route in writers.values():
            denied = invoke("legacy", *route)
            assert denied.returncode == 1 and denied.stderr.strip() == "ROUTECTL_WRITER_LOCKED" and not denied.stdout
        after = artifacts(controller)
        assert supervisor.poll() is None and before == after
        assert all(identity == samples[0] for identity in samples)
        return {
            "status_stable": "PASS", "status_reads_with_writer_active": len(samples),
            "phase3_failure_replay": {"before": old.stderr.strip(), "after": "PASS", "owner": "Edge supervisor"},
            "writer_identity": samples[0], "writer_lock_held": True, "status_side_effects": "NONE",
            "second_writers_blocked": list(writers), "before": before, "after": after,
        }


def transition():
    controller, key = setup("transition")
    store, edge = controller.store, controller.edge
    prepare_green(controller)
    previous = store.load()
    receipt = ROOT / "baseline.json"
    write_private_atomic(receipt, canonical(baseline(previous, key)), replace=False)
    assert status("transition", "PASS")["state"] == previous
    report = {}
    with child("transition", "switch-api", "GREEN", receipt) as writer:
        for checkpoint_name in ("before-patch", "after-patch"):
            signal = ROOT / checkpoint_name
            wait_until(lambda: signal.is_file() or writer.poll() is not None)
            assert writer.poll() is None and signal.is_file()
            before = artifacts(controller)
            assert before["pending"]["stage"] == "DISPATCHED" and before["generation"] == 0
            intent = before["pending"]["intent"]
            expected_hash = intent["previous_hash"] if checkpoint_name == "before-patch" else intent["next_hash"]
            assert before["effective"] == edge.persisted().hash == expected_hash
            for _ in range(12):
                outcome = status("transition", "STATUS_TRANSITION_IN_PROGRESS")
                assert outcome["WRITER_LOCKED"] == "YES" and outcome["WRITER_PID"] == writer.pid
            assert before == artifacts(controller)
            report[checkpoint_name] = {"reads": 12, "pending_generation": intent["generation"],
                                       "effective_sha256": expected_hash}
            write_private_atomic(ROOT / (checkpoint_name + ".continue"), b"go\n", replace=False)
        out, err = writer.communicate(timeout=20)
        assert writer.returncode == 0, (out, err)
    after = status("transition", "PASS")["state"]
    assert after == store.load() and after["pending"] is None
    assert after["routing"]["generation"] == 1 and after["routing"]["active"] == {"api": "GREEN", "frontend": "BLUE"}
    assert after["routing"]["config_hash"] == edge.effective()[0].hash == edge.persisted().hash
    return {"checks": report, "status_transition": "PASS", "impossible_state_observed": False}


class StatusDocker(LocalDocker):
    def create_container(self, role, image, *command, args=(), **options):
        if role == "relay":
            args = (*args, "--mount", binding(DEPLOY_DIRECTORY, str(CURRENT_TOOLING)))
        return super().create_container(role, image, *command, args=args, **options)


def isolated(mode, archive):
    docker = StatusDocker()
    try:
        wire = start_wire(docker)
        if mode == "legacy":
            docker.read(wire.relay, "sh", "-ec", "mkdir -m 700 /prior-tooling; tar --strip-components=2 -xf - -C /prior-tooling",
                        input=archive)
        result = docker.execute(wire.relay, "env", f"PYTHONPATH={CURRENT_TOOLING}", "python", "-B",
                                str(CURRENT_TOOLING / "tests/qa_edge_status.py"), "--inside", mode, timeout=180)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout)
    finally:
        docker.cleanup()


def main():
    assert __debug__, "QA assertions are required"
    if sys.argv[1:] == ["--inside", "legacy"]:
        print(json.dumps(stable_supervisor(), sort_keys=True))
    elif sys.argv[1:] == ["--inside", "transition"]:
        print(json.dumps(transition(), sort_keys=True))
    else:
        assert not sys.argv[1:]
        archive = subprocess.check_output(["git", "archive", PRIOR_REVISION, "deploy/blue-green-v2"], cwd=PROJECT_DIRECTORY)
        report = {"scope": "DISPOSABLE_LOCAL_CADDY_EDGE_AND_LINUX_FLOCK", "prior_revision": PRIOR_REVISION,
                  "stable": isolated("legacy", archive), "transition": isolated("transition", archive)}
        raw = canonical(report) + b"\n"
        evidence = PROJECT_DIRECTORY / ".tmp" / ("bgv266-edge-status-" + secrets.token_hex(6) + ".json")
        write_private_atomic(evidence, raw, replace=False)
        print(raw.decode(), end="")
        print(f"RECEIPT={evidence}")
        print(f"RECEIPT_SHA256={sha256(raw).hexdigest()}")


if __name__ == "__main__":
    main()
