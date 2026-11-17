"""Measure ingress latency with Edge completions and passive monotonic request membership."""

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from ipaddress import IPv4Address
import json
import re

from docker_logs import _timestamp_ns
from gates import identifier
from player_capture import NATIVE_RUNTIME, PLAYER_LATENCY_CAPTURE, NS_PER_SECOND
from player_observer_monotonic import CAPTURE_LIFETIME_NS, MAX_ROWS
from release_state import Stop, decode, require


METRIC = "EDGE_TOTAL_DURATION_MS"
LIMITS = {
    "heartbeat": ("PLAYER_HEARTBEAT", "HEARTBEAT", 5 * NS_PER_SECOND),
    NATIVE_RUNTIME: ("PLAYER_RUNTIME_CONFIG", "RUNTIME", 8 * NS_PER_SECOND),
    "playback-events": ("PLAYER_PLAYBACK_EVENT", "PLAYBACK", 5 * NS_PER_SECOND),
}
EDGE_CLASSES = {route_class: endpoint for endpoint, (route_class, _, _) in LIMITS.items()}
CLASS_NAMES = {*EDGE_CLASSES, "OTHER"}
NATIVE_FRAMES = {"latency_response", "latency_incomplete"}
INCOMPLETE_LOG_STREAM = b"PLAYER_LATENCY_EDGE_LOG_STREAM_INCOMPLETE"
SENSITIVE_KEYS = {
    "authorization", "cookie", "cookies", "set-cookie", "request", "request_body", "response_body",
    "request_headers", "response_headers", "resp_headers", "headers", "body", "uri", "url", "query", "query_string",
    "player_devices.id", "player_id", "player_device_id", "channel_id", "token", "jwt", "password",
}


def _marker(event):
    return event == PLAYER_LATENCY_CAPTURE and type(event["version"]) is int


def split_capture(frames):
    """Give the continuity core its original stream shape, including the completion record."""
    if len(frames) < 4 or not _marker(decode(frames[2])):
        return list(frames), False
    continuity = list(frames[:2])
    for raw in frames[3:]:
        row = decode(raw)
        if not isinstance(row, dict) or row.get("type") not in NATIVE_FRAMES:
            continuity.append(raw)
    return continuity, True


def _timestamp(value):
    return type(value) is int and 0 < value <= 10**19


def _captured_requests(frames, window):
    require(4 <= len(frames) <= MAX_ROWS + 2, "PLAYER_LATENCY_CAPTURE_INCOMPLETE")
    require(all(isinstance(row, (bytes, str)) and len(row) <= 2048 for row in frames),
            "PLAYER_LATENCY_CAPTURE_INVALID")
    ready, clock, marker = map(decode, frames[:3])
    done = decode(frames[-1])
    require(_marker(marker), "PLAYER_LATENCY_CAPTURE_CAPABILITY_MISSING")
    require(all(type(row) is dict for row in (ready, clock, done)), "PLAYER_LATENCY_CAPTURE_INVALID")
    begin, deadline = clock.get("ready_monotonic_ns"), clock.get("deadline_monotonic_ns")
    require(ready.get("type") == "ready" and _timestamp(ready.get("started_ns"))
            and clock.get("type") == "monotonic_clock" and _timestamp(begin) and _timestamp(deadline)
            and deadline - begin == CAPTURE_LIFETIME_NS and begin <= window.opened_ns
            and window.finish_ns <= deadline and done.get("type") == "done"
            and _timestamp(done.get("ended_monotonic_ns")) and done["ended_monotonic_ns"] >= deadline
            and type(done.get("kernel_dropped")) is int and done["kernel_dropped"] == 0,
            "PLAYER_LATENCY_CAPTURE_INCOMPLETE")
    events = {}
    for raw in frames[3:-1]:
        row = decode(raw)
        require(type(row) is dict, "PLAYER_LATENCY_CAPTURE_INVALID")
        endpoint, kind, trace = row.get("endpoint"), row.get("type"), row.get("trace")
        valid_kinds = NATIVE_FRAMES if endpoint == NATIVE_RUNTIME else {"response", "incomplete"}
        require(isinstance(endpoint, str) and endpoint in LIMITS and isinstance(kind, str)
                and kind in valid_kinds and identifier(trace) and trace not in events,
                "PLAYER_LATENCY_CAPTURE_INVALID")
        started = row.get("request_start_monotonic_ns")
        require(_timestamp(started) and begin <= started < deadline and _timestamp(row.get("started_ns")),
                "PLAYER_LATENCY_CAPTURE_INVALID")
        expected = {"type", "trace", "endpoint", "started_ns", "request_start_monotonic_ns"}
        if kind in ("response", "latency_response"):
            complete, status = row.get("response_complete_monotonic_ns"), row.get("status")
            require(_timestamp(complete) and started <= complete < deadline
                    and _timestamp(row.get("response_complete_ns")) and type(status) is int and 200 <= status <= 599,
                    "PLAYER_LATENCY_CAPTURE_INVALID")
            expected |= {"status", "response_complete_monotonic_ns", "response_complete_ns"}
            if endpoint == "heartbeat" and status == 200:
                expected |= {"channel_id", "received_at"}
        require(set(row) == expected, "PLAYER_LATENCY_CAPTURE_INVALID")
        events[trace] = row
    return events


