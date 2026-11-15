"""Local Docker rehearsal of the prepared handoff, using the existing R3 stand-ins."""

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
from types import SimpleNamespace

import qa_initial_handoff as original
from gates import PlayerGate, ReadyGate
from qa_docker import LocalDocker, PROJECT_DIRECTORY
from qa_player_observer import Capture
from release_state import Stop, now
from routectl import Controller


ATTEMPTS = 30
BATCH_SIZE = 10
MIN_DOWNTIMES = 20
DEATH_EXIT = 73

TIMED_PHASES = {
    "prepare": ("T_PREPARE_BEGIN", "T_PREPARE_COMPLETE"),
    "final_revalidation": ("T_FINAL_REVALIDATION_BEGIN", "T_FINAL_REVALIDATION_COMPLETE"),
    "downtime": ("T_FRONTEND_STOP", "T_FIRST_HTTP_200"),
}
DOWNTIME_SEGMENTS = (
    ("STOP_TO_PORTS", "T_FRONTEND_STOP", "T_PORTS_FREE"),
    ("PORTS_TO_PROXY", "T_PORTS_FREE", "T_PROXY_READY"),
    ("PROXY_TO_TLS", "T_PROXY_READY", "T_TLS_READY"),
    ("TLS_TO_CONFIG_PATCH", "T_TLS_READY", "T_CONFIG_PATCH_BEGIN"),
    ("CONFIG_PATCH", "T_CONFIG_PATCH_BEGIN", "T_CONFIG_PATCH_COMPLETE"),
    ("PATCH_TO_EFFECTIVE_VERIFICATION", "T_CONFIG_PATCH_COMPLETE", "T_EFFECTIVE_CONFIG_VERIFIED"),
    ("VERIFICATION_TO_INGRESS", "T_EFFECTIVE_CONFIG_VERIFIED", "T_INGRESS_READY"),
    ("INGRESS_TO_FIRST_HTTP", "T_INGRESS_READY", "T_FIRST_HTTP_200"),
)


def failure(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    result = {"class": type(exc).__name__}
    if frames:
        last = frames[-1]
        result["source"] = f"{Path(last.filename).name}:{last.lineno}:{last.name}"
    message = str(exc)
    if isinstance(exc, Stop) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,90}", message):
        result["reason"] = message
    elif message == "QA HTTPS GET failed with transport RC 28":
        result["reason"] = "QA_HTTPS_GET_RC28"
    return result


def prepared_flow(fixture):
    from handoff_topology import HandoffTopology
    from prepared_handoff import PreparedHandoff

    docker = fixture.docker
    topology = HandoffTopology(
        lambda *args: json.loads(docker.run(*args)),
        historical_id=fixture.old, backend_id=fixture.api, frontend_id=fixture.frontend,
        edge_id=fixture.edge, postgres_id=fixture.db.pg, proxy_network=fixture.proxy,
        ingress_network=fixture.ingress, tls_volume=fixture.tls,
        list_running=lambda: docker.run("ps", "-q", "--no-trunc").splitlines(),
    )
    return PreparedHandoff(fixture.pre_controller, topology)


def move(fixture, mark, capture=None):
    """Perform the same fixture-only physical transfer as the old rehearsal."""
    docker = fixture.docker
    mark("T_FRONTEND_STOP")
    docker.run("stop", "--time", "3", fixture.old)
    original.release_endpoint(docker, fixture.proxy, fixture.old, original.PROXY_IP)
    original.release_endpoint(docker, fixture.ingress, fixture.old, original.PUBLIC_IP)
    original.no_host_port_owner(fixture.ports)
    mark("T_PORTS_FREE")
    original.release_endpoint(docker, fixture.proxy, fixture.edge, original.PREVIEW_IP)
    original.connect(docker, fixture.proxy, fixture.edge, original.PROXY_IP)
    assert original.networks(docker, fixture.edge)[fixture.proxy][0] == original.PROXY_IP
    mark("T_PROXY_READY")
    original.connect(docker, fixture.ingress, fixture.edge, original.PUBLIC_IP)
    assert original.published(docker, fixture.edge) == fixture.ports
    mark("T_EDGE_OWNS_PORTS")
    assert not docker.inspect("container", fixture.old)["State"]["Running"]
    original.assert_tls_mount(docker, fixture.tls, fixture.edge)
    mark("T_TLS_READY")
    if capture is not None:
        capture.start()
        mark("T_PLAYER_CAPTURE_READY")


