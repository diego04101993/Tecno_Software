"""Observe an API-only promotion using the shared monotonic Player core."""

import os
from pathlib import Path
import sys
from time import monotonic_ns
from urllib.parse import unquote, urlsplit

from gates import DockerProbe, PlayerGate, sign_receipt, utc, verified_payload
from player_latency import evaluate_player_latency, split_capture
from player_observer import (
    capture_baseline, capture_input, edge_logs, from_ns, local_docker, phase3_ready_line,
    postgres_rows, read_only_query, save_phase3_request_evidence, save_receipt,
)
from player_observer_launcher import EPOCH_FIELDS, _inspect_collector, _proc_clock
from player_observer_monotonic import MINIMUM_WINDOW_NS, MonotonicWindow, observe_committed_slot
from promotion_observer_context import FrozenPromotion, file_epoch, require_promotion_telemetry
from release_state import SLOTS, StateStore, decode, digest, read_private, require
from routectl import CaddyEdge, Controller


def _clock_identity(receipt):
    clock = decode(read_private(receipt))
    namespace, offsets = _proc_clock(Path("/proc/self"))
    require(clock["TIME_NAMESPACE_MATCH"] == clock["TIME_NAMESPACE_ONLY"] == "YES"
            and clock["OBSERVER_PID"] == os.getpid()
            and namespace == clock["OBSERVER_TIME_NS"] == clock["COLLECTOR_TIME_NS"]
            and offsets == clock["OBSERVER_TIMENS_OFFSETS"] == clock["COLLECTOR_TIMENS_OFFSETS"],
            "PLAYER_MONOTONIC_CLOCK_INVALID")
    live = _inspect_collector(clock["COLLECTOR_ID"], clock["COLLECTOR_IMAGE"], clock["COLLECTOR_RESTART_COUNT"])
    require(all(clock[name] == live[name] for name in EPOCH_FIELDS if name.startswith("COLLECTOR_")),
            "PLAYER_MONOTONIC_CLOCK_INVALID")
    return {"time_namespace": namespace, "collector_id": clock["COLLECTOR_ID"],
            "collector_pid": clock["COLLECTOR_HOST_PID"], "collector_started_at": clock["COLLECTOR_STARTED_AT"]}


def _epoch(container):
    state = container["State"]
    require(state["Running"] is True and state["Status"] == "running" and state["Pid"] > 0
            and all(state.get(flag) is False for flag in ("OOMKilled", "Paused", "Restarting", "Dead")),
            "OBSERVER_ENDPOINT_IDENTITY_INVALID")
    return container["Id"], container["Image"], container["RestartCount"], state["Pid"], state["StartedAt"]


def _matches_database(api, postgres):
    api_env = dict(row.split("=", 1) for row in api["Config"]["Env"])
    pg_env = dict(row.split("=", 1) for row in postgres["Config"]["Env"])
    url = urlsplit(api_env["DATABASE_URL"])
    require(url.scheme in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2")
            and url.port in (None, 5432) and not url.query and not url.fragment
            and unquote(url.path.removeprefix("/")) == pg_env["POSTGRES_DB"], "OBSERVER_POSTGRES_TARGET_INVALID")
    api_networks = api["NetworkSettings"]["Networks"]
    for name, endpoint in postgres["NetworkSettings"]["Networks"].items():
        peer = api_networks.get(name)
        aliases = {postgres["Name"].removeprefix("/"), endpoint["IPAddress"],
                   *(endpoint.get("Aliases") or []), *(endpoint.get("DNSNames") or [])}
        if peer is not None and peer["NetworkID"] == endpoint["NetworkID"] and url.hostname in aliases:
            return
    require(False, "OBSERVER_POSTGRES_TARGET_INVALID")


