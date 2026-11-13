"""Opt-in, fully disposable Blue-Green V2 Docker integration QA.

Run from this repository with python -B deploy/blue-green-v2/tests/qa_integration.py.
The runner never pulls/builds images and only addresses a fixed local Engine.
"""

import concurrent.futures
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from qa_docker import DatabaseFixture, LocalDocker, PROJECT_DIRECTORY, RELEASES, wait_until
from qa_capacity import observe_capacity
from qa_http import utc_now
from qa_product import ProductQA
from qa_proxy_headers import verify_proxy_headers
from qa_topology import Topology

from gates import PlayerGate, ReadyGate, attribute_heartbeats, device_ref, digest, sign_receipt
from release_state import StateStore, Stop
from routectl import Controller


BLUE = {"api": "BLUE", "frontend": "BLUE"}
MIXED = {"api": "GREEN", "frontend": "BLUE"}
GREEN = {"api": "GREEN", "frontend": "GREEN"}
QA_CONTRACTS = {
    "BLUE": (".tmp/BLUEGREEN_4D320B4_R2_1_OBS5/QA_OBSERVABILITY_REPORT.json",
             "a33006276d4c4b4d73c2aab24b41dff313d3e30a2f2e9d1649c66877dc67205c"),
    "GREEN": (".tmp/BLUEGREEN_F348076_R2_1_OBS6_4/QA_OBSERVABILITY_REPORT.json",
              "8aa9b51f758a815548d463d936901d4ba55e4cb22402da5cd3194202381ed94c"),
}


def expect_stop(code, operation):
    try:
        operation()
    except Stop as error:
        assert str(error) == code, str(error)
    else:
        raise AssertionError("Expected failure " + code)


def contract_evidence():
    """Reuse the sealed product-platform tests for the exact immutable images."""
    result = {}
    for slot, (name, expected) in QA_CONTRACTS.items():
        source = Path(os.getenv(f"BGV2_QA_{slot}_CONTRACT_REPORT", PROJECT_DIRECTORY / name))
        raw = source.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        assert actual == expected, "Unexpected sealed contract report digest"
        data = json.loads(raw)["image_qa"]
        release = RELEASES[slot]
        assert (data["commit"], data["tree"]) == (release["revision"], release["tree"])
        assert data["images"] == {"backend": release["images"]["api"], "frontend": release["images"]["frontend"]}
        assert data["result"] == "PASS" and data["run_count"] == len(data["runs"]) == 3
        for run in data["runs"]:
            for contract in ("windows_legacy", "android_normal", "android_node_slice"):
                assert run["automated_platform_contracts"][contract]["automated_contract"] == "PASS"
            mixed = run["mixed_pools"]
            assert mixed and all(item["checkout"] == item["checkin"] for item in mixed)
        result[slot] = actual
    return result


def ready_receipts(qa, topology, key):
    probe = topology.probe()
    healthy = utc_now()
    contracts = contract_evidence()
    qa.results["sealed_contract_receipt_sha256"] = contracts
    qa.results["isolated_pool_instrumentation"] = "BALANCED_SEALED_EVIDENCE_ONLY"
    receipts = {}
    for slot in RELEASES:
        before = {role: probe(role, slot) for role in ("api", "frontend")}
        probe.frontend_listener(slot, before["frontend"])
        roles = {
            "api": {
                **qa.api(slot), "dual_slot_schema": True, "media_cross_slot": True,
                "dual_slot_capacity": True, "pool": "BALANCED",
                "contracts": {"windows_legacy": True, "android_normal": True, "android_node_slice": True},
            },
            "frontend": qa.frontend(slot, route=slot),
        }
        for role, checks in roles.items():
            identity = probe(role, slot)
            assert identity == before[role], "QA slot process changed during readiness"
            receipts[role, slot] = sign_receipt("READY", {
                "slot": slot, "role": role, "release": RELEASES[slot],
                "identity_hash": digest(identity), "started_at": identity["started_at"],
                "healthy_at": healthy, "ready_at": utc_now(), "private_route": True, "checks": checks,
            }, key)
        print(f"QA {slot}: private TLS, channels, runtime 36+16, frontend assets and process identity PASS.",
              file=sys.stderr, flush=True)
    return receipts


def prepare_green(controller, receipts):
    controller.prepare("GREEN", RELEASES["GREEN"])
    controller.start("GREEN")
    for role in GREEN:
        controller.register_ready(role, "GREEN", receipts[role, "GREEN"])