def public_checks(fixture, edge_identity):
    docker = fixture.docker
    assert docker.identity(fixture.edge) == edge_identity
    assert original.service_smoke(docker, fixture.edge_http, original.PUBLIC_IP) == fixture.html
    original.trusted_headers(docker, fixture.edge_http, fixture.api, fixture.client_ip)
    assert original.cert_hashes(docker, fixture.edge) == fixture.tls_fingerprint
    original.assert_tls_mount(docker, fixture.tls, fixture.edge)
    assert fixture.controller.status()["routing"]["active"] == {"api": "BLUE", "frontend": "BLUE"}
    assert fixture.post_probe.classify_legacy_blue()[0] == "LIVE_BACKEND_IS_ADOPTED_LEGACY_BLUE_EXACT"


def elapsed(timeline, begin, end):
    if begin in timeline.monotonic_ns and end in timeline.monotonic_ns:
        return timeline.elapsed(begin, end)
    return None


def rollback(fixture, row):
    timeline = original.Timeline()
    try:
        original.reverse(fixture, timeline)
        assert elapsed(timeline, "T_ROLLBACK_DECISION", "T_FIRST_R3_HTTP_200") <= original.HANDOFF_LIMIT_SECONDS
        row["rollback"] = "PASS"
    except Exception as exc:
        row["rollback"] = "FAIL"
        row["rollback_error"] = failure(exc)
    finally:
        row["rollback_timeline"] = timeline.receipt()
        row["rollback_seconds"] = elapsed(timeline, "T_ROLLBACK_DECISION", "T_FIRST_R3_HTTP_200")


def attempt(fixture, row):
    timeline = original.Timeline()
    capture = None
    row["handoff"] = "PRE_DOWNTIME_STOP"
    try:
        original.prepare_trial(fixture)
        edge_identity = fixture.docker.identity(fixture.edge)
        capture = Capture(
            SimpleNamespace(docker=fixture.docker, edge=fixture.edge, bindings={"api:BLUE": fixture.binding("api")}),
            30, slot="BLUE", defer=True,
        )
        flow = prepared_flow(fixture)
        prepared = flow.prepare(mark=timeline.mark)
        row["prepared_sha256"] = prepared["sha256"]
        flow.execute(prepared, expected_sha256=prepared["sha256"],
                     move=lambda: move(fixture, timeline.mark, capture), mark=timeline.mark)
        original.first_http(fixture.published_http, edge=True)
        timeline.mark("T_FIRST_HTTP_200")
        public_checks(fixture, edge_identity)
        assert elapsed(timeline, *TIMED_PHASES["downtime"]) <= original.HANDOFF_LIMIT_SECONDS, "DOWNTIME_OVER_30S"
        row["handoff"] = "PASS"
    except Exception as exc:
        row["handoff"] = "FAIL" if "T_FRONTEND_STOP" in timeline.monotonic_ns else "PRE_DOWNTIME_STOP"
        row["error"] = failure(exc)
    finally:
        row["entered_downtime"] = "T_FRONTEND_STOP" in timeline.monotonic_ns
        if row["entered_downtime"]:
            rollback(fixture, row)
        else:
            try:
                assert original.published(fixture.docker, fixture.old) == fixture.ports
                original.first_http(fixture.published_http, edge=False)
                row["historical_still_serving"] = True
            except Exception as exc:
                row["historical_still_serving"] = False
                row["historical_check_error"] = failure(exc)
        row["timeline"] = timeline.receipt()
        row["duration_seconds"] = {name: elapsed(timeline, *ends) for name, ends in TIMED_PHASES.items()}
        row["downtime_segments"] = {name: elapsed(timeline, begin, end) for name, begin, end in DOWNTIME_SEGMENTS}
        if capture is not None:
            try:
                capture.close()
                fixture.docker.run("stop", "--time", "1", capture.container)
            except Exception as exc:
                row["fixture_cleanup_error"] = failure(exc)