def _endpoints(args, backend_id, clock):
    expected = (args.edge_name, backend_id, args.postgres_id, clock["collector_id"])
    edge, api, postgres, collector = decode(local_docker("container", "inspect", *expected).stdout)
    require(edge["Name"] == "/" + args.edge_name and api["Id"] == backend_id
            and postgres["Id"] == args.postgres_id and collector["Id"] == clock["collector_id"]
            and collector["HostConfig"]["NetworkMode"] == "container:" + edge["Id"],
            "OBSERVER_ENDPOINT_IDENTITY_INVALID")
    if args.action == "promotion-observe":
        require(edge["Id"] == args.edge_id, "OBSERVER_ENDPOINT_IDENTITY_INVALID")
    require(all(item["State"]["Health"]["Status"] == "healthy" for item in (api, postgres)),
            "OBSERVER_ENDPOINT_UNHEALTHY")
    _matches_database(api, postgres)
    return tuple(_epoch(item) for item in (edge, api, postgres, collector))


def _controller(args):
    def manifest(role):
        file = getattr(args, f"legacy_{role}_manifest")
        pin = getattr(args, f"legacy_{role}_sha256")
        require(bool(file) == bool(pin), "OBSERVER_LEGACY_MANIFEST_PIN_REQUIRED")
        return (decode(read_private(file)) if file else None), pin

    blue, blue_pin = manifest("blue")
    frontend, frontend_pin = manifest("frontend")
    probe = DockerProbe(decode(read_private(args.bindings)), edge_name=args.edge_name,
                        legacy_blue=blue, legacy_blue_sha256=blue_pin,
                        legacy_frontend_blue=frontend, legacy_frontend_blue_sha256=frontend_pin)
    if blue is not None:
        require(probe.legacy_postgres_id() == args.postgres_id, "OBSERVER_POSTGRES_TARGET_INVALID")
    return Controller(StateStore(args.state_dir), CaddyEdge(args.socket, args.autosave), probe, None, None)


def _baseline(args, diagnostic, controller, clock, read, key):
    epoch = file_epoch(controller.store.path)
    bindings = read_private(args.bindings)
    report = controller.status_report()
    require(report["STATUS"] == "PASS", "OBSERVER_ROUTING_STATUS_NOT_PASS")
    state = report["state"]
    effective, etag = controller.edge.effective()
    revision = require_promotion_telemetry(state, effective, args.expected_edge_config_revision)
    active = state["routing"]["active"]
    require(state["pending"] is None and state["rollback"] is None and active["api"] == active["frontend"],
            "OBSERVER_REQUIRES_STEADY_PREDECESSOR")
    target, = set(SLOTS) - {active["api"]}
    ready = state["slots"][target]
    require(ready is not None and ready["phase"] == "READY", "OBSERVER_REQUIRES_READY_TARGET")
    backend = ready["ready"]["api"]["identity"]["container_id"]
    before = _endpoints(args, backend, clock)
    diagnostic.advance("capture_baseline")
    sample = monotonic_ns()
    baseline = capture_baseline(read, state, key)
    diagnostic.record_baseline_count(baseline["payload"])
    require(baseline["payload"]["baseline_refs"], "PLAYER_PROMOTION_EMPTY_BASELINE")
    after = controller.status_report()
    after_effective, after_etag = controller.edge.effective()
    require(after["STATUS"] == "PASS" and after["state"] == state
            and after_effective.hash == effective.hash and after_etag == etag
            and file_epoch(controller.store.path) == epoch and read_private(args.bindings) == bindings,
            "OBSERVER_ROUTING_CONTEXT_CHANGED")
    require(_endpoints(args, backend, clock) == before and _clock_identity(args.clock_receipt) == clock,
            "OBSERVER_ENDPOINT_IDENTITY_INVALID")
    proof = sign_receipt("PROMOTION_BASELINE_CLOCK", {**clock, "baseline_sha256": digest(baseline),
                         "baseline_monotonic_ns": sample, "edge_id": before[0][0], "postgres_id": args.postgres_id,
                         "edge_config_revision": revision, "edge_config_hash": effective.hash}, key)
    diagnostic.advance("write_baseline_receipt")
    save_receipt(args.receipt + ".clock.json", proof)
    save_receipt(args.receipt, baseline)
    diagnostic.advance("report_baseline")
    print("PLAYER_BASELINE_RECEIPT_WRITTEN")
    return 0