def baseline_receipt(controller, database, key):
    identifiers = database.readonly(
        "SELECT DISTINCT player_device_id FROM player_heartbeats "
        "WHERE received_at >= NOW() - INTERVAL '120 seconds' ORDER BY player_device_id")
    assert set(identifiers) == set(database.fixture["devices"])
    state = controller.status()
    return sign_receipt("BASELINE", {
        "slot": "GREEN", "prior_slot": "BLUE", "previous_generation": state["routing"]["generation"],
        "identity_hash": state["slots"]["GREEN"]["ready"]["api"]["identity_hash"],
        "release": RELEASES["GREEN"], "baseline_at": utc_now(),
        "baseline_refs": sorted(device_ref(device, key) for device in identifiers),
    }, key)


def access_logs(topology):
    result = topology.docker.invoke("logs", topology.edge)
    assert result.returncode == 0
    entries = []
    for line in (result.stdout + result.stderr).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("logger", "").startswith("http.log.access"):
            entries.append(value)
    return entries


def player_evidence(qa, transition, started, completed, acks, playback_trace, key, baseline):
    access = access_logs(qa.topology)
    generation = transition["generation"]
    observations = [row for row in access if row.get("ingress") == "public"
                    and row.get("route_generation") == str(generation)
                    and row.get("endpoint") in {"heartbeat", "playback-events"}]
    by_trace = {row["request_id"]: row for row in observations}
    assert len(by_trace) == len(observations) and set(by_trace) == {playback_trace, *(ack["trace"] for ack in acks)}
    assert all("request" not in row and "resp_headers" not in row for row in observations)
    replies = []
    for ack in acks:
        row = by_trace[ack["trace"]]
        assert row["upstream_status"] == 200 and row["upstream_headers_ms"] >= 0
        assert int(row["received_at_ms"]) > 0 and row["duration"] >= 0
        replies.append({**ack, "generation": int(row["route_generation"]), "slot": "GREEN",
                        "upstream": row["upstream"], "status": row["status"], "public": True,
                        "path_class": row["endpoint"]})
    playback = by_trace[playback_trace]
    assert playback["upstream"] == "backend-green:8000" and playback["upstream_status"] == playback["status"] == 201
    raw_rows, = qa.database.readonly(
        "SELECT json_agg(json_build_object('channel_id',channel_id,'player_device_id',"
        f"player_device_id,'received_at',received_at)) FROM player_heartbeats WHERE received_at >= '{started}'")
    database_rows = json.loads(raw_rows)
    proofs = attribute_heartbeats(replies, database_rows, key, generation, "GREEN", "backend-green:8000")
    assert len(proofs) == len(acks)
    return sign_receipt("PLAYER", {
        **transition, **{field: baseline["payload"][field] for field in ("baseline_at", "baseline_refs")},
        "started_at": started, "finished_at": completed, "heartbeats": proofs, "source": "EDGE_ACK_EXACT_DB_JOIN",
        "access_complete": True, "http_409": 0, "http_5xx": 0, "incompatible": 0,
        "playback_201": 1, "playback_evidence": "PASS",
    }, key)