def _edge_json(raw):
    require(raw != INCOMPLETE_LOG_STREAM, "PLAYER_LATENCY_EDGE_LOG_STREAM_INCOMPLETE")

    def unique(pairs):
        row = dict(pairs)
        require(len(row) == len(pairs), "PLAYER_LATENCY_EDGE_LOG_INVALID")
        return row

    def reject_constant(_):
        raise Stop("PLAYER_LATENCY_EDGE_LOG_INVALID")

    try:
        row = json.loads(raw, object_pairs_hook=unique, parse_float=Decimal, parse_constant=reject_constant)
    except (ValueError, UnicodeError, ArithmeticError, RecursionError) as error:
        raise Stop("PLAYER_LATENCY_EDGE_LOG_INVALID") from error
    require(isinstance(row, dict), "PLAYER_LATENCY_EDGE_LOG_INVALID")
    return row


def _access_rows(lines, events, context, window, faults):
    all_ids, unmatched = defaultdict(list), []
    generation = str(context["generation"])
    for index, raw in enumerate(lines):
        if index >= MAX_ROWS or len(raw) > 65536:
            faults[None].add("PLAYER_LATENCY_EDGE_LOG_BOUNDS_EXCEEDED")
            break
        try:
            row = _edge_json(raw)
        except Stop as error:
            faults[None].add(str(error))
            continue
        if not str(row.get("logger", "")).startswith("http.log.access"):
            continue
        trace, route_class = row.get("request_id"), row.get("route_class")
        known_trace = identifier(trace)
        if known_trace:
            all_ids[trace].append(row)
            if trace in events and not window.admits(events[trace]["request_start_monotonic_ns"]):
                continue
        if row.get("ingress") in ("preview-blue", "preview-green"):
            continue
        raw_generation = row.get("route_generation")
        legacy_endpoint = row.get("endpoint")
        endpoint = EDGE_CLASSES.get(route_class) if isinstance(route_class, str) else None
        if endpoint is None and isinstance(legacy_endpoint, str) and legacy_endpoint in LIMITS:
            endpoint = legacy_endpoint
        if endpoint is not None:
            if (isinstance(raw_generation, str) and re.fullmatch(r"0|[1-9][0-9]*", raw_generation)
                    and (len(raw_generation), raw_generation) < (len(generation), generation)):
                continue
            if not known_trace:
                faults[endpoint].add("PLAYER_LATENCY_EDGE_REQUEST_ID_MISSING")
            elif trace not in events:
                unmatched.append((trace, endpoint))
        elif (raw_generation == generation and row.get("ingress") == "public"
              and row.get("upstream") == context["upstream"]
              and (not isinstance(route_class, str) or route_class not in CLASS_NAMES)):
            faults[None].add("PLAYER_LATENCY_EDGE_CLASS_MISSING")
    for trace, endpoint in unmatched:
        code = "PLAYER_LATENCY_DUPLICATE_EDGE_REQUEST_ID" if len(all_ids[trace]) > 1 else "PLAYER_LATENCY_EDGE_WITHOUT_WIRE"
        faults[endpoint].add(code)
    return all_ids


def _duration_ns(value):
    require(type(value) in (int, Decimal) and 0 <= value <= 86400, "PLAYER_LATENCY_EDGE_DURATION_INVALID")
    if type(value) is Decimal:
        require(-30 <= value.as_tuple().exponent <= 5, "PLAYER_LATENCY_EDGE_DURATION_INVALID")
    # Go encodes time.Duration.Seconds() as a binary64 number. Recover the
    # original integer at most 0.02 ns away, wider than the encoder's maximum
    # rounding error in the accepted range without admitting sub-ns timings.
    numerator, denominator = value.as_integer_ratio()
    ns, remainder = divmod(numerator * NS_PER_SECOND, denominator)
    require(min(remainder, denominator - remainder) * 50 <= denominator,
            "PLAYER_LATENCY_EDGE_DURATION_INVALID")
    return ns + (2 * remainder > denominator)