def _attest_baseline(args, frozen, key, clock, window):
    baseline = decode(read_private(args.baseline))
    sidecar = decode(read_private(args.baseline + ".clock.json"))
    gate = PlayerGate(key, require_monotonic=True)
    context = frozen.context
    transition = frozen.state["routing"]["api_transition"]
    intent = frozen.state["history"][-1]["intent"]
    data = verified_payload("BASELINE", baseline, key,
                            {"slot": context["slot"], "prior_slot": context["prior_slot"],
                             "previous_generation": intent["previous_generation"], "identity_hash": context["identity_hash"],
                             "release": context["release"]}, {"baseline_at", "baseline_refs"})
    proof = gate.baseline_clock(baseline, sidecar, expected_edge_config_revision=context["edge_config_revision"],
                                expected_edge_config_hash=intent["previous_hash"])
    require(gate.baseline_hash(data) == transition["baseline_hash"], "BASELINE_DOES_NOT_MATCH_SWITCH")
    require(all(proof[name] == value for name, value in clock.items())
            and proof["edge_id"] == args.edge_id and proof["postgres_id"] == args.postgres_id
            and window.opened_ns - MINIMUM_WINDOW_NS <= proof["baseline_monotonic_ns"] <= window.opened_ns,
            "PLAYER_MONOTONIC_BASELINE_INVALID")
    return data, transition, gate


def _read_window(args, diagnostic, frozen, window, namespace, read, refs, key, audit, *, latency_audit):
    diagnostic.advance("read_capture")
    frames = capture_input(sys.stdin.buffer, opened_monotonic_ns=window.opened_ns, time_namespace=namespace,
                           ready_line=phase3_ready_line(args.capture_ready_json), checkpoint=frozen.check_state)
    diagnostic.counts["capture_records"] = len(frames)
    require(window.finish_ns <= monotonic_ns(), "OBSERVER_WINDOW_END_IN_FUTURE")
    require(len(frames) >= 3, "CAPTURE_STREAM_INCOMPLETE")
    ready, done = decode(frames[0]), decode(frames[-1])
    require(ready.get("type") == "ready" and done.get("type") == "done", "CAPTURE_STREAM_INCOMPLETE")
    # UTC bounds retrieve Docker log bytes. Only the wire's monotonic start grants eligibility.
    diagnostic.advance("collect_edge_logs")
    since = min(utc(args.opened_at), from_ns(ready["started_ns"])).isoformat()
    logs = tuple(edge_logs(args.edge_id, since, args.receipt, diagnostics=diagnostic, promotion_latency=True))
    diagnostic.advance("adjudicate")
    latency_audit.update(evaluate_player_latency(frames, logs, frozen.context, window))
    continuity_frames, _ = split_capture(frames)
    return observe_committed_slot(continuity_frames, logs, lambda pairs: read_only_query(read, pairs), refs, frozen.context,
                                  key, window, time_namespace=namespace, audit=audit)