@contextmanager
def locked_channels(database):
    docker = database.docker
    command = [*docker.command, "exec", "-i", database.pg, "psql", "-XAt", "-q", "-v",
               "ON_ERROR_STOP=1", "-U", "bgv2qa", "-d", database.db_name]
    child = subprocess.Popen(command, env=docker.env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        child.stdin.write("BEGIN; LOCK TABLE channels IN ACCESS EXCLUSIVE MODE; SELECT 'BGV2_LOCKED';\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "BGV2_LOCKED"
        yield
    finally:
        child.stdin.write("ROLLBACK;\n")
        child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()


def rollback_with_inflight(controller, qa):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
        with locked_channels(qa.database):
            slow = workers.submit(qa.read, "/api/channels/", headers=qa.admin, timeout=30)
            wait_until(lambda: int(qa.database.query(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"
            )) > 0, timeout=5)
            assert not slow.done()
            reverted = controller.rollback()
            assert reverted["routing"]["active"] == BLUE
            assert qa.read("/").require(200).json()["name"] == "BGV2 QA BLUE"
            assert not slow.done(), "The old request terminated before releasing the fixture DB lock"
        return slow.result().require(200).headers["x-request-id"]


def verify_inflight(qa, trace, generation):
    entries = [row for row in access_logs(qa.topology) if row.get("request_id") == trace]
    assert len(entries) == 1 and entries[0]["upstream"] == "backend-green:8000"
    assert entries[0]["route_generation"] == str(generation)
    return {"completed_status": entries[0]["status"], "retained_original_slot": "GREEN"}


def verify_frontend_target(qa, slot, generation):
    entries = [row for row in access_logs(qa.topology) if row.get("request_id") == qa.public_frontend_trace]
    assert len(entries) == 1 and entries[0]["upstream"] == f"frontend-{slot.lower()}:80"
    assert entries[0]["route_generation"] == str(generation) and entries[0]["upstream_status"] == 200


def verify_missing_player(controller, qa, receipts, key, initial):
    # Re-qualify the same processes in the synthetic fixture after the first
    # rollback. Suppress one known device after a fresh real routing change.
    prepare_green(controller, receipts)
    for device in range(4):
        qa.heartbeat(device)
    baseline = baseline_receipt(controller, qa.database, key)
    split = controller.switch_api("GREEN", baseline)
    assert split["routing"]["active"] == MIXED
    transition = split["routing"]["api_transition"]
    started = utc_now()
    acks = [qa.heartbeat(device) for device in range(3)]
    playback = qa.playback(0)
    simulated_end = datetime.fromisoformat(started) + timedelta(seconds=120)
    proof = player_evidence(qa, transition, started, simulated_end.isoformat(), acks, playback, key, baseline)
    previous_gate = controller.player_gate
    controller.player_gate = PlayerGate(key, now=lambda: simulated_end)
    try:
        actual = proof["payload"]
        missing, = set(actual["baseline_refs"]) - {item["device_ref"] for item in actual["heartbeats"]}
        control = {**actual, "baseline_refs": [ref for ref in actual["baseline_refs"] if ref != missing]}
        control["baseline_hash"] = PlayerGate.baseline_hash(control)
        control_context = {**transition, "baseline_hash": control["baseline_hash"]}
        assert controller.player_gate(sign_receipt("PLAYER", control, key), control_context)
        expect_stop("PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK",
                    lambda: controller.switch_frontend("GREEN", proof))
    finally:
        controller.player_gate = previous_gate
    assert controller.status()["routing"]["active"] == BLUE
    assert qa.read("/").require(200).json()["name"] == "BGV2 QA BLUE"
    same_identities(qa.topology, initial)
    return {"baseline_devices": 4, "attributed_devices": 3, "frontend": "BLOCKED",
            "api_rollback": "PASS", "clock": "SIMULATED_120_S", "traffic": "REAL_DISPOSABLE_HTTP"}


def same_identities(topology, expected):
    observed = {key: topology.docker.identity(identity) for key, identity in topology.containers.items()}
    assert observed == expected


def check_log_privacy(qa):
    marker = "bgv2-qa-redaction-" + secrets.token_hex(6)
    response = qa.read("/?token=" + marker, headers={"Authorization": "Bearer " + marker,
                       "Cookie": "qa=" + marker, "X-Request-ID": marker}).require(200)
    assert response.headers["x-request-id"] != marker
    result = qa.topology.docker.invoke("logs", qa.topology.edge)
    raw = result.stdout + result.stderr
    assert marker.encode() not in raw and qa.token.encode() not in raw
    assert all(token.encode() not in raw for token in qa.database.player_tokens)
    return True


def save_evidence(result, suffix):
    path = PROJECT_DIRECTORY / ".tmp" / f"bgv2-phase2-{secrets.token_hex(6)}-{suffix}.json"
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as output:
        json.dump(result, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return path


def verify_routes(root):
    from edge_boot import ColdStart

    if not __debug__:
        raise RuntimeError("Disposable QA requires Python assertions enabled")
    docker = LocalDocker()
    result = docker.verify_inputs()
    qa = None
    phase = "topology"
    try:
        topology = Topology(docker)
        database = DatabaseFixture(docker, topology.app_net, topology.uploads)
        database.start()
        schema = database.signature()
        topology.add_slot("BLUE", database)
        topology.add_slot("GREEN", database)
        http = topology.start_edge()
        qa = ProductQA(topology, database, http)
        phase = "preflight"
        topology.check_isolation()
        qa.login(direct_slot="BLUE")
        qa.verify_media(qa.upload(direct_slot="BLUE"))
        qa.verify_media(qa.upload(direct_slot="GREEN"))
        qa.collect_connections()
        assert database.signature() == schema and database.index_proof() == database.index
        key = secrets.token_bytes(48)
        capacity = {}
        phase = "private_readiness"
        with observe_capacity(topology, database, capacity):
            receipts = ready_receipts(qa, topology, key)
        qa.results["capacity_load"] = capacity
        qa.results["asset_rollback_parity"] = qa.asset_rollback_parity()
        initial = {key: docker.identity(value) for key, value in topology.containers.items()}
        gate = PlayerGate(key)
        phase = "api_switch"
        controller = Controller(StateStore(root / "state"), topology.relay, topology.probe(), ReadyGate(key), gate)
        startup = ColdStart(controller)
        startup.initialize(topology.baseline, RELEASES["BLUE"], {role: receipts[role, "BLUE"] for role in BLUE},
                           topology.baseline_hash)
        startup.recover()
        assert controller.status()["routing"]["active"] == BLUE
        qa.preflight()
        qa.cors()
        qa.results["proxy_headers"] = verify_proxy_headers(topology, http)
        prepare_green(controller, receipts)
        for device in range(4):
            qa.heartbeat(device)
        baseline = baseline_receipt(controller, database, key)
        with http.websocket(qa.token, database.fixture["client"], topology.linux_client) as prior:
            prior.ack()
            split = controller.switch_api("GREEN", baseline)
            assert split["routing"]["active"] == MIXED
            transition = split["routing"]["api_transition"]
            start_mono = time.monotonic()
            started = utc_now()
            qa.results["ws_after_api_switch"] = prior.after_reload()
        assert qa.read("/").require(200).json()["name"] == "BGV2 QA GREEN"
        same_identities(topology, initial)
        with http.websocket(qa.token, database.fixture["client"], topology.linux_client) as live:
            live.ack()
        acks = [qa.heartbeat(device) for device in range(4)]
        playback = qa.playback(0)
        qa.verify_media(qa.upload())
        qa.collect_connections()
        qa.results["edge_log_redaction"] = check_log_privacy(qa)
        phase = "player_observation"
        # Confirm the actual log shape/ACK join before waiting out the window.
        player_evidence(qa, transition, started, utc_now(), acks, playback, key, baseline)
        print("QA checks passed through API switch and GREEN Player attribution. Observing the full 120 s window.",
              file=sys.stderr, flush=True)
        deadline = start_mono + 120.2
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(1, remaining))
        proof = player_evidence(qa, transition, started, utc_now(), acks, playback, key, baseline)
        assert gate(proof, transition)
        qa.results["player_attribution"] = {
            "matched": len(acks), "playback_201": 1,
            "window_seconds": (datetime.fromisoformat(proof["payload"]["finished_at"]) -
                               datetime.fromisoformat(started)).total_seconds(),
            "scope": "SYNTHETIC_QA_ONLY",
        }
        phase = "frontend_switch"
        with http.websocket(qa.token, database.fixture["client"], topology.linux_client) as prior:
            prior.ack()
            switched = controller.switch_frontend("GREEN", proof)
            assert switched["routing"]["active"] == GREEN
            qa.results["ws_after_frontend_switch"] = prior.after_reload()
        qa.frontend("GREEN")
        verify_frontend_target(qa, "GREEN", switched["routing"]["generation"])
        assert switched["routing"]["generation"] > transition["generation"]
        same_identities(topology, initial)
        phase = "edge_restart"
        docker.run("restart", topology.edge)
        topology.wait_bootstrap()
        startup.recover()
        topology.wait_edge()
        assert controller.reconcile()["result"] == "IN_SYNC"
        assert qa.read("/").require(200).json()["name"] == "BGV2 QA GREEN"
        same_identities(topology, initial)
        qa.results["edge_restart"] = "PRESERVED_GENERATION"
        phase = "rollback"
        old_request = rollback_with_inflight(controller, qa)
        qa.results["inflight_rollback"] = verify_inflight(qa, old_request, switched["routing"]["generation"])
        qa.frontend("BLUE")
        verify_frontend_target(qa, "BLUE", controller.status()["routing"]["generation"])
        same_identities(topology, initial)
        qa.results["normal_route_generations"] = {
            "api": transition["generation"], "frontend": switched["routing"]["generation"],
            "rollback": controller.status()["routing"]["generation"],
        }
        phase = "missing_player_rollback"
        qa.results["missing_player_rollback"] = verify_missing_player(controller, qa, receipts, key, initial)
        assert database.index_proof() == database.index and database.signature() == schema
        assert database.identity == docker.identity(database.pg)
        qa.results.update(result="PASS", api_switch="PASS", api_rollback="PASS", frontend_switch="PASS",
                          frontend_rollback="PASS", same_process="PASS", postgres_unchanged="PASS",
                          media_cross_slot="PASS")
        result.update(qa.results)
        return result
    except BaseException as error:
        result.update(qa.results if qa is not None else {})
        result.update(result="FAIL", stopped_phase=phase, error_class=type(error).__name__)
        print("Sanitized QA failure evidence: " + str(save_evidence(result, "failure")), flush=True)
        raise
    finally:
        docker.cleanup()


def main():
    scratch = PROJECT_DIRECTORY / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bgv2-phase2-", dir=scratch) as directory:
        result = verify_routes(Path(directory))
        path = save_evidence(result, "pass")
        print("Sanitized QA evidence: " + str(path), file=sys.stderr, flush=True)
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