def _upstream_ms(value):
    if type(value) is str and re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,24})?(?:[eE][+-]?[0-9]{1,2})?", value):
        value = Decimal(value)
    require(type(value) in (int, Decimal) and 0 <= value <= 86_400_000, "PLAYER_LATENCY_UPSTREAM_TIMING_INVALID")
    if type(value) is Decimal:
        require(-30 <= value.as_tuple().exponent <= 8, "PLAYER_LATENCY_UPSTREAM_TIMING_INVALID")
    return value


def _safe_access_record(raw):
    if len(raw) > 65536:
        return {"kind": "OVERSIZED_REDACTED"}
    try:
        row = _edge_json(raw)
    except Stop:
        return {"kind": "MALFORMED_REDACTED"}
    logger = row.get("logger")
    if type(logger) is not str or not logger.startswith("http.log.access"):
        return {"kind": "NON_ACCESS_REDACTED"}
    evidence = {"kind": "ACCESS"}
    enumerations = {
        "route_class": CLASS_NAMES,
        "slot": ("BLUE", "GREEN"),
        "ingress": ("public", "preview-blue", "preview-green"),
        "endpoint": (*LIMITS, "channels", "admin-runtime"),
    }
    for field, options in enumerations.items():
        value = row.get(field)
        if type(value) is str and value in options:
            evidence[field] = value
    trace, generation = row.get("request_id"), row.get("route_generation")
    if identifier(trace):
        evidence["request_id"] = trace
    if type(generation) is str and re.fullmatch(r"0|[1-9][0-9]{0,63}|preview", generation):
        evidence["route_generation"] = generation
    received = row.get("received_at_ms")
    if type(received) is str and re.fullmatch(r"[0-9]{13}", received):
        evidence["received_at_ms"] = received
    upstream = row.get("upstream")
    if type(upstream) is str and re.fullmatch(r"backend-(blue|green):8000|frontend-(blue|green):80", upstream):
        evidence["upstream"] = upstream
    elif type(upstream) is str and re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}:8000", upstream):
        try:
            evidence["upstream"] = str(IPv4Address(upstream.rsplit(":", 1)[0])) + ":8000"
        except ValueError:
            pass
    for field in ("status", "upstream_status"):
        value = row.get(field)
        if type(value) is int and 0 <= value <= 599:
            evidence[field] = value
    timings = ("upstream_duration_ms", "upstream_headers_ms", "proxy_ms")
    for field in timings:
        try:
            evidence[field] = str(_upstream_ms(row.get(field)))
        except Stop:
            pass
    try:
        evidence["duration_ns"] = _duration_ns(row.get("duration"))
    except Stop:
        pass
    if {key.lower() for key in row} & SENSITIVE_KEYS:
        evidence["sensitive_fields_redacted"] = True
    return evidence


def _stream_integrity(raw):
    missing_timestamp = incomplete = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            incomplete += 1
        prefix, separator, _ = line.partition(b" ")
        try:
            if not separator:
                raise ValueError
            _timestamp_ns(prefix.decode("ascii"))
        except (UnicodeError, ValueError):
            missing_timestamp += 1
    return {"untimestamped_or_malformed_prefix": missing_timestamp, "unterminated_lines": incomplete}


def sanitized_log_evidence(capture):
    """Keep technical per-stream chronology even when the Docker read failed."""
    streams = {name: {"captured_bytes": len(raw), "records": [], **_stream_integrity(raw)}
               for name, raw in (("stdout", capture.stdout), ("stderr", capture.stderr))}
    merged = []
    # Permit inspection of partial output for the archive only. The original
    # return code still governs the collector and is recorded below.
    diagnostic_view = replace(capture, returncode=0)
    for index, row in enumerate(diagnostic_view.timeline(capture.container_id)):
        if index >= MAX_ROWS:
            break
        line = {"timestamp": row.timestamp, **_safe_access_record(row.payload)}
        records = streams[row.stream]["records"]
        merged.append({"stream": row.stream, "record": len(records)})
        records.append(line)
    intact = not any(s["untimestamped_or_malformed_prefix"] or s["unterminated_lines"] for s in streams.values())
    return {
        "container_id": capture.container_id,
        "time_window": {"since_inclusive": capture.since, "until_exclusive": capture.until},
        "collected_at_utc": capture.collected_at,
        "command_sha256": capture.command_sha256,
        "returncode": capture.returncode,
        "stream_integrity": "PASS" if intact else "FAIL_EVIDENCE",
        "streams": streams,
        "merged_timeline": merged,
    }