def _observe(args, diagnostic, controller, clock, read, key):
    diagnostic.advance("verify_committed_routing")
    frozen = FrozenPromotion.capture(controller, args.bindings, args.committed_state_sha256,
                                     expected_edge_config_revision=args.expected_edge_config_revision)
    window = MonotonicWindow(args.opened_monotonic_ns, args.started_monotonic_ns, args.finished_monotonic_ns)
    window.validate()
    require(window.start_ns <= monotonic_ns(), "OBSERVER_WINDOW_START_IN_FUTURE")
    baseline, transition, gate = _attest_baseline(args, frozen, key, clock, window)
    require(utc(args.opened_at) == utc(transition["switched_at"]), "OBSERVER_ROUTING_CONTEXT_CHANGED")
    audit_times = {name: utc(getattr(args, name)).isoformat() for name in ("started_at", "finished_at")}
    diagnostic.set_window(opened_at=utc(args.opened_at), **{name: utc(value) for name, value in audit_times.items()})
    diagnostic.record_baseline_count(baseline)
    backend = frozen.context["backend_container"]
    endpoints = _endpoints(args, backend, clock)
    refs = set(baseline["baseline_refs"])
    proof = {"OBSERVER_MODE": "PROMOTION_MONOTONIC", "EXPECTED_GENERATION": frozen.context["generation"],
             "EXPECTED_EDGE_CONFIG_REVISION": frozen.context["edge_config_revision"],
             "EXPECTED_SLOT": frozen.context["slot"], "EXPECTED_UPSTREAM": frozen.context["upstream"],
             "EXPECTED_BACKEND_CONTAINER": backend, "ROUTING_CONTEXT_SHA256": digest(frozen.context),
             "WINDOW_OPENED_MONOTONIC": window.opened_ns, "WINDOW_START_MONOTONIC": window.start_ns,
             "WINDOW_FINISH_MONOTONIC": window.finish_ns, "TIME_NAMESPACE_MATCH": "YES",
             "TIME_NAMESPACE": clock["time_namespace"]}
    audit = {"version": 1, **proof, "requests": [], "player_latency": {}}
    try:
        evidence, accepted, deadline = _read_window(args, diagnostic, frozen, window, clock["time_namespace"],
                                                     read, refs, key, audit["requests"],
                                                     latency_audit=audit["player_latency"])
        diagnostic.advance("recheck_routing_and_endpoints")
        frozen.recheck()
        require(_endpoints(args, backend, clock) == endpoints and _clock_identity(args.clock_receipt) == clock,
                "OBSERVER_ENDPOINT_IDENTITY_INVALID")
    except (Exception, KeyboardInterrupt):
        try:
            save_phase3_request_evidence(args.receipt, audit, diagnostic)
        except Exception:
            # Preserve the original failure; ObserverDiagnostics records evidence write errors.
            pass
        raise
    observed = {item["device_ref"] for item in evidence["heartbeats"]}
    proof.update(TERMINAL_DEADLINE_MONOTONIC=deadline, PLAYER_OBSERVED_COUNT=len(refs & observed),
                 PLAYER_MISSING_COUNT=len(refs - observed))
    audit.update(proof)
    proof["REQUEST_EVIDENCE_SHA256"] = digest(audit)
    baseline_fields = {name: baseline[name] for name in ("baseline_at", "baseline_refs")}
    receipt = sign_receipt("PLAYER", {**transition, **audit_times, **baseline_fields, **evidence, "observation": proof}, key)
    diagnostic.record_adjudication(receipt)
    diagnostic.advance("write_request_evidence")
    save_phase3_request_evidence(args.receipt, audit, diagnostic)
    frozen.check_state()
    latency = audit["player_latency"]
    latency_accepted = latency["PLAYER_TIMEOUT_CONTRACTS_REAL"] in ("PASS", "UNVERIFIED_NO_TRAFFIC")
    if latency_accepted:
        diagnostic.advance("write_player_receipt")
        save_receipt(args.receipt, receipt)
        diagnostic.advance("report_player_result")
    continuity_accepted = accepted and gate(receipt, transition, routing_context=frozen.context)
    print("PLAYER_CONTINUITY_GATE=" + ("PASS_ALL_BASELINE" if continuity_accepted else "FAIL"))
    for name, value in latency.items():
        if name.isupper():
            print(f"{name}={value if value is not None else 'UNAVAILABLE'}")
    accepted = continuity_accepted and latency_accepted
    print("PLAYER_OBSERVATION=PASS" if accepted else "PLAYER_OBSERVATION=FAIL")
    return 0 if accepted else diagnostic.fail(2)


def run_promotion(args, diagnostic):
    diagnostic.advance("verify_collector_time_namespace")
    clock = _clock_identity(args.clock_receipt)
    diagnostic.advance("load_state_and_key")
    controller = _controller(args)
    key = read_private(args.key)

    def read(sql):
        return postgres_rows(args.postgres_id, sql)

    if args.action == "promotion-baseline":
        return _baseline(args, diagnostic, controller, clock, read, key)
    return _observe(args, diagnostic, controller, clock, read, key)
