"""Exercise monotonic promotion and passive Player latency on disposable Linux Docker."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import get_clock_info, monotonic_ns
from uuid import uuid4

from qa_docker import DEPLOY_DIRECTORY, DatabaseFixture, LocalDocker, RELEASES, wait_until
from qa_http import API_HOST, APP_HOST
from qa_player_observer import Capture, heartbeat, playback, read_capture_ready
from qa_topology import Topology

from docker_logs import verify_caddy_proxy_log
from edge_boot import ColdStart
from gates import PlayerGate
from player_capture import PHASE3_CAPTURE_SECONDS
from player_observer_launcher import promotion_switch_marker
from promotion_observer_context import committed_routing_context
from release_state import StateStore, Stop, canonical, decode, digest, read_private, write_private_atomic
from routectl import CaddyEdge, Controller


ROLES = ("api", "frontend")
BLUE_PAIR = {"api": "BLUE", "frontend": "BLUE"}
SPLIT_PAIR = {"api": "GREEN", "frontend": "BLUE"}
GREEN_PAIR = {"api": "GREEN", "frontend": "GREEN"}
SAAS_GREEN = {
    "revision": "a1f986af239c26c22ff3bd9ff9a7a43c05d5ede9",
    "tree": "dc07aa473636b2a1d7cb5cdffe9a664243ef4e42",
    "images": {
        "api": "sha256:b824c46326a6b1e8204f8fd307dcbc3212123756ab1bd426e52d19317009fcdb",
        "frontend": "sha256:6ee4c4469a24a4ffd19bb1561c7cf95214ffa6355dbdac323dd6d889653d4762",
    },
}
INTERFACE_LOOKUP = """
import fcntl,socket,struct,sys
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    for _, interface in socket.if_nameindex():
        try:
            result = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s', interface.encode()))
        except OSError:
            continue
        if socket.inet_ntoa(result[20:24]) == sys.argv[1]:
            print(interface)
            break
    else:
        raise SystemExit(1)