def _milliseconds(ns):
    whole, fraction = divmod(ns, 1_000_000)
    return f"{whole}.{fraction:06d}"


def _measurement(edge, event, context):
    endpoint = event["endpoint"]
    require(edge.get("route_class") == LIMITS[endpoint][0] and edge.get("ingress") == "public"
            and edge.get("route_generation") == str(context["generation"]) and edge.get("slot") == context["slot"]
            and edge.get("upstream") == context["upstream"], "PLAYER_LATENCY_EDGE_ROUTE_MISMATCH")
    require(not {key.lower() for key in edge} & SENSITIVE_KEYS, "PLAYER_LATENCY_EDGE_LOG_NOT_REDACTED")
    status = event["status"]
    require(type(edge.get("status")) is int and type(edge.get("upstream_status")) is int
            and edge["status"] == edge["upstream_status"] == status, "PLAYER_LATENCY_HTTP_STATUS_MISMATCH")
    total = _duration_ns(edge.get("duration"))
    upstream = _upstream_ms(edge.get("upstream_duration_ms"))
    headers = _upstream_ms(edge.get("upstream_headers_ms"))
    require(total > 0 and headers <= upstream
            and upstream <= Decimal(total) / 1_000_000 + Decimal("0.00000002"),
            "PLAYER_LATENCY_UPSTREAM_TIMING_INVALID")
    return {
        "REQUEST_ID": event["trace"], "REQUEST_START_MONOTONIC_NS": event["request_start_monotonic_ns"],
        "RESPONSE_COMPLETE_MONOTONIC_NS": event["response_complete_monotonic_ns"],
        "ROUTE_CLASS": LIMITS[endpoint][0], "ROUTE_GENERATION": context["generation"], "SLOT": context["slot"],
        "UPSTREAM": context["upstream"], "HTTP_STATUS": status, "EDGE_TOTAL_DURATION_NS": total,
        "EDGE_TOTAL_DURATION_MS": _milliseconds(total), "UPSTREAM_DURATION_MS": str(upstream),
        "UPSTREAM_HEADER_LATENCY_MS": str(headers),
    }


def evaluate_player_latency(frames, logs, context, window):
    """Only monotonic wire starts confer membership, and only a unique Edge row confers latency."""
    faults = defaultdict(set)
    measured = defaultdict(list)
    requests = []
    try:
        window.validate()
        events = _captured_requests(frames, window)
    except Stop as error:
        faults[None].add(str(error))
        events = {}
    edges = _access_rows(logs, events, context, window, faults)
    for trace, event in events.items():
        if not window.admits(event["request_start_monotonic_ns"]):
            continue
        endpoint = event["endpoint"]
        try:
            require(event["type"] in ("response", "latency_response"), "PLAYER_LATENCY_WIRE_INCOMPLETE")
            require(bool(edges[trace]), "PLAYER_LATENCY_EDGE_LOG_MISSING")
            require(len(edges[trace]) == 1, "PLAYER_LATENCY_DUPLICATE_EDGE_REQUEST_ID")
            record = _measurement(edges[trace][0], event, context)
        except Stop as error:
            faults[endpoint].add(str(error))
        else:
            measured[endpoint].append(record)
            requests.append(record)

    report = {"PLAYER_CONTRACT_LATENCY_METRIC": METRIC, "PLAYER_CONTRACT_LATENCY_UNIT": "ns",
              "requests": requests, "evidence_errors": sorted(set().union(*faults.values()))}
    states = set()
    for endpoint, (_, title, limit) in LIMITS.items():
        rows = measured[endpoint]
        maximum = max((row["EDGE_TOTAL_DURATION_NS"] for row in rows), default=None)
        if faults[None] or faults[endpoint]:
            status = "FAIL_EVIDENCE"
        elif maximum is None:
            status = "UNVERIFIED_NO_TRAFFIC"
        elif maximum >= limit or any(row["HTTP_STATUS"] == 409 or row["HTTP_STATUS"] >= 500 for row in rows):
            status = "FAIL"
        else:
            status = "PASS"
        prefix = f"REAL_{title}_LATENCY"
        report.update({f"{prefix}_COUNT": len(rows), f"{prefix}_MAX": maximum, f"{prefix}_STATUS": status})
        states.add(status)
    precedence = ("FAIL_EVIDENCE", "FAIL", "UNVERIFIED_NO_TRAFFIC", "PASS")
    report["PLAYER_TIMEOUT_CONTRACTS_REAL"] = next(status for status in precedence if status in states)
    return report
