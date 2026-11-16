"""Continuity for a verified route using the captured request's monotonic start."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from gates import GateError, PlayerGate, attribute_heartbeats, identifier, legacy_blue_endpoint, sign_receipt, utc, verified_payload
from player_capture import NS_PER_SECOND as SECOND, PHASE3_CAPTURE_SECONDS
from release_state import Stop, decode, require


MINIMUM_WINDOW_NS = 120 * SECOND
CAPTURE_LIFETIME_NS = PHASE3_CAPTURE_SECONDS * SECOND
MAX_ROWS = 100_000
PATHS = {"heartbeat": 200, "playback-events": 201}
TIME_NAMESPACE = re.compile(r"time:\[[1-9][0-9]*\]")
GENERATION = re.compile(r"0|[1-9][0-9]*")
UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp(value):
    return type(value) is int and 0 < value <= 10**19


def _utc_wire(value):
    require(_timestamp(value), "INVALID_CAPTURE_TIMESTAMP")
    seconds, fraction = divmod(value, SECOND)
    stamp = UTC_EPOCH + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


@dataclass(frozen=True)
class MonotonicWindow:
    opened_ns: int
    start_ns: int
    finish_ns: int

    def validate(self):
        require(all(_timestamp(value) for value in (self.opened_ns, self.start_ns, self.finish_ns))
                and self.opened_ns <= self.start_ns
                and self.finish_ns - self.start_ns >= MINIMUM_WINDOW_NS,
                "PLAYER_MONOTONIC_WINDOW_INVALID")

    def admits(self, stamp):
        return self.start_ns <= stamp < self.finish_ns


def _audit_row(trace, window, deadline, event=None):
    began = None if event is None else event["request_start_monotonic_ns"]
    complete = None if event is None else event.get("response_complete_monotonic_ns")
    eligible = None if began is None else window.admits(began)
    return {"REQUEST_ID": trace, "PLAYER_PSEUDONYM": None,
            "REQUEST_START_MONOTONIC_NS": began,
            "REQUEST_START_UTC": None if event is None else _utc_wire(event["started_ns"]),
            "WINDOW_START_MONOTONIC_NS": window.start_ns, "WINDOW_FINISH_MONOTONIC_NS": window.finish_ns,
            "REQUEST_TERMINAL_DEADLINE_MONOTONIC_NS": deadline,
            "REQUEST_ELIGIBLE": eligible, "ROUTE_GENERATION": None, "SLOT": None, "UPSTREAM": None,
            "EDGE_RECEIVED_AT_MS": None, "EDGE_HTTP_STATUS": None,
            "HTTP_STATUS": None if event is None else event.get("status"),
            "RESPONSE_COMPLETE_MONOTONIC_NS": complete,
            "RESPONSE_COMPLETE_UTC": None if complete is None else _utc_wire(event["response_complete_ns"]),
            "ACK_RECEIVED_AT": None, "ACK_JOIN_RESULT": "NOT_ATTEMPTED",
            "FINAL_ADJUDICATION": "NO_CREDIT" if eligible is False else "UNVERIFIED"}


def _read_capture(lines, window, time_namespace, audit, *, phase3=False):
    frames = iter(lines)
    prelude = []
    for _ in range(2):
        raw = next(frames, None)
        require(raw is not None and len(raw) <= 2048, "PLAYER_MONOTONIC_CLOCK_INVALID")
        prelude.append(decode(raw))
    ready, clock = prelude
    require(type(ready) is dict and set(ready) == {"type", "started_ns"} and ready["type"] == "ready"
            and _timestamp(ready["started_ns"]) and type(clock) is dict
            and set(clock) == {"type", "ready_monotonic_ns", "deadline_monotonic_ns", "time_namespace"}
            and clock["type"] == "monotonic_clock" and type(time_namespace) is str
            and TIME_NAMESPACE.fullmatch(time_namespace) and clock["time_namespace"] == time_namespace,
            "PLAYER_MONOTONIC_CLOCK_INVALID")
    start, deadline = clock["ready_monotonic_ns"], clock["deadline_monotonic_ns"]
    require(_timestamp(start) and _timestamp(deadline) and deadline - start == CAPTURE_LIFETIME_NS
            and start <= window.opened_ns and window.finish_ns <= deadline, "PLAYER_MONOTONIC_CAPTURE_SCOPE_INVALID")
    captured, rows = {}, {}
    done = False
    for count, raw in enumerate(frames, 2):
        require(count <= MAX_ROWS and len(raw) <= 2048 and not done, "CAPTURE_STREAM_INVALID")
        event = decode(raw)
        require(type(event) is dict, "CAPTURE_EVENT_INVALID")
        kind = event.get("type")
        if kind == "done":
            require(set(event) == {"type", "ended_ns", "ended_monotonic_ns", "kernel_packets", "kernel_dropped"}
                    and _timestamp(event["ended_ns"]) and _timestamp(event["ended_monotonic_ns"])
                    and event["ended_monotonic_ns"] >= deadline and type(event["kernel_dropped"]) is int
                    and event["kernel_dropped"] == 0 and type(event["kernel_packets"]) is int
                    and event["kernel_packets"] >= 0, "PLAYER_MONOTONIC_CAPTURE_INCOMPLETE")
            done = True
            continue
        trace, endpoint = event.get("trace"), event.get("endpoint")
        require(kind in ("response", "incomplete") and identifier(trace) and endpoint in PATHS
                and trace not in captured, "CAPTURE_TRACE_AMBIGUOUS")
        expected = {"type", "trace", "endpoint", "started_ns", "request_start_monotonic_ns"}
        began = event.get("request_start_monotonic_ns")
        require(_timestamp(began) and _timestamp(event.get("started_ns")),
                "PLAYER_MONOTONIC_REQUEST_START_INVALID")
        if kind == "response":
            status = event.get("status")
            complete = event.get("response_complete_monotonic_ns")
            expected |= {"status", "response_complete_monotonic_ns", "response_complete_ns"}
            require(type(status) is int and 200 <= status <= 599 and _timestamp(complete)
                    and _timestamp(event.get("response_complete_ns")),
                    "PLAYER_MONOTONIC_TERMINAL_INVALID")
            if endpoint == "heartbeat" and status == 200:
                expected |= {"channel_id", "received_at"}
        require(set(event) == expected, "CAPTURE_EVENT_FIELDS_INVALID")
        captured[trace] = event
        if not phase3 or endpoint == "heartbeat":
            rows[trace] = _audit_row(trace, window, deadline, event)
            if not phase3:
                rows[trace]["ENDPOINT"] = endpoint
            audit.append(rows[trace])
            if endpoint == "heartbeat" and event.get("status") == 200:
                try:
                    utc(event["received_at"])
                except GateError:
                    if rows[trace]["REQUEST_ELIGIBLE"]:
                        rows[trace]["FINAL_ADJUDICATION"] = "FAIL_INVALID_ACK_TIMESTAMP"
                else:
                    rows[trace]["ACK_RECEIVED_AT"] = event["received_at"]
        if not start <= began < deadline or kind == "response" and not began <= complete < deadline:
            if trace in rows:
                rows[trace]["FINAL_ADJUDICATION"] = "FAIL_INVALID_CAPTURE_TIMELINE"
            require(start <= began < deadline, "PLAYER_MONOTONIC_REQUEST_START_INVALID")
            require(False, "PLAYER_MONOTONIC_TERMINAL_INVALID")
    require(done, "CAPTURE_STREAM_INCOMPLETE")
    return captured, rows, deadline


def _edge_rows(lines, *, phase3):
    rows = {}
    for index, line in enumerate(lines):
        require(index <= MAX_ROWS and len(line) <= 65536, "EDGE_LOG_BOUNDS_EXCEEDED")
        row = decode(line)
        require(type(row) is dict, "EDGE_LOG_INVALID")
        if (not str(row.get("logger", "")).startswith("http.log.access")
                or row.get("ingress") != "public" or row.get("endpoint") not in PATHS):
            continue
        require(not ({"request", "resp_headers", "headers", "body"} & set(row)), "EDGE_ACCESS_LOG_NOT_REDACTED")
        trace, status = row.get("request_id"), row.get("status")
        require(identifier(trace) and trace not in rows and type(status) is int
                and (200 <= status <= 599 or not phase3 and status == 0),
                "EDGE_LOG_TRACE_OR_STATUS_INVALID")
        require(type(row.get("received_at_ms")) is str and re.fullmatch(r"[0-9]{13}", row["received_at_ms"]),
                "EDGE_LOG_TIMESTAMP_INVALID")
        rows[trace] = row
    return rows


def _route_audit(row, edge, context, *, phase3):
    if row is None or edge is None:
        return
    generation = edge.get("route_generation")
    row["EDGE_RECEIVED_AT_MS"] = int(edge["received_at_ms"])
    row["EDGE_HTTP_STATUS"] = edge["status"]
    if phase3:
        valid_generation = type(generation) is str and re.fullmatch(r"[0-9]{1,12}", generation)
    else:
        valid_generation = type(generation) is str and GENERATION.fullmatch(generation) and len(generation) <= 64
    row["ROUTE_GENERATION"] = int(generation) if valid_generation else None
    if edge.get("upstream") == context["upstream"]:
        row["UPSTREAM"] = context["upstream"]
        if not phase3 or row["ROUTE_GENERATION"] == context["generation"]:
            row["SLOT"] = context["slot"]
    elif not phase3 and edge.get("upstream") == context["prior_upstream"]:
        row["UPSTREAM"] = context["prior_upstream"]
        row["SLOT"] = context["prior_slot"]
    if row["HTTP_STATUS"] is None:
        row["HTTP_STATUS"] = edge["status"]


def _promotion_route(edge, context):
    """Return the audit disposition and fault before considering any ACK."""
    generation = edge.get("route_generation")
    if type(generation) is not str or not GENERATION.fullmatch(generation):
        return "FAIL_INVALID_ROUTE_GENERATION", "PLAYER_MONOTONIC_ROUTE_GENERATION_INVALID"
    expected = str(context["generation"])
    if (len(generation), generation) > (len(expected), expected):
        return "FAIL_FUTURE_ROUTE_GENERATION", "PLAYER_MONOTONIC_FUTURE_GENERATION"
    upstream = edge.get("upstream")
    if upstream == context["prior_upstream"]:
        fault = "EDGE_WIRE_CORRELATION_INCOMPLETE" if generation == expected else None
        return "WRONG_SLOT_FOR_OBSERVATION", fault
    if generation != expected:
        return "STALE_ROUTE_GENERATION", None
    if upstream != context["upstream"]:
        return "FAIL_WRONG_UPSTREAM", "EDGE_WIRE_CORRELATION_INCOMPLETE"
    return None, None


def _correlate(captured, rows, edges, window, deadline, context, audit, *, phase3):
    acks, faults = [], []
    counters = Counter()
    for trace, event in captured.items():
        edge = edges.pop(trace, None)
        row = rows.get(trace)
        _route_audit(row, edge, context, phase3=phase3)
        eligible = window.admits(event["request_start_monotonic_ns"])
        if not phase3 and edge is not None:
            disposition, fault = _promotion_route(edge, context)
            if disposition is not None and (eligible or disposition == "FAIL_FUTURE_ROUTE_GENERATION"):
                row["FINAL_ADJUDICATION"] = disposition
                if fault is not None:
                    faults.append(fault)
                continue
        if not eligible:
            continue
        if event["type"] == "incomplete":
            if row is not None:
                row["FINAL_ADJUDICATION"] = "FAIL_INCOMPLETE"
            faults.append("PLAYER_MONOTONIC_TERMINAL_INCOMPLETE")
            continue
        endpoint, status = event["endpoint"], event["status"]
        matching_route = (edge is not None and edge.get("route_generation") == str(context["generation"])
                          and edge.get("upstream") == context["upstream"])
        if not (matching_route and edge["endpoint"] == endpoint and edge["status"] == edge.get("upstream_status") == status):
            if row is not None:
                row["FINAL_ADJUDICATION"] = "FAIL_CORRELATION"
            faults.append("EDGE_WIRE_CORRELATION_INCOMPLETE")
            continue
        if status == 409:
            counters["http_409"] += 1
        elif status >= 500:
            counters["http_5xx"] += 1
        elif status not in (401, PATHS[endpoint]):
            counters["incompatible"] += 1
        if status != PATHS[endpoint]:
            if row is not None:
                row["FINAL_ADJUDICATION"] = "NO_CREDIT" if status == 401 else "FAIL_HTTP_STATUS"
            continue
        if endpoint == "playback-events":
            counters["playback_201"] += 1
            if row is not None:
                row["FINAL_ADJUDICATION"] = "VERIFIED_PLAYBACK"
            continue
        ack = row["ACK_RECEIVED_AT"]
        if ack is None:
            faults.append("INVALID_TIMESTAMP")
            continue
        if not identifier(event["channel_id"]):
            row["FINAL_ADJUDICATION"] = "FAIL_CORRELATION"
            faults.append("WRONG_HEARTBEAT_ORIGIN")
            continue
        acks.append({"trace": trace, "channel_id": event["channel_id"], "received_at": ack,
                    "generation": context["generation"], "slot": context["slot"], "upstream": context["upstream"],
                    "status": 200, "public": True, "path_class": "heartbeat"})
    for trace, edge in edges.items():
        if phase3:
            if edge.get("route_generation") != str(context["generation"]):
                continue
            disposition, fault = "FAIL_MISSING_MONOTONIC_EVIDENCE", "PLAYER_MONOTONIC_EDGE_WITHOUT_WIRE"
        else:
            disposition, fault = _promotion_route(edge, context)
            if disposition is None:
                disposition, fault = "FAIL_MISSING_MONOTONIC_EVIDENCE", "PLAYER_MONOTONIC_EDGE_WITHOUT_WIRE"
        if not phase3 or edge["endpoint"] == "heartbeat":
            row = _audit_row(trace, window, deadline)
            _route_audit(row, edge, context, phase3=phase3)
            if not phase3:
                row["ENDPOINT"] = edge["endpoint"]
            row["FINAL_ADJUDICATION"] = disposition
            audit.append(row)
        if fault is not None:
            faults.append(fault)
    return acks, counters, faults


def _join(acks, query, key, context, rows, baseline_refs):
    """Keep independently verified rows even if another request fails."""
    pairs = [(ack["channel_id"], utc(ack["received_at"]).isoformat()) for ack in acks]
    counts = Counter(pairs)
    faults = []
    unique_pairs = [pair for pair in pairs if counts[pair] == 1]
    matches = {}
    try:
        for record in (query(unique_pairs) if unique_pairs else []):
            require(identifier(record["channel_id"]), "INVALID_HEARTBEAT_CHANNEL")
            pair = record["channel_id"], utc(record["received_at"]).isoformat()
            require(counts[pair] == 1, "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")
            matches.setdefault(pair, []).append(record)
    except Exception as error:
        for ack in acks:
            rows[ack["trace"]].update(ACK_JOIN_RESULT="UNVERIFIED", FINAL_ADJUDICATION="FAIL_ACK_JOIN")
        return [], [error]
    proof = []
    for ack, pair in zip(acks, pairs):
        row = rows[ack["trace"]]
        try:
            require(counts[pair] == 1, "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")
            match, = attribute_heartbeats([ack], matches.get(pair, []), key, context["generation"],
                                          context["slot"], context["upstream"])
        except Stop as error:
            row.update(ACK_JOIN_RESULT="NOT_UNIQUE_OR_INVALID", FINAL_ADJUDICATION="FAIL_ACK_JOIN")
            faults.append(error)
            continue
        proof.append(match)
        reference = match["device_ref"]
        row.update(PLAYER_PSEUDONYM=reference, ACK_JOIN_RESULT="UNIQUE",
                   FINAL_ADJUDICATION="CREDITED" if reference in baseline_refs else "NEW_DEVICE_NO_BASELINE_CREDIT")
    return proof, faults


def observe_committed_slot(capture, logs, query, baseline_refs, context, key, window, *,
                           time_namespace, audit=None, phase3=False):
    """Adjudicate an already verified route without using UTC for request eligibility."""
    window.validate()
    if not phase3:
        require(type(context["generation"]) is int and context["generation"] > 0
                and {context["slot"], context["prior_slot"]} == {"BLUE", "GREEN"}
                and type(context["upstream"]) is str and context["upstream"]
                and type(context["prior_upstream"]) is str and context["prior_upstream"]
                and context["prior_upstream"] != context["upstream"], "INVALID_OBSERVATION_TARGET")
    if audit is None:
        audit = []
    captured, rows, deadline = _read_capture(capture, window, time_namespace, audit, phase3=phase3)
    edges = _edge_rows(logs, phase3=phase3)
    acks, counters, faults = _correlate(captured, rows, edges, window, deadline, context, audit, phase3=phase3)
    baseline_refs = set(baseline_refs)
    proof, join_faults = _join(acks, query, key, context, rows, baseline_refs)
    if faults or join_faults:
        for row in rows.values():
            if row["FINAL_ADJUDICATION"] == "CREDITED":
                row["FINAL_ADJUDICATION"] = "VERIFIED_NO_WINDOW_RECEIPT"
        if faults:
            require(False, faults[0])
        raise join_faults[0]
    observed_refs = {item["device_ref"] for item in proof}
    accepted = baseline_refs <= observed_refs and not any(counters[k] for k in ("http_409", "http_5xx", "incompatible"))
    evidence = {"source": "EDGE_ACK_EXACT_DB_JOIN", "access_complete": True, "heartbeats": proof,
                "http_409": counters["http_409"], "http_5xx": counters["http_5xx"],
                "incompatible": counters["incompatible"], "playback_201": counters["playback_201"],
                "playback_evidence": "PASS" if counters["playback_201"] else "UNVERIFIED"}
    return evidence, accepted, deadline


def adjudicate_monotonic(capture, logs, query, baseline, context, key, window, *,
                         time_namespace, diagnostics=None, audit=None, audit_times=None):
    """Verify the historical Phase 3 baseline and retain its sealed receipt format."""
    window.validate()
    require(type(context["generation"]) is int and context["generation"] == 0 and context["slot"] == "BLUE"
            and context["upstream"] == legacy_blue_endpoint(context["upstream"]), "INVALID_OBSERVATION_TARGET")
    data = verified_payload("PHASE3_BASELINE", baseline, key, context,
                            {"baseline_at", "baseline_refs", "baseline_monotonic_ns", "time_namespace"})
    baseline_hash = PlayerGate.baseline_hash(data)
    utc(data["baseline_at"])
    require(type(data["baseline_monotonic_ns"]) is int and data["time_namespace"] == time_namespace
            and window.opened_ns - MINIMUM_WINDOW_NS <= data["baseline_monotonic_ns"] <= window.opened_ns,
            "PLAYER_MONOTONIC_BASELINE_INVALID")
    times = {} if audit_times is None else {name: utc(audit_times[name]).isoformat()
                                           for name in ("opened_at", "started_at", "finished_at")}
    if diagnostics is not None:
        diagnostics.record_baseline_count(data)
        diagnostics.set_window(baseline_at=utc(data["baseline_at"]), **{k: utc(v) for k, v in times.items()})
    evidence, accepted, deadline = observe_committed_slot(capture, logs, query, data["baseline_refs"], context, key,
                                                          window, time_namespace=time_namespace, audit=audit,
                                                          phase3=True)
    receipt = sign_receipt("PHASE3_PLAYER", {**context, **times,
        "baseline_hash": baseline_hash, "baseline_at": data["baseline_at"], "baseline_refs": data["baseline_refs"],
        **evidence,
        "window_monotonic": {"opened_ns": window.opened_ns, "started_ns": window.start_ns,
                             "finished_ns": window.finish_ns, "terminal_deadline_ns": deadline,
                             "baseline_sample_ns": data["baseline_monotonic_ns"], "time_namespace": time_namespace},
        "result": "FAIL" if not accepted else "PASS" if data["baseline_refs"] else "PASS_NO_ACTIVE_BASELINE",
    }, key)
    return receipt, accepted