def killed_writer(fixture, prepared, sender):
    """Exit under the actual writer lock after verifying the effective configuration."""
    def mark(name):
        sender.send({"mark": name, "at": now()})
        if name == "T_EFFECTIVE_CONFIG_VERIFIED":
            sender.close()
            os._exit(DEATH_EXIT)

    try:
        fixture.pre_controller = Controller(fixture.store, fixture.wire, fixture.pre_probe,
                                            ReadyGate(fixture.key), PlayerGate(fixture.key))
        prepared_flow(fixture).execute(prepared, expected_sha256=prepared["sha256"],
                                       move=lambda: move(fixture, mark), mark=mark)
    except Exception as exc:
        sender.send({"error": failure(exc)})
    finally:
        sender.close()


def process_death_check(fixture):
    """Exercise real process death, fail-closed Edge and unchanged historical reversal."""
    original.prepare_trial(fixture)
    prepared = prepared_flow(fixture).prepare(mark=lambda _name: None)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    # Construct the gates inside the child since their default clocks are lambdas.
    child_fixture = SimpleNamespace(
        docker=fixture.docker, old=fixture.old, api=fixture.api, frontend=fixture.frontend, edge=fixture.edge,
        db=SimpleNamespace(pg=fixture.db.pg), proxy=fixture.proxy, ingress=fixture.ingress, tls=fixture.tls,
        ports=fixture.ports, store=fixture.store, wire=fixture.wire, pre_probe=fixture.pre_probe, key=fixture.key,
    )
    child = context.Process(target=killed_writer, args=(child_fixture, prepared, sender))
    child.start()
    sender.close()
    marks = []
    row = {"record": "writer_death_negative", "result": "FAIL", "child_marks": marks}
    try:
        child.join(timeout=60)
        if child.is_alive():
            child.terminate()
            child.join(timeout=8)
            raise RuntimeError("DISPOSABLE_WRITER_DEATH_TEST_TIMEOUT")
        while True:
            try:
                if not receiver.poll():
                    break
                marks.append(receiver.recv())
            except (EOFError, BrokenPipeError):
                # Win32 reports a closed pipe from poll() after the writer
                # exits. The exit code and exact terminal mark still must match.
                break
        row["child_exit_code"] = child.exitcode
        names = [entry.get("mark") for entry in marks]
        assert child.exitcode == DEATH_EXIT and names[-1] == "T_EFFECTIVE_CONFIG_VERIFIED"
        assert "T_FRONTEND_STOP" in names and "T_INGRESS_READY" not in names
        assert not fixture.wire.ingress_open()
        fixture.published_http.request(original.API_HOST, "/", timeout=2).require(503)
        row["closed_http_status"] = 503
    except Exception as exc:
        row["error"] = failure(exc)
    finally:
        try:
            names = [entry.get("mark") for entry in marks]
            old_running = fixture.docker.inspect("container", fixture.old)["State"]["Running"]
            if "T_FRONTEND_STOP" in names or not old_running:
                rollback(fixture, row)
        finally:
            receiver.close()
            child.close()
    if "error" not in row and row.get("rollback") == "PASS":
        row["result"] = "PASS"
    return row


