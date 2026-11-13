"""Disposable real Edge/PG/HTTP QA for the passive observer (no image builds)."""

from datetime import timedelta
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import secrets
import subprocess
import tempfile
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

from qa_docker import DatabaseFixture, LocalDocker, PROJECT_DIRECTORY, RELEASES
from qa_http import API_HOST, utc_now
from qa_topology import Topology, binding

from edge_boot import ColdStart
from gates import PlayerGate, device_ref, digest, utc
from player_observer import adjudicate, capture_baseline, from_ns, read_only_query
from release_state import StateStore, Stop
from routectl import Controller


ROLES = ("api", "frontend")
SCRIPT = Path(__file__).resolve().parents[1] / "player_capture.py"


def read_capture_ready(stream, timeout=5):
    signal = Queue(maxsize=1)
    reader = Thread(target=lambda: signal.put(stream.readline(4096)), daemon=True)
    reader.start()
    try:
        ready = signal.get(timeout=timeout)
    except Empty:
        raise RuntimeError("PASSIVE_CAPTURE_READINESS_TIMEOUT") from None
    try:
        assert json.loads(ready)["type"] == "ready"
    except (AssertionError, KeyError, TypeError, ValueError):
        raise RuntimeError("PASSIVE_CAPTURE_FAILED_TO_START") from None
    return ready


