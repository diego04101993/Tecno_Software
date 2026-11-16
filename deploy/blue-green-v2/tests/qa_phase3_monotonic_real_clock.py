"""Opt-in real-clock fixture QA for Phase 3 admission and in-flight drain."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from time import get_clock_info, monotonic_ns, sleep, time_ns

from test_phase3_monotonic_window import MS, MonotonicFixture, SECOND


def wait_until(deadline):
    while (remaining := deadline - monotonic_ns()) > 0:
        sleep(remaining / SECOND)


def observe(index, namespace):
    ready, ready_utc = monotonic_ns(), time_ns()
    opened = monotonic_ns()
    started, started_utc = monotonic_ns(), time_ns()
    case = MonotonicFixture(2000 + index, opened=opened, started=started, ready=ready,
                            started_utc=started_utc, ready_utc=ready_utc, time_namespace=namespace)
    for device in case.devices[:-1]:
        request, requested_utc = monotonic_ns(), time_ns()
        sleep(0.004)
        completion, completed_utc = monotonic_ns(), time_ns()
        case.event(request, device, complete=completion, started_utc=requested_utc, completed_utc=completed_utc)

    wait_until(case.finished - SECOND)
    request, requested_utc = monotonic_ns(), time_ns()
    crossing = index % 2 == 0
    if crossing:
        wait_until(case.finished + 250 * MS)
    else:
        sleep(0.01)
    completion, completed_utc = monotonic_ns(), time_ns()
    case.event(request, case.devices[-1], complete=completion, started_utc=requested_utc, completed_utc=completed_utc)
    wait_until(case.finished)
    elapsed = monotonic_ns() - started
    audit = []
    accepted, data = case.adjudicate(audit=audit)
    if not (elapsed >= 120 * SECOND and request < case.finished and (completion >= case.finished) is crossing
            and completion < case.deadline and accepted and len(case.credited(data)) == 17
            and data["result"] == "PASS" and data["playback_evidence"] == "UNVERIFIED"):
        raise AssertionError("real-time Phase 3 continuity fixture failed")
    return {
        "result": "PASS", "baseline_credited": 17, "baseline_expected": 17,
        "clock": "CLOCK_MONOTONIC", "time_namespace": namespace, "elapsed_monotonic_ns": elapsed,
        "window_start_monotonic_ns": started,
        "window_finish_monotonic_ns": case.finished, "eligible_request_start_monotonic_ns": request,
        "terminal_response_monotonic_ns": completion, "crossed_finish": crossing,
        "terminal_deadline_monotonic_ns": case.deadline,
    }


def main():
    if get_clock_info("monotonic").implementation != "clock_gettime(CLOCK_MONOTONIC)":
        raise RuntimeError("Real-clock Phase 3 fixture requires Linux CLOCK_MONOTONIC")
    namespace = os.readlink("/proc/self/ns/time")
    with ThreadPoolExecutor(max_workers=6) as pool:
        windows = list(pool.map(lambda index: observe(index, namespace), range(6)))
    print(json.dumps({"fixture": "OFFLINE_REAL_MONOTONIC_OBSERVATION_WITH_SIMULATED_TRANSPORT",
                      "windows": windows, "windows_passed": len(windows),
                      "inflight_crossings": sum(row["crossed_finish"] for row in windows)}, sort_keys=True))


if __name__ == "__main__":
    main()