"""


def save(path, value):
    write_private_atomic(path, canonical(value) + b"\n", replace=False)


def read(path):
    return decode(read_private(path))


def command_result(command, *, timeout=45, expected=0):
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    require_exit(result.returncode, expected, result.stderr)
    return result


def require_exit(actual, expected, stderr):
    if actual == expected:
        return
    # Preserve code-only diagnostics and keep raw command arguments and streams private.
    codes = [line for line in stderr.decode(errors="replace").splitlines()
             if re.fullmatch(r"[A-Z][A-Z0-9_]+(?:=[A-Z0-9_.-]+)?", line)]
    raise AssertionError(f"Launcher exit {actual}, expected {expected}: {', '.join(codes)}")


def extend_disposable_players(db):
    """Clone sealed QA rows while retaining defaults and fields on the fixture schema."""
    known_channel = db.fixture["channels"][0]
    known_player = db.fixture["devices"][0]
    for ordinal in range(4, 17):
        channel, player = uuid4(), uuid4()
        token = secrets.token_urlsafe(36)
        db.query(f"""
            INSERT INTO public.channels
            SELECT (jsonb_populate_record(NULL::public.channels, to_jsonb(source) || jsonb_build_object(
                'id', '{channel}', 'name', 'BGV267 QA channel {ordinal}', 'channel_code', '{channel.hex[:8]}'))).*
            FROM public.channels source WHERE source.id='{known_channel}';
            INSERT INTO public.player_devices
            SELECT (jsonb_populate_record(NULL::public.player_devices, to_jsonb(source) || jsonb_build_object(
                'id', '{player}', 'channel_id', '{channel}', 'hardware_id', 'bgv267-qa-{ordinal}',
                'device_name', 'BGV267 QA Player {ordinal}', 'player_token_hash', '{sha256(token.encode()).hexdigest()}'))).*
            FROM public.player_devices source WHERE source.id='{known_player}';
        """)
        db.player_tokens.append(token)
    assert len(db.player_tokens) == 17 and db.readonly("SELECT count(*) FROM public.player_devices") == ["17"]


class MonotonicCapture(Capture):
    """Reuse the owned collector and attach its sealed monotonic transport."""

    def __init__(self, topology):
        super().__init__(topology, PHASE3_CAPTURE_SECONDS, defer=True)

    def start(self):
        assert self.child is None
        interface = self.docker.read(self.container, "python", "-I", "-B", "-c",
                                     INTERFACE_LOOKUP, self.expected["proxy_ip"])
        assert re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)
        self.child = subprocess.Popen([
            *self.docker.command, "exec", self.container, "python", "-I", "-B", "/observe.py", interface,
            self.expected["proxy_ip"], self.expected["ip"], str(self.seconds), "--phase3-monotonic", "--player-latency",
        ], env=self.docker.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.ready = read_capture_ready(self.child.stdout)


class PromotionFixture:
    def __init__(self, evidence):
        self.evidence = evidence
        evidence.mkdir(mode=0o700)
        self.docker = LocalDocker()
        self.secret_store = TemporaryDirectory(prefix="bgv267-fixture-key-")

    def setup(self):
        self.topology = Topology(self.docker)
        self.db = DatabaseFixture(self.docker, self.topology.app_net, self.topology.uploads)
        self.db.start()
        extend_disposable_players(self.db)
        for slot in RELEASES:
            self.topology.add_slot(slot, self.db)
        self.http = self.topology.start_edge()
        self.topology.check_isolation()
        edge = self.docker.inspect("container", self.topology.edge)
        origin = Path("/proc") / str(edge["State"]["Pid"]) / "root"
        self.wire = CaddyEdge(origin / "run/bgv2/admin.sock", origin / "config/caddy/autosave.json")
        self.key = os.urandom(48)
        self.probe = self.topology.probe()
        self.ready = {slot: {role: digest(self.probe(role, slot)) for role in ROLES} for slot in RELEASES}
        self.store = StateStore(self.evidence / "state")
        self.gate = PlayerGate(self.key, require_monotonic=True)
        # Lifecycle readiness is setup in this fixture. Promotion uses the actual strict PlayerGate.
        self.controller = Controller(self.store, self.wire, self.probe,
                                     lambda receipt, context: receipt == context["identity_hash"], self.gate)
        cold = ColdStart(self.controller)
        cold.initialize(self.topology.baseline, RELEASES["BLUE"], self.ready["BLUE"], self.topology.baseline_hash)
        cold.recover()
        self.bindings = self.evidence / "bindings.json"
        save(self.bindings, self.topology.bindings)
        self.key_path = Path(self.secret_store.name) / "receipt.key"
        write_private_atomic(self.key_path, self.key, replace=False)
        self.epochs = self.identities()

    def identities(self):
        return {pair: self.docker.identity(container) for pair, container in self.topology.containers.items()}

    def close(self):
        try:
            capture = getattr(self, "capture", None)
            if capture is not None:
                capture.close()
        finally:
            try:
                self.docker.cleanup()
            finally:
                self.secret_store.cleanup()

    def refresh_players(self):
        for token in self.db.player_tokens:
            heartbeat(self.http, token)

    def launcher(self, clock, action, *args):
        command = [sys.executable, "-B", str(DEPLOY_DIRECTORY / "player_observer_launcher.py"),
                   "--collector-id", self.capture.container, "--collector-image", RELEASES["GREEN"]["images"]["api"],
                   "--collector-restart-count", "0", "--clock-receipt", str(clock)]
        if action != "probe":
            command += ["--preflight-receipt", str(self.preflight)]
        return command + [action, *args]

    def observer(self, receipt, action, *args):
        return ["--state-dir", str(self.store.directory), "--postgres-id", self.db.pg, "--key", str(self.key_path),
                "--receipt", str(receipt), action, "--socket", str(self.wire.socket_path),
                "--autosave", str(self.wire.autosave_path), "--bindings", str(self.bindings),
                "--edge-name", f"{self.docker.name}-edge", *args]

    def ready_green(self):
        self.controller.prepare("GREEN", RELEASES["GREEN"])
        self.controller.start("GREEN")
        for role in ROLES:
            self.controller.register_ready(role, "GREEN", self.ready["GREEN"][role])

    def api_switch(self, label):
        baseline = self.evidence / f"{label}-baseline.json"
        clock = self.evidence / f"{label}-baseline-launch.json"
        command_result(self.launcher(clock, "promotion-baseline", "--", *self.observer(baseline, "promotion-baseline")))
        proof = read(baseline)
        assert len(proof["payload"]["baseline_refs"]) == 17
        assert read(clock)["TIME_NAMESPACE_MATCH"] == "YES"
        switched = self.controller.switch_api("GREEN", proof, clock_receipt=read(Path(str(baseline) + ".clock.json")))
        stamp = monotonic_ns()
        assert switched["routing"]["active"] == SPLIT_PAIR
        return baseline, switched, stamp

    def advance(self, generation):
        for index in range((generation - 1) // 2):
            self.ready_green()
            _, switched, _ = self.api_switch(f"warmup-{index}")
            assert switched["routing"]["generation"] == index * 2 + 1
            reverted = self.controller.rollback()
            assert reverted["routing"]["active"] == BLUE_PAIR and reverted["routing"]["generation"] == index * 2 + 2
        assert self.controller.status()["routing"]["generation"] == generation - 1

    def public_route(self, role, slot, generation):
        host, path = (API_HOST, "/api/player/time") if role == "api" else (APP_HOST, "/")
        response = self.http.request(host, path).require(200)
        capture = self.docker.logs(self.topology.edge)
        verify_caddy_proxy_log(capture, self.topology.edge, request_id=response.headers["x-request-id"],
                               generation=generation, upstream=self.topology.bindings[f"{role}:{slot}"]["upstream"])

    def playback_window(self, label, baseline, switched, stamp, *, missing):
        receipt = self.evidence / f"{label}-player.json"
        timeline = self.evidence / f"{label}-timeline.jsonl"
        clock = self.evidence / f"{label}-observe-launch.json"
        save(timeline, promotion_switch_marker(switched, stamp))
        command = self.launcher(clock, "promotion-observe", "--timeline", str(timeline), "--", *self.observer(
            receipt, "promotion-observe", "--edge-id", self.topology.edge, "--baseline", str(baseline),
            "--capture-ready-json", self.capture.ready.decode("ascii").strip(),
        ))
        with subprocess.Popen(command, stdin=self.capture.child.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as observer:
            def observation_started():
                for line in read_private(timeline).splitlines():
                    row = decode(line)
                    if row["event"] == "T_PLAYER_OBSERVATION_STARTED":
                        return row
                if observer.poll() is not None:
                    _, errors = observer.communicate()
                    require_exit(observer.returncode, 0, errors)
                return None

            try:
                marker = wait_until(observation_started, timeout=18)
                target = self.db.player_tokens[:-1] if missing else self.db.player_tokens
                for token in target:
                    heartbeat(self.http, token)
                self.http.request(API_HOST, "/api/player/runtime-config",
                                  headers={"Authorization": "Bearer " + target[0]}).require(200)
                playback(self.http, target[0])
                self.public_route("api", "GREEN", switched["routing"]["generation"])
                output, errors = observer.communicate(timeout=180)
                elapsed = monotonic_ns() - stamp
                require_exit(observer.returncode, 2 if missing else 0, errors)
            finally:
                # Avoid waiting indefinitely on the launcher if the test-emulator aborts.
                if observer.poll() is None:
                    observer.terminate()
                    observer.wait(timeout=10)
        assert self.capture.child.wait(timeout=8) == 0
        room = read(clock)
        assert room["TIME_NAMESPACE_MATCH"] == "YES"
        assert marker["player_time_namespace"] == room["COLLECTOR_TIME_NS"] == room["OBSERVER_TIME_NS"]
        assert elapsed >= 120_000_000_000
        proof = read(receipt)
        requests = read(Path(str(receipt) + ".requests.json"))
        assert requests["requests"] and digest(requests) == proof["payload"]["observation"]["REQUEST_EVIDENCE_SHA256"]
        archive = read(Path(str(receipt) + ".docker-logs.json"))
        assert archive["stream_integrity"] == "PASS" and set(archive["streams"]) == {"stdout", "stderr"}
        assert archive["merged_timeline"] and "raw_encoding" not in archive
        report = requests["player_latency"]
        lines = output.decode().splitlines()
        assert "PLAYER_TIMEOUT_CONTRACTS_REAL=PASS" in lines
        if not missing:
            assert "PLAYER_CONTINUITY_GATE=PASS_ALL_BASELINE" in lines
        assert report["PLAYER_TIMEOUT_CONTRACTS_REAL"] == "PASS"
        for metric, count, limit in (("HEARTBEAT", len(target), 5), ("RUNTIME", 1, 8), ("PLAYBACK", 1, 5)):
            prefix = "REAL_" + metric + "_LATENCY_"
            assert report[prefix + "COUNT"] == count and report[prefix + "STATUS"] == "PASS"
            assert 0 < report[prefix + "MAX"] < limit * 1_000_000_000
        return proof, elapsed, report

    def reject_replayed_receipt(self, proof, generation):
        reverted = self.controller.rollback()
        assert reverted["routing"]["generation"] == generation + 2 and reverted["routing"]["active"] == BLUE_PAIR
        self.refresh_players()
        self.ready_green()
        _, switched, _ = self.api_switch("after-rollback")
        assert switched["routing"]["generation"] == generation + 3
        context = committed_routing_context(switched, self.wire.effective()[0])
        assert not self.gate(proof, switched["routing"]["api_transition"], routing_context=context)
        try:
            self.controller.switch_frontend("GREEN", proof)
        except Stop as error:
            assert str(error) == "PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK"
        else:
            raise AssertionError("A valid old receipt promoted a later generation")
        routing = self.controller.status()["routing"]
        assert routing["active"] == BLUE_PAIR and routing["generation"] == generation + 4
        self.public_route("api", "BLUE", routing["generation"])
        self.public_route("frontend", "BLUE", routing["generation"])
        return {"rejection": "PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK", "restored_generation": routing["generation"]}

    def run(self, generation, *, missing=False):
        label = "missing-one" if missing else "green"
        self.capture = MonotonicCapture(self.topology)
        self.preflight = self.evidence / "collector-preflight.json"
        command_result(self.launcher(self.preflight, "probe"))
        self.refresh_players()
        self.advance(generation)
        self.refresh_players()
        self.ready_green()
        self.capture.start()
        baseline, switched, stamp = self.api_switch(label)
        assert switched["routing"]["generation"] == generation
        proof, elapsed, latency = self.playback_window(label, baseline, switched, stamp, missing=missing)
        transition = switched["routing"]["api_transition"]
        credited = {row["device_ref"] for row in proof["payload"]["heartbeats"]}
        baseline_refs = set(read(baseline)["payload"]["baseline_refs"])
        expected = 16 if missing else 17
        assert len(baseline_refs) == 17 and len(credited & baseline_refs) == expected and credited <= baseline_refs
        assert proof["payload"]["playback_evidence"] == "PASS"
        routing = committed_routing_context(switched, self.wire.effective()[0])
        assert self.gate(proof, transition, routing_context=routing) is not missing
        observation = proof["payload"]["observation"]
        start, finish = observation["WINDOW_START_MONOTONIC"], observation["WINDOW_FINISH_MONOTONIC"]
        assert finish - start >= 120_000_000_000
        replay_control = None
        if missing:
            reverted = self.controller.rollback()
            assert reverted["routing"]["generation"] == generation + 1 and reverted["routing"]["active"] == BLUE_PAIR
            assert reverted["routing"]["api_transition"] is None
            self.public_route("api", "BLUE", generation + 1)
            self.public_route("frontend", "BLUE", generation + 1)
            try:
                self.controller.switch_frontend("GREEN", proof)
            except Stop as error:
                assert str(error) == "FRONTEND_SWITCH_NOT_ALLOWED"
            else:
                raise AssertionError("Rollback allowed old GREEN evidence")
        else:
            committed = self.controller.switch_frontend("GREEN", proof)
            assert committed["routing"]["generation"] == generation + 1 and committed["routing"]["active"] == GREEN_PAIR
            self.public_route("frontend", "GREEN", generation + 1)
            if generation == 7:
                replay_control = self.reject_replayed_receipt(proof, generation)
        assert self.identities() == self.epochs
        assert self.db.index_proof() == self.db.index
        result = {"result": "EXPECTED_ROLLBACK" if missing else "PASS", "generation": generation,
                  "expected_players": 17, "observed_players": expected, "missing_players": 17 - expected,
                  "api_switch_to_terminal_elapsed_monotonic_ns": elapsed,
                  "window_start_monotonic_ns": start, "window_finish_monotonic_ns": finish,
                  "window_duration_monotonic_ns": finish - start,
                  "TIME_NAMESPACE_MATCH": "YES", "runtime_epochs_unchanged": True,
                  "final_active": BLUE_PAIR if missing else GREEN_PAIR, "final_generation": generation + 1,
                  "observer_generated_requests": 0, "valid_old_receipt_after_rollback": replay_control,
                  "PLAYER_CONTINUITY_GATE": "FAIL_MISSING_BASELINE" if missing else "PASS_ALL_BASELINE",
                  "edge_log_evidence": "SANITIZED_SEPARATE_STREAMS_MERGED_TIMELINE",
                  **{k: v for k, v in latency.items() if k.startswith(("REAL_", "PLAYER_"))}}
        save(self.evidence / "summary.json", result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--generations", nargs="+", type=int, choices=(1, 3, 7), default=(1, 3, 7))
    parser.add_argument("--skip-negative", action="store_true")
    args = parser.parse_args()
    if not __debug__ or get_clock_info("monotonic").implementation != "clock_gettime(CLOCK_MONOTONIC)":
        raise RuntimeError("Disposable replay requires Linux CLOCK_MONOTONIC and enabled assertions")
    os.umask(0o077)
    root = args.evidence_dir.resolve()
    root.mkdir(mode=0o700)
    RELEASES["GREEN"] = SAAS_GREEN
    LocalDocker().verify_inputs()
    cases = [(generation, False) for generation in args.generations]
    if not args.skip_negative:
        cases.append((1, True))
    fixtures = []
    with ExitStack() as cleanup:
        for generation, missing in cases:
            fixture = PromotionFixture(root / ("rollback" if missing else f"generation-{generation}"))
            cleanup.callback(fixture.close)
            fixtures.append(fixture)
            fixture.setup()
        with ThreadPoolExecutor(max_workers=len(fixtures)) as pool:
            futures = [pool.submit(fixture.run, generation, missing=missing)
                       for fixture, (generation, missing) in zip(fixtures, cases, strict=True)]
            results = [future.result() for future in futures]
    result = {"result": "PASS", "scope": "REAL_DISPOSABLE_EDGE_POSTGRES_PASSIVE_TRANSPORT",
              "green_release": SAAS_GREEN, "blue_release": RELEASES["BLUE"], "cases": results,
              "cleanup": "PASS", "production_accessed": False}
    save(root / "summary.json", result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
