"""Exercise Phase 3 adjudication through the real launcher on a local Linux Docker host."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from time import get_clock_info, monotonic_ns, sleep, time_ns

from player_observer_launcher import launch_observer_in_collector_time_namespace
from player_observer_monotonic import MonotonicWindow, _read_capture
from qa_docker import LocalDocker, RELEASES
from release_state import Stop, canonical, decode, write_private_atomic
from test_phase3_monotonic_window import MS, MonotonicFixture, SECOND


COLLECTOR_IMAGE = RELEASES["BLUE"]["images"]["api"]
OTHER_NAMESPACES = ("net", "mnt", "pid", "user", "uts", "cgroup")
CASES = ("ALL_BASELINE", "MISSING_ONE", "REPLACEMENT", "PRESWITCH", "INFLIGHT")
CLOCK_PROBE = """
import json, os, time
with open('/proc/self/timens_offsets', encoding='ascii') as stream:
    offsets = {name: [int(sec), int(ns)] for name, sec, ns in (line.split() for line in stream)}
print(json.dumps({'namespace': os.readlink('/proc/self/ns/time'), 'offsets': offsets,
                  'monotonic_ns': time.monotonic_ns(), 'utc_ns': time.time_ns()}))
"""


def read_clock(pid):
    proc = Path("/proc") / str(pid)
    offsets = {name: [int(seconds), int(nanoseconds)] for name, seconds, nanoseconds in
               (line.split() for line in (proc / "timens_offsets").read_text(encoding="ascii").splitlines())}
    return {"namespace": os.readlink(proc / "ns/time"), "offsets": offsets}


def other_namespaces():
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in OTHER_NAMESPACES}


def live_collector_clock(docker, collector):
    return decode(docker.read(collector, "python", "-I", "-B", "-c", CLOCK_PROBE))


def record(directory, name, result):
    write_private_atomic(directory / name, canonical(result), replace=False)


def prove_old_guard(host, collector):
    ready = collector["monotonic_ns"]
    window = MonotonicWindow(ready, ready + SECOND, ready + 121 * SECOND)
    frames = [canonical({"type": "ready", "started_ns": collector["utc_ns"]}),
              canonical({"type": "monotonic_clock", "ready_monotonic_ns": ready,
                         "deadline_monotonic_ns": ready + 165 * SECOND,
                         "time_namespace": collector["namespace"]})]
    try:
        _read_capture(frames, window, host["namespace"], [])
    except Stop as error:
        if str(error) == "PLAYER_MONOTONIC_CLOCK_INVALID":
            return str(error)
        raise
    raise AssertionError("The unjoined observer accepted the live collector namespace")


def wait_until(deadline):
    while (remaining := deadline - monotonic_ns()) > 0:
        sleep(remaining / SECOND)


def run_fixture(mode, index, collector):
    ready = live_collector_clock(LocalDocker(), collector)
    on_observer = read_clock("self")
    assert (on_observer["namespace"], on_observer["offsets"]) == (ready["namespace"], ready["offsets"])
    if mode == "PRESWITCH":
        # Pre-switch requests still occur after the actual collector is ready.
        wait_until(ready["monotonic_ns"] + 25 * MS)
    opened = monotonic_ns()
    started, started_utc = monotonic_ns(), time_ns()
    case = MonotonicFixture(3000 + index, opened=opened, started=started, ready=ready["monotonic_ns"],
                            started_utc=started_utc, ready_utc=ready["utc_ns"], time_namespace=ready["namespace"])
    expected = case.devices
    crossing = None
    elapsed = None
    if mode == "ALL_BASELINE":
        case.normal()
    elif mode == "MISSING_ONE":
        missing = index % len(case.devices)
        expected = case.devices[:missing] + case.devices[missing + 1:]
        case.normal(expected)
    elif mode == "REPLACEMENT":
        expected = case.devices[:-1]
        case.normal((*expected, case.fixture.replacement))
    elif mode == "PRESWITCH":
        expected = ()
        for position, device in enumerate(case.devices, 1):
            case.event(case.started - (18 - position) * MS, device, complete=case.started + position * MS)
    elif mode == "INFLIGHT":
        case.normal(case.devices[:-1])
        crossing = case.event(case.finished - 1 - index, case.devices[-1], complete=case.finished + 5 * MS)
    elif mode == "REAL_WINDOW":
        docker = LocalDocker()
        for device in case.devices[:-1]:
            request = live_collector_clock(docker, collector)
            completed = live_collector_clock(docker, collector)
            case.event(request["monotonic_ns"], device, complete=completed["monotonic_ns"],
                       started_utc=request["utc_ns"], completed_utc=completed["utc_ns"])
        wait_until(case.finished - 2 * SECOND)
        request = live_collector_clock(docker, collector)
        wait_until(case.finished + 25 * MS)
        completed = live_collector_clock(docker, collector)
        crossing = case.event(request["monotonic_ns"], case.devices[-1], complete=completed["monotonic_ns"],
                              started_utc=request["utc_ns"], completed_utc=completed["utc_ns"])
        elapsed = monotonic_ns() - case.started
        assert elapsed >= 120 * SECOND
        wait_until(case.deadline)
    else:
        raise AssertionError(f"Unexpected QA mode {mode}")

    audit = []
    accepted, payload = case.adjudicate(time_namespace=on_observer["namespace"], audit=audit)
    credited = case.credited(payload)
    assert (accepted, credited) == (len(expected) == 17, case.fixture.refs(expected))
    if mode == "PRESWITCH":
        assert not case.queries and all(row["FINAL_ADJUDICATION"] == "NO_CREDIT" for row in audit)
    if crossing is not None:
        admitted, = [row for row in audit if row["REQUEST_ID"] == crossing["trace"]]
        assert admitted["REQUEST_START_MONOTONIC_NS"] < case.finished <= admitted["RESPONSE_COMPLETE_MONOTONIC_NS"]
        assert admitted["REQUEST_ELIGIBLE"] is True and admitted["FINAL_ADJUDICATION"] == "CREDITED"
    return {"case": mode, "index": index, "result": payload["result"], "credited": len(credited),
            "observer_time_namespace": on_observer["namespace"], "other_namespaces": other_namespaces(),
            "window_elapsed_monotonic_ns": elapsed, "window_finish_monotonic_ns": case.finished,
            "window_start_monotonic_ns": case.started, "terminal_deadline_monotonic_ns": case.deadline}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--fixture", choices=(*CASES, "REAL_WINDOW"))
    parser.add_argument("--index", type=int)
    parser.add_argument("--collector")
    args = parser.parse_args()
    if not __debug__ or get_clock_info("monotonic").implementation != "clock_gettime(CLOCK_MONOTONIC)":
        raise RuntimeError("QA requires Linux CLOCK_MONOTONIC and assertions enabled")
    evidence = args.evidence_dir.resolve()
    if args.fixture is not None:
        result = run_fixture(args.fixture, args.index, args.collector)
        record(evidence, f"{args.index:03d}-player.json", result)
        return

    evidence.mkdir(mode=0o700)
    docker = LocalDocker()
    collector = docker.create_container("time-collector", COLLECTOR_IMAGE, "900", cpus="",
                                         args=("--network", "none", "--entrypoint", "/bin/sleep"))
    try:
        docker.run("start", collector)
        inspection = docker.inspect("container", collector)
        host = read_clock("self")
        collector_pid = inspection["State"]["Pid"]
        on_proc = read_clock(collector_pid)
        live = live_collector_clock(docker, collector)
        assert (inspection["Id"], inspection["Image"], inspection["RestartCount"]) == (collector, COLLECTOR_IMAGE, 0)
        assert inspection["State"]["Running"] is True and inspection["State"]["OOMKilled"] is False
        assert on_proc == {"namespace": live["namespace"], "offsets": live["offsets"]}
        assert host["namespace"] != live["namespace"], "QA must begin with different kernel time namespaces"
        old_guard = prove_old_guard(host, live)
        original_namespaces = other_namespaces()
        nsenter = subprocess.check_output(["nsenter", "--version"], text=True).splitlines()[0]
        record(evidence, "pre-fix.json", {"original_observer": host, "collector": live,
                                          "collector_host_pid": collector_pid, "old_guard": old_guard,
                                          "nsenter": nsenter})

        def launch(item):
            index, case = item
            proof = evidence / f"{index:03d}-launch.json"
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--evidence-dir", str(evidence),
                       "--fixture", case, "--index", str(index), "--collector", collector]
            assert launch_observer_in_collector_time_namespace(collector, COLLECTOR_IMAGE, 0, command, proof) == 0
            launch_proof = decode(proof.read_bytes())
            player = decode((evidence / f"{index:03d}-player.json").read_bytes())
            assert launch_proof["TIME_NAMESPACE_MATCH"] == "YES"
            assert launch_proof["COLLECTOR_HOST_PID"] == collector_pid
            assert launch_proof["COLLECTOR_STARTED_AT"] == inspection["State"]["StartedAt"]
            assert (launch_proof["COLLECTOR_TIME_NS"] == launch_proof["OBSERVER_TIME_NS"]
                    == player["observer_time_namespace"] == live["namespace"])
            assert launch_proof["COLLECTOR_TIMENS_OFFSETS"] == launch_proof["OBSERVER_TIMENS_OFFSETS"] == live["offsets"]
            assert player["other_namespaces"] == original_namespaces
            return player

        result = {"fixture": "REAL_LINUX_NAMESPACES_AND_CLOCKS_WITH_DISPOSABLE_PLAYER_EVIDENCE", "pre_fix": old_guard,
                  "time_namespace_only": True, "nsenter": nsenter, "collector_time_namespace": live["namespace"]}
        stress = [launch((index, case)) for index, case in enumerate(CASES * 20)]
        result["launch_stress"] = len(stress)
        result["player_cases"] = {case: {"passed_checks": sum(row["case"] == case for row in stress),
                                         "adjudication": next(row["result"] for row in stress if row["case"] == case)}
                                  for case in CASES}
        with ThreadPoolExecutor(max_workers=3) as pool:
            result["real_windows"] = list(pool.map(launch, ((index, "REAL_WINDOW") for index in range(100, 103))))
        record(evidence, "summary.json", result)
        print(json.dumps(result, sort_keys=True), flush=True)
    finally:
        docker.cleanup()


if __name__ == "__main__":
    main()