def report_summary(rows, wanted, negative):
    entered = [row for row in rows if row["entered_downtime"]]
    duration_stats = {
        name: original.summarize([row["duration_seconds"][name] for row in rows
                                  if row.get("duration_seconds", {}).get(name) is not None])
        for name in TIMED_PHASES
    }
    longest = max((row for row in entered if row["duration_seconds"]["downtime"] is not None),
                  key=lambda row: row["duration_seconds"]["downtime"], default=None)
    slowest = None
    if longest is not None:
        segments = {key: value for key, value in longest["downtime_segments"].items() if value is not None}
        if segments:
            key = max(segments, key=segments.get)
            slowest = {"attempt": longest["attempt"], "segment": key, "seconds": segments[key]}
    correct = sum(row["handoff"] == row.get("rollback") == "PASS" and "fixture_cleanup_error" not in row
                  for row in entered)
    over = sum((row["duration_seconds"]["downtime"] or 0) > original.HANDOFF_LIMIT_SECONDS for row in entered)
    passed = (len(rows) == wanted == ATTEMPTS and len(entered) >= MIN_DOWNTIMES and correct == len(entered)
              and over == 0 and negative is not None and negative["result"] == "PASS")
    return {
        "record": "summary", "result": "PASS" if passed else "FAIL", "attempts": len(rows),
        "pre_downtime_stops": len(rows) - len(entered), "entered_downtime": len(entered),
        "handoff_and_rollback_pass": correct, "downtime_over_30s": over,
        "durations_seconds": duration_stats, "slowest_downtime_segment": slowest,
        "writer_death_negative": negative and negative["result"],
    }


def run(wanted, output):
    rows = []
    negative = None
    batch = None

    def record(value):
        out = json.dumps(value, sort_keys=True)
        output.write(out + "\n")
        output.flush()
        os.fsync(output.fileno())
        print(out, flush=True)

    def start_fixture():
        scratch = tempfile.TemporaryDirectory(prefix="bgv26-r2-fixture-", dir=PROJECT_DIRECTORY / ".tmp")
        docker = LocalDocker()
        return scratch, docker

    def close_fixture():
        nonlocal batch
        if batch is not None:
            scratch, docker = batch
            batch = None
            try:
                docker.cleanup()
                record({"record": "owned_cleanup", "status": "PASS", "owner": docker.nonce})
            finally:
                scratch.cleanup()

    try:
        fixture = None
        for index in range(1, wanted + 1):
            row = {"record": "attempt", "attempt": index, "entered_downtime": False}
            if fixture is None:
                close_fixture()
                batch = start_fixture()
                scratch, docker = batch
                row["fixture_preflight_begin"] = now()
                try:
                    fixture = original.Handoff(docker, Path(scratch.name))
                    original.prepare(fixture)
                except Exception as exc:
                    row.update(handoff="PRE_DOWNTIME_STOP", error=failure(exc), source="fixture_preflight",
                               fixture_preflight_end=now())
                    fixture = None
                else:
                    row["fixture_preflight_end"] = now()
            if fixture is not None:
                attempt(fixture, row)
            record(row)
            rows.append(row)
            if (fixture is not None and row["handoff"] == row.get("rollback") == "PASS" and negative is None):
                try:
                    negative = process_death_check(fixture)
                except Exception as exc:
                    negative = {"record": "writer_death_negative", "result": "FAIL", "error": failure(exc)}
                record(negative)
                if negative["result"] != "PASS":
                    fixture = None
            if row.get("rollback") == "FAIL" or row.get("fixture_cleanup_error") or index % BATCH_SIZE == 0:
                fixture = None
    finally:
        close_fixture()
    summary = report_summary(rows, wanted, negative)
    record(summary)
    return summary


def main():
    assert __debug__, "QA requires assertions"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=ATTEMPTS)
    args = parser.parse_args()
    if not 1 <= args.attempts <= ATTEMPTS:
        parser.error(f"--attempts must be from 1 to {ATTEMPTS}")
    scratch = PROJECT_DIRECTORY / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="bgv26-r2-handoff-",
                                     suffix=".jsonl", dir=scratch, delete=False) as out:
        print("EVIDENCE=" + out.name, flush=True)
        result = run(args.attempts, out)
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
