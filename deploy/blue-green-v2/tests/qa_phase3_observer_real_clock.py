"""Opt-in, offline QA with two independent real-time Phase 3 observation windows."""

from concurrent.futures import ThreadPoolExecutor
import json
from time import monotonic_ns, sleep, time_ns

from player_observer import from_ns
from test_phase3_player_windows import PlayerWindow, SECOND, UPSTREAM


def wait_until(deadline):
    while (remaining := deadline - monotonic_ns()) > 0:
        sleep(remaining / SECOND)


def observe(seed):
    # A process-local fixture emits sanitized observations and DB rows. The
    # actual wait and all UTC stamps come from the host, without clock patches.
    collector = time_ns()
    baseline = time_ns()
    window = PlayerWindow(seed, baseline, collector)
    opened = time_ns()
    start_mono, started = monotonic_ns(), time_ns()
    wait_until(start_mono + SECOND)
    for device in window.devices[:8]:
        window.event(time_ns(), device)
        sleep(0.004)
    wait_until(start_mono + 116 * SECOND)
    for device in window.devices[8:]:
        window.event(time_ns(), device)
        sleep(0.004)
    if seed % 2:
        window.event(time_ns(), endpoint="playback-events", status=201)
    wait_until(start_mono + 120 * SECOND + 250_000_000)
    finished, stop_mono = time_ns(), monotonic_ns()
    accepted, payload = window.decide(opened, started, finished, time_ns())
    monotonic_seconds = (stop_mono - start_mono) / SECOND
    utc_seconds = (from_ns(finished) - from_ns(started)).total_seconds()
    observed = {row["device_ref"] for row in payload["heartbeats"]}
    if not (accepted and payload["result"] == "PASS" and observed == window.refs(window.devices)
            and len(observed) == 17 and min(monotonic_seconds, utc_seconds) >= 120
            and (payload["generation"], payload["slot"], payload["upstream"]) == (0, "BLUE", UPSTREAM)
            and payload["playback_evidence"] == ("PASS" if seed % 2 else "UNVERIFIED")):
        raise AssertionError("real-clock Phase 3 continuity failed")
    return {"result": "PASS", "baseline_count": 17, "observed_count": len(observed),
            "elapsed_monotonic_seconds": monotonic_seconds, "elapsed_utc_seconds": utc_seconds,
            "playback": payload["playback_evidence"]}


def main():
    with ThreadPoolExecutor(max_workers=2) as pool:
        windows = list(pool.map(observe, (101, 102)))
    print(json.dumps({"windows": windows, "clock": "REAL_MONOTONIC_AND_UTC",
                      "traffic": "OFFLINE_DISPOSABLE_EVIDENCE_FIXTURE"}, sort_keys=True))


if __name__ == "__main__":
    main()