class Capture:
    def __init__(self, topology, seconds, *, slot="GREEN", defer=False):
        self.docker = topology.docker
        self.expected = topology.bindings[f"api:{slot}"]
        self.seconds = seconds
        self.child = None
        self.container = self.docker.create_container("observer-" + secrets.token_hex(3), RELEASES[slot]["images"]["api"],
            "sleep", "300", args=("--network", f"container:{topology.edge}", "--cap-drop", "ALL",
                "--cap-add", "NET_RAW", "--security-opt", "no-new-privileges", "--read-only",
                "--mount", binding(SCRIPT, "/observe.py")))
        self.docker.run("start", self.container)
        if not defer:
            self.start()

    def start(self):
        assert self.child is None
        address_scan = """
import fcntl,socket,struct,sys
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    for _, interface in socket.if_nameindex():
        try:
            result = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s',interface.encode()))
        except OSError:
            continue
        if socket.inet_ntoa(result[20:24]) == sys.argv[1]:
            print(interface)
            break
    else:
        raise SystemExit(1)
"""
        interface = self.docker.read(self.container, "python", "-I", "-B", "-c", address_scan, self.expected["proxy_ip"])
        self.child = subprocess.Popen([*self.docker.command, "exec", self.container, "python", "-I", "-B", "/observe.py",
                                       interface, self.expected["proxy_ip"], self.expected["ip"], str(self.seconds)],
                                      env=self.docker.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.ready = read_capture_ready(self.child.stdout)
        except RuntimeError:
            self.close()
            raise

    def collect(self):
        rest, _ = self.child.communicate(timeout=180)
        assert self.child.returncode == 0, "PASSIVE_CAPTURE_EXITED"
        return [self.ready, *rest.splitlines()]

    def close(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=8)


def logs(topology):
    result = topology.docker.invoke("logs", topology.edge)
    assert result.returncode == 0
    return (result.stdout + result.stderr).splitlines()


def heartbeat(http, token, status=200):
    # Independent Player emulators do not expose an ACK or token to the observer.
    http.request(API_HOST, "/api/player/heartbeat", method="POST", headers={"Authorization": "Bearer " + token},
                 body={"status": "online"}).require(status)


def playback(http, token):
    http.request(API_HOST, "/api/player/playback-events", method="POST", headers={"Authorization": "Bearer " + token},
                 body={"started_at": utc_now(), "duration_seconds": 1}).require(201)


def add_replacement(db):
    # Prepare a fifth disposable device/channel before the negative test.
    # There is a uniqueness constraint on player_devices.channel_id.
    token = secrets.token_urlsafe(36)
    channel, device = str(uuid4()), str(uuid4())
    known_channel, known_device = db.fixture["channels"][0], db.fixture["devices"][0]
    db.query("INSERT INTO channels(id,name,channel_code,client_id,branch_id,status,mode,device_mode,orientation,screen_count,"
             "resolution_width,resolution_height,canvas_width,canvas_height,expanded_outputs,output_mapping_json,created_at,updated_at) "
             f"SELECT '{channel}','BGV2 Player Replacement','{channel[:8]}',client_id,branch_id,status,mode,device_mode,orientation,"
             f"screen_count,resolution_width,resolution_height,canvas_width,canvas_height,expanded_outputs,output_mapping_json,"
             f"now(),now() FROM channels WHERE id='{known_channel}'; "
             "INSERT INTO player_devices(id,channel_id,client_id,branch_id,hardware_id,device_name,player_token_hash,"
             "is_active,metadata_json,created_at,updated_at) SELECT "
             f"'{device}','{channel}',client_id,branch_id,'qa-replacement','qa-replacement',"
             f"'{hashlib.sha256(token.encode()).hexdigest()}',true,metadata_json,now(),now() FROM player_devices WHERE id='{known_device}'")
    return token


def verify(root, docker, negative_only=False, checkpoint=None):
    docker.verify_inputs()
    topology = Topology(docker)
    db = DatabaseFixture(docker, topology.app_net, topology.uploads)
    db.start()
    topology.add_slot("BLUE", db)
    topology.add_slot("GREEN", db)
    http = topology.start_edge()
    key = secrets.token_bytes(48)
    probe = topology.probe()
    ready = {slot: {role: digest(probe(role, slot)) for role in ROLES} for slot in RELEASES}
    controller = Controller(StateStore(root / "state"), topology.relay, probe,
                            lambda proof, context: proof == context["identity_hash"], PlayerGate(key))
    cold = ColdStart(controller)
    cold.initialize(topology.baseline, RELEASES["BLUE"], ready["BLUE"], topology.baseline_hash)
    cold.recover()
    identities = {pair: docker.identity(container) for pair, container in topology.containers.items()}
    for token in db.player_tokens:
        heartbeat(http, token)
    baseline_ids = set(db.readonly("SELECT DISTINCT player_device_id FROM player_heartbeats"))
    assert baseline_ids == set(db.fixture["devices"])
    baseline_refs = sorted(device_ref(device, key) for device in baseline_ids)
    replacement = add_replacement(db)
    output = {}

    def exercise(label, tokens, success, with_playback=False, real_window=False):
        controller.prepare("GREEN", RELEASES["GREEN"])
        controller.start("GREEN")
        for role in ROLES:
            controller.register_ready(role, "GREEN", ready["GREEN"][role])
        # Refresh the same baseline from BLUE before each independent trial.
        if not real_window:
            for token in db.player_tokens:
                heartbeat(http, token)
        baseline = capture_baseline(db.readonly, controller.status(), key)
        assert baseline["payload"]["baseline_refs"] == baseline_refs
        capture = Capture(topology, 180 if real_window else 50)
        try:
            transition = controller.switch_api("GREEN", baseline)["routing"]["api_transition"]
            started = utc_now()
            for token in tokens:
                heartbeat(http, token)
            if with_playback:
                playback(http, db.player_tokens[0])
            if label == "MISSING_ONE":
                heartbeat(http, secrets.token_urlsafe(36), 401)
            # The positive production-length case uses one actual observation
            # window. Negative cases compress the idle tail, never the HTTP.
            deadline = monotonic() + (120.2 if real_window else 0)
            if real_window:
                print("PASSIVE_OBSERVER_QA_COLLECTING_120_SECONDS", flush=True)
            while monotonic() < deadline:
                sleep(min(0.2, max(0, deadline - monotonic())))
            wire = capture.collect()
            if real_window:
                # Use the collector's actual end, captured after kernel drop
                # accounting, as the exact end of the attested window.
                finished = from_ns(json.loads(wire[-1])["ended_ns"]).isoformat()
            else:
                finished = utc_now()
        finally:
            capture.close()
        if not real_window:
            ended = utc(started) + timedelta(seconds=120)
            # Negative QA advances only the tail after observing actual kernel
            # zero drops. There is no production path that can alter this clock.
            final = json.loads(wire[-1])
            assert final["type"] == "done" and final["kernel_dropped"] == 0
            final["ended_ns"] = max(final["ended_ns"], int(ended.timestamp() * 1_000_000_000) + 1_000_000)
            wire[-1] = json.dumps(final)
            finished = ended.isoformat()
            controller.player_gate = PlayerGate(key, now=lambda: ended)
        proof = adjudicate(wire, logs(topology), lambda pairs: read_only_query(db.readonly, pairs), baseline,
                           transition, key, started, finished)
        assert controller.player_gate(proof, transition) is success
        observed = {item["device_ref"] for item in proof["payload"]["heartbeats"]}
        if success:
            controller.switch_frontend("GREEN", proof)
            controller.rollback()
        else:
            try:
                controller.switch_frontend("GREEN", proof)
            except Stop as error:
                assert str(error) == "PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK"
            else:
                raise AssertionError("MISSING_DEVICE_PROMOTED")
        controller.player_gate = PlayerGate(key)
        assert controller.status()["routing"]["active"] == {"api": "BLUE", "frontend": "BLUE"}
        assert {pair: docker.identity(container) for pair, container in topology.containers.items()} == identities
        output[label] = {"gate": "PASS" if success else "FAIL_AS_REQUIRED", "api_rollback": "PASS",
                         "matched": len(observed & set(baseline_refs)), "new_devices": len(observed - set(baseline_refs)),
                         "playback": proof["payload"]["playback_evidence"],
                         "seconds": (utc(finished) - utc(started)).total_seconds(), "real_window": real_window}
        print(f"PASSIVE_OBSERVER_QA_{label}={json.dumps(output[label], sort_keys=True)}", flush=True)
        if checkpoint is not None:
            checkpoint(label, output[label])

    if not negative_only:
        exercise("ALL_BASELINE", db.player_tokens, True, with_playback=True, real_window=True)
    exercise("MISSING_ONE", db.player_tokens[:3], False)
    exercise("DUPLICATE_NEW_REPLACING_MISSING", [*db.player_tokens[:3], db.player_tokens[0], replacement], False)
    return {"result": "PASS", "scope": "DISPOSABLE_REAL_EDGE_AND_POSTGRES", "cases": output,
            "observer_generated_requests": 0, "pre_switch_heartbeat": "EXCLUDED", "raw_capture_persisted": False}


def main():
    import argparse

    if not __debug__:
        raise RuntimeError("Observer QA requires assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-only", action="store_true", help="Run isolated HTTP negative controls only")
    args = parser.parse_args()
    docker = LocalDocker()
    scratch = PROJECT_DIRECTORY / ".tmp"
    def checkpoint(label, summary):
        path = scratch / f"bgv2-passive-{docker.nonce}-{label.lower()}.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(summary, output, indent=2, sort_keys=True)
            output.write("\n")

    with tempfile.TemporaryDirectory(prefix="bgv2-passive-", dir=scratch) as directory:
        try:
            report = verify(Path(directory), docker, args.negative_only, checkpoint)
        finally:
            docker.cleanup()
    # Windows temp-file ACL semantics differ from POSIX. This report contains
    # only scenario metrics and no captured lines, credentials or identifiers.
    path = scratch / f"bgv2-passive-{secrets.token_hex(6)}.json"
    # The integration summary has no receipts, raw capture, IDs, tokens, or key.
    with path.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    print(json.dumps(report, sort_keys=True), flush=True)
    print(path)


if __name__ == "__main__":
    main()
