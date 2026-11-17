"""Produce a PlayerGate receipt from passive Edge evidence and a read-only DB.

Neither this module nor player_capture.py sends a Player request. Wire events
and database rows are volatile inputs. Persist the HMAC receipt and the
already sanitized Docker logs in separate private evidence files.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import chain
import os
from pathlib import Path
import re
from select import select
import subprocess
import sys
from time import monotonic_ns, time_ns

from docker_logs import collect_docker_logs
from gates import PlayerGate, attribute_heartbeats, device_ref, identifier, legacy_blue_endpoint, sign_receipt, utc, verified_payload
from player_observer_diagnostics import InvalidObserverArguments, ObserverArgumentParser, ObserverDiagnostics
from release_state import StateStore, Stop, canonical, decode, read_private, require, require_private, sync_dir
from routectl import BLUE_PAIR, CaddyEdge, Controller


PATHS = {"heartbeat": 200, "playback-events": 201}
NANOSECONDS = 1_000_000_000
MAX_ROWS = 100_000
CAPTURE_PIPE_WATCHDOG_NS = 180 * NANOSECONDS


BASELINE_QUERY = """
WITH clock AS (SELECT statement_timestamp() AS as_of), active AS (
    SELECT d.id AS device
    FROM public.player_devices d
    JOIN public.channels c ON (c.id, c.client_id, c.branch_id) = (d.channel_id, d.client_id, d.branch_id)
    JOIN public.clients t ON t.id = d.client_id AND t.status = 'active'
    JOIN public.branches b ON (b.id, b.client_id) = (d.branch_id, d.client_id) AND b.is_active
    CROSS JOIN clock
    CROSS JOIN LATERAL (
        SELECT h.id FROM public.player_heartbeats h
        WHERE h.channel_id = d.channel_id AND h.player_device_id = d.id
          AND h.client_id = d.client_id AND h.branch_id = d.branch_id
          AND h.received_at BETWEEN clock.as_of - interval '120 seconds' AND clock.as_of
        ORDER BY h.received_at DESC, h.id DESC LIMIT 1
    ) observed
    WHERE d.is_active
)
SELECT json_build_object('as_of', clock.as_of, 'devices',
    COALESCE((SELECT json_agg(active.device ORDER BY active.device) FROM active), '[]'::json)) FROM clock
"""


def baseline_devices(read, key):
    rows = read(BASELINE_QUERY)
    require(len(rows) == 1, "INVALID_PLAYER_BASELINE_QUERY_RESULT")
    snapshot = decode(rows[0])
    require(type(snapshot) is dict and set(snapshot) == {"as_of", "devices"} and type(snapshot["devices"]) is list
            and len(snapshot["devices"]) <= MAX_ROWS and all(identifier(value) for value in snapshot["devices"])
            and len(snapshot["devices"]) == len(set(snapshot["devices"])), "INVALID_PLAYER_BASELINE_QUERY_RESULT")
    return {"baseline_at": utc(snapshot["as_of"]).isoformat(),
            "baseline_refs": sorted(device_ref(value, key) for value in snapshot["devices"])}


def capture_baseline(read, state, key):
    """Sign exactly the old slot's eligible, recently observed device IDs."""
    active = state["routing"]["active"]
    require(state["pending"] is None and state["rollback"] is None and active["api"] == active["frontend"],
            "OBSERVER_REQUIRES_STEADY_PREDECESSOR")
    prior = active["api"]
    require(prior in ("BLUE", "GREEN"), "INVALID_OBSERVATION_TARGET")
    slot = "GREEN" if prior == "BLUE" else "BLUE"
    entry = state["slots"][slot]
    require(entry is not None and entry["phase"] == "READY", "OBSERVER_REQUIRES_READY_TARGET")
    return sign_receipt("BASELINE", {
        "slot": slot, "prior_slot": prior, "previous_generation": state["routing"]["generation"],
        "identity_hash": entry["ready"]["api"]["identity_hash"], "release": entry["release"],
        **baseline_devices(read, key),
    }, key)


def phase3_context(state, postgres_id):
    routing = state["routing"]
    require(state["pending"] is None and state["rollback"] is None and routing["api_transition"] is None
            and routing["generation"] == 0 and routing["active"] == BLUE_PAIR and state["slots"]["GREEN"] is None
            and state["slots"]["BLUE"]["phase"] == "ACTIVE", "OBSERVER_REQUIRES_INITIAL_BLUE_BLUE")
    require(state.get("legacy_blue", {}).get("postgres_id") == postgres_id, "PHASE3_POSTGRES_ID_MISMATCH")
    ready = state["slots"]["BLUE"]["ready"]["api"]
    target = legacy_blue_endpoint(ready["identity"]["upstream"])
    return {"generation": 0, "slot": "BLUE", "upstream": target, "config_hash": routing["config_hash"],
            "identity_hash": ready["identity_hash"], "postgres_id": postgres_id}


def capture_phase3_baseline(read, state, key, postgres_id, *, baseline_monotonic_ns=None, time_namespace=None):
    """Bind a live baseline to the local monotonic clock, preserving archive replay."""
    require((baseline_monotonic_ns is None) == (time_namespace is None), "PHASE3_BASELINE_CLOCK_INVALID")
    clock = {}
    if time_namespace is not None:
        require(type(baseline_monotonic_ns) is int and baseline_monotonic_ns > 0
                and type(time_namespace) is str and re.fullmatch(r"time:\[[1-9][0-9]*\]", time_namespace),
                "PHASE3_BASELINE_CLOCK_INVALID")
        clock = {"baseline_monotonic_ns": baseline_monotonic_ns, "time_namespace": time_namespace}
    return sign_receipt("PHASE3_BASELINE", {**phase3_context(state, postgres_id), **clock,
                                           **baseline_devices(read, key)}, key)


def from_ns(value):
    require(type(value) is int and 0 < value <= 10**19, "INVALID_CAPTURE_TIMESTAMP")
    seconds, remainder = divmod(value, NANOSECONDS)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder // 1000)


def read_capture(lines, started, finished):
    before = None
    done = False
    responses = {}
    incomplete = {}
    for index, line in enumerate(lines):
        require(index <= MAX_ROWS and len(line) <= 2048 and not done, "CAPTURE_STREAM_INVALID")
        event = decode(line)
        require(type(event) is dict and "type" in event, "CAPTURE_EVENT_INVALID")
        kind = event.pop("type")
        if kind == "ready":
            require(index == 0 and set(event) == {"started_ns"}, "CAPTURE_READY_INVALID")
            before = from_ns(event["started_ns"])
        elif kind == "done":
            require(before is not None and set(event) == {"ended_ns", "kernel_packets", "kernel_dropped"}
                    and type(event["kernel_dropped"]) is int and event["kernel_dropped"] == 0
                    and type(event["kernel_packets"]) is int and event["kernel_packets"] >= 0,
                    "CAPTURE_KERNEL_ACCOUNTING_INVALID")
            require(before <= started <= finished <= from_ns(event["ended_ns"]), "CAPTURE_COVERAGE_INCOMPLETE")
            done = True
        else:
            require(before is not None and kind in ("response", "incomplete"), "CAPTURE_FAILED")
            trace = event.get("trace")
            endpoint = event.get("endpoint")
            require(identifier(trace) and endpoint in PATHS and trace not in responses and trace not in incomplete,
                    "CAPTURE_TRACE_AMBIGUOUS")
            status = event.get("status")
            ack = {"received_at", "channel_id"} if endpoint == "heartbeat" and status == 200 else set()
            expected = {"trace", "endpoint", "started_ns"}
            if kind == "response":
                require(type(status) is int and 200 <= status <= 599, "CAPTURE_STATUS_INVALID")
                expected |= {"status"} | ack
            require(set(event) == expected, "CAPTURE_EVENT_FIELDS_INVALID")
            require(before <= from_ns(event["started_ns"]), "CAPTURE_EVENT_PREDATES_CAPTURE")
            (responses if kind == "response" else incomplete)[trace] = event
    require(done, "CAPTURE_STREAM_INCOMPLETE")
    return responses, incomplete


def sanitized_edge_rows(lines, generation, started, finished):
    rows = {}
    for index, line in enumerate(lines):
        require(index <= MAX_ROWS and len(line) <= 65536, "EDGE_LOG_BOUNDS_EXCEEDED")
        record = decode(line)
        require(type(record) is dict, "EDGE_LOG_INVALID")
        if not str(record.get("logger", "")).startswith("http.log.access"):
            continue
        if record.get("ingress") != "public" or record.get("endpoint") not in PATHS:
            continue
        require(not ({"request", "resp_headers", "headers", "body"} & set(record)), "EDGE_ACCESS_LOG_NOT_REDACTED")
        stamp = record.get("received_at_ms")
        require(isinstance(stamp, str) and re.fullmatch(r"[0-9]{13}", stamp), "EDGE_LOG_TIMESTAMP_INVALID")
        received = from_ns(int(stamp) * 1_000_000)
        if not started <= received < finished or record.get("route_generation") != str(generation):
            continue
        trace = record.get("request_id")
        status = record.get("status")
        require(identifier(trace) and trace not in rows and type(status) is int and 200 <= status <= 599,
                "EDGE_LOG_TRACE_OR_STATUS_INVALID")
        rows[trace] = {key: record.get(key) for key in ("endpoint", "status", "upstream_status", "upstream")}
    return rows


def adjudicate(capture, logs, query, baseline, transition, key, started, finished, *, diagnostics=None):
    """Return an exact receipt or fail closed on incomplete attribution.

    `query` accepts a list of sanitized `(channel_id, received_at)` tuples
    and resolves only those rows in a read-only transaction.
    """
    start, end = utc(started), utc(finished)
    require(start >= utc(transition["switched_at"]) and end - start >= timedelta(seconds=120),
            "INVALID_OBSERVATION_WINDOW")
    slot = transition["slot"]
    generation = transition["generation"]
    require(slot in ("BLUE", "GREEN") and type(generation) is int and generation >= 1,
            "INVALID_OBSERVATION_TARGET")
    baseline_context = {
        "slot": slot, "prior_slot": transition["prior_slot"], "identity_hash": transition["identity_hash"],
        "release": transition["release"], "previous_generation": baseline["payload"]["previous_generation"],
    }
    baseline_data = verified_payload("BASELINE", baseline, key, baseline_context, {"baseline_at", "baseline_refs"})
    require(PlayerGate.baseline_hash(baseline_data) == transition["baseline_hash"]
            and baseline_context["previous_generation"] < generation, "BASELINE_DOES_NOT_MATCH_SWITCH")
    if diagnostics is not None:
        diagnostics.record_baseline_count(baseline_data)
        diagnostics.set_window(started_at=start, finished_at=end)
    evidence = observed_evidence(capture, logs, query, key, slot, generation, f"backend-{slot.lower()}:8000", start, end)
    return sign_receipt("PLAYER", {
        **transition, "baseline_at": baseline_data["baseline_at"], "baseline_refs": baseline_data["baseline_refs"],
        "started_at": started, "finished_at": finished, **evidence,
    }, key)


def observed_evidence(capture, logs, query, key, slot, generation, upstream, start, end):
    responses, incomplete = read_capture(capture, start, end)
    access = sanitized_edge_rows(logs, generation, start, end)
    successful = []
    expected_wire = set()
    counters = Counter()
    for trace, row in access.items():
        status, endpoint = row["status"], row["endpoint"]
        if status == 409:
            counters["http_409"] += 1
        if status >= 500:
            counters["http_5xx"] += 1
        if row["upstream"] != upstream:
            # An old-slot response never proves continuity of the promoted
            # slot. A wrong upstream in the current generation is a failure.
            counters["incompatible"] += 1
            continue
        if status >= 500:
            # A failed dial may not have a response from Uvicorn.
            continue
        expected_wire.add(trace)
        wire = responses.get(trace)
        require(wire is not None and trace not in incomplete and wire["endpoint"] == endpoint
                and wire["status"] == row["upstream_status"] == status,
                "EDGE_WIRE_CORRELATION_INCOMPLETE")
        require(start <= from_ns(wire["started_ns"]) < end, "WIRE_REQUEST_OUTSIDE_WINDOW")
        if status != PATHS[endpoint]:
            if status not in (401, 409):
                counters["incompatible"] += 1
            continue
        if endpoint == "playback-events":
            counters["playback_201"] += 1
            continue
        require(identifier(wire["channel_id"]) and start <= utc(wire["received_at"]) < end,
                "HEARTBEAT_ACK_OUTSIDE_WINDOW")
        successful.append({"trace": trace, "channel_id": wire["channel_id"], "received_at": wire["received_at"],
                           "generation": generation, "slot": slot, "upstream": upstream,
                           "status": 200, "public": True, "path_class": "heartbeat"})

    def admitted(events):
        return {trace for trace, event in events.items() if start <= from_ns(event["started_ns"]) < end}

    # Capturing a request for which the sanitized Edge log is absent is
    # evidence loss, including requests still in flight at observation end.
    require(admitted(responses) <= set(access) and admitted(incomplete) <= {
        trace for trace, row in access.items() if row["status"] >= 500}, "EDGE_LOG_COVERAGE_INCOMPLETE")
    require(expected_wire <= responses.keys(), "EDGE_CAPTURE_COVERAGE_INCOMPLETE")
    pairs = [(item["channel_id"], utc(item["received_at"]).isoformat()) for item in successful]
    records = query(pairs) if pairs else []
    proof = attribute_heartbeats(successful, records, key, generation, slot, upstream)
    return {"heartbeats": proof, "source": "EDGE_ACK_EXACT_DB_JOIN",
        "access_complete": True, "http_409": counters["http_409"], "http_5xx": counters["http_5xx"],
        "incompatible": counters["incompatible"], "playback_201": counters["playback_201"],
        "playback_evidence": "PASS" if counters["playback_201"] else "UNVERIFIED",
    }


def decide_phase3(capture, logs, query, baseline, context, key, started, finished, opened_at, *, diagnostics=None):
    """Replay an archived Phase 3 observation using its original contract."""
    data = verified_payload("PHASE3_BASELINE", baseline, key, context, {"baseline_at", "baseline_refs"})
    baseline_hash = PlayerGate.baseline_hash(data)
    start, end, opened, before = map(utc, (started, finished, opened_at, data["baseline_at"]))
    if diagnostics is not None:
        diagnostics.record_baseline_count(data)
        diagnostics.set_window(started_at=start, finished_at=end, opened_at=opened, baseline_at=before)
    require(opened - timedelta(seconds=120) <= before <= opened <= start and end - start >= timedelta(seconds=120),
            "INVALID_PHASE3_OBSERVATION_WINDOW")
    evidence = observed_evidence(capture, logs, query, key, context["slot"], context["generation"], context["upstream"],
                                 start, end)
    refs = {item["device_ref"] for item in evidence["heartbeats"]}
    accepted = (set(data["baseline_refs"]) <= refs
                and all(evidence[field] == 0 for field in ("http_409", "http_5xx", "incompatible")))
    return sign_receipt("PHASE3_PLAYER", {**context, **evidence, "baseline_hash": baseline_hash,
        "baseline_at": data["baseline_at"], "baseline_refs": data["baseline_refs"],
        "opened_at": opened_at, "started_at": started, "finished_at": finished,
        "result": "FAIL" if not accepted else "PASS" if data["baseline_refs"] else "PASS_NO_ACTIVE_BASELINE",
    }, key), accepted


def decide(capture, logs, query, baseline, transition, key, started, finished, *, diagnostics=None):
    """Adjudicate and return the receipt alongside the existing gate decision."""
    receipt = adjudicate(capture, logs, query, baseline, transition, key, started, finished, diagnostics=diagnostics)
    return receipt, PlayerGate(key)(receipt, transition)


def read_only_query(read, pairs):
    # Interpolate only canonical UUIDs and validated UTC timestamp values.
    # Batch to bound arguments and memory for a long observation window.
    require(0 < len(pairs) <= MAX_ROWS, "ACK_QUERY_BOUNDS_EXCEEDED")
    result = []
    require(len(set(pairs)) == len(pairs), "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")
    for first in range(0, len(pairs), 100):
        rows = []
        for channel, received in pairs[first:first + 100]:
            require(identifier(channel), "INVALID_ACK_QUERY_CHANNEL")
            rows.append(f"('{channel}', TIMESTAMPTZ '{utc(received).isoformat()}')")
        values = ", ".join(rows)
        result.extend(read("SELECT json_build_object('channel_id',h.channel_id,'player_device_id',h.player_device_id,"
                           "'received_at',h.received_at) FROM public.player_heartbeats h JOIN (VALUES " + values +
                           ") AS a(channel_id,received_at) ON h.channel_id::text = a.channel_id AND "
                           "h.received_at = a.received_at"))
    require(len(result) <= len(pairs), "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")
    return [decode(row) for row in result]


def local_docker(*args, input=None, check=True):
    command = ["docker", "--host", "unix:///var/run/docker.sock", *args]
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
    result = subprocess.run(command, input=input, env=env, capture_output=True, timeout=30, check=False)
    if check:
        require(result.returncode == 0, "LOCAL_DOCKER_OBSERVER_FAILED")
    return result


def postgres_rows(container, sql):
    command = ('exec psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"')
    data = local_docker("exec", "-i", container, "sh", "-ec", command,
                        input=("BEGIN READ ONLY;\n" + sql + ";\nROLLBACK;\n").encode()).stdout.splitlines()
    require(len(data) >= 2 and data[0] == b"BEGIN" and data[-1] == b"ROLLBACK", "OBSERVER_READ_ONLY_TRANSACTION_FAILED")
    return data[1:-1]


def save_receipt(destination, receipt):
    from tempfile import mkstemp

    destination = Path(destination)
    require_private(destination.parent, directory=True)
    fd, path = mkstemp(prefix=".player-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(canonical(receipt) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(path, destination)
        sync_dir(destination.parent)
    finally:
        os.unlink(path)


def phase3_pipe_watchdog(stream, opened_ns, time_namespace, ready_line=None, checkpoint=None):
    """Bound transport independently of the shorter request-evidence deadline."""
    deadline = opened_ns + CAPTURE_PIPE_WATCHDOG_NS
    fd = stream.fileno()
    pending = b""
    captured = [] if ready_line is None else [ready_line]
    while True:
        remaining = deadline - monotonic_ns()
        require(remaining > 0, "PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED")
        if checkpoint is not None:
            checkpoint()
        delay = remaining if checkpoint is None else min(remaining, NANOSECONDS)
        readable, _, _ = select([fd], [], [], delay / NANOSECONDS)
        if not readable and checkpoint is not None and monotonic_ns() < deadline:
            continue
        require(bool(readable) and monotonic_ns() < deadline, "PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED")
        block = os.read(fd, 65536)
        if not block:
            if pending:
                require(len(captured) <= MAX_ROWS, "CAPTURE_STREAM_INVALID")
                captured.append(pending)
            return captured
        rows = (pending + block).split(b"\n")
        pending = rows.pop()
        for row in rows:
            require(len(captured) <= MAX_ROWS and len(row) < 2048, "CAPTURE_STREAM_INVALID")
            captured.append(row + b"\n")
            if len(captured) == 2:
                try:
                    clock = decode(row)
                except Stop:
                    continue
                if (type(clock) is dict and clock.get("type") == "monotonic_clock"
                        and clock.get("time_namespace") == time_namespace):
                    ready_ns = clock.get("ready_monotonic_ns")
                    if type(ready_ns) is int and 0 < ready_ns <= opened_ns:
                        deadline = ready_ns + CAPTURE_PIPE_WATCHDOG_NS
        require(len(pending) <= 2048, "CAPTURE_STREAM_INVALID")


def capture_input(stream, *, opened_monotonic_ns=None, time_namespace=None, ready_line=None, checkpoint=None):
    if opened_monotonic_ns is not None:
        return phase3_pipe_watchdog(stream, opened_monotonic_ns, time_namespace, ready_line, checkpoint)
    captured = []
    for index, line in enumerate(iter(lambda: stream.readline(2049), b"")):
        require(index <= MAX_ROWS and len(line) <= 2048, "CAPTURE_STREAM_INVALID")
        captured.append(line)
    return captured


def edge_logs(edge_id, started, receipt_path, *, diagnostics=None, promotion_latency=False):
    since = (utc(started) - timedelta(seconds=2)).isoformat() if started is not None else None
    receipt = Path(receipt_path)
    evidence = receipt.with_name(receipt.name + ".docker-logs.json")
    stream_incomplete = False

    def preserve(logs):
        nonlocal stream_incomplete
        if diagnostics is not None:
            diagnostics.record_edge_streams(logs)
            diagnostics.advance("write_edge_log_evidence")
        if promotion_latency:
            from player_latency import sanitized_log_evidence
            archive = sanitized_log_evidence(logs)
            stream_incomplete = archive["stream_integrity"] != "PASS"
        else:
            archive = logs.evidence()
        save_receipt(evidence, archive)
        if diagnostics is not None:
            diagnostics.advance("collect_edge_logs")

    capture = collect_docker_logs(lambda *args: local_docker(*args, check=False),
                                  edge_id, since=since, until=from_ns(time_ns()).isoformat(),
                                  preserve=preserve)
    lines = capture.lines(edge_id)
    if stream_incomplete:
        from player_latency import INCOMPLETE_LOG_STREAM
        return chain(lines, (INCOMPLETE_LOG_STREAM,))
    return lines


def verify_phase3_ingress(edge, state, context):
    effective, _ = edge.effective()
    require(edge.ingress_open() is True and effective.hash == edge.persisted().hash == context["config_hash"]
            and effective.active == BLUE_PAIR and effective.generation == 0
            and effective.target_for("api", "BLUE") == context["upstream"], "PHASE3_INGRESS_CHANGED")
    Controller._verify_route_bindings(state, effective)
    marker = edge.ingress_marker.lstat()
    require(read_private(edge.ingress_marker) == (context["config_hash"] + "\n").encode(), "PHASE3_INGRESS_CHANGED")
    return edge.boot_token(), marker.st_dev, marker.st_ino, marker.st_mtime_ns


def phase3_time_namespace():
    return os.readlink("/proc/self/ns/time")


def phase3_ready_line(pre_read_ready):
    if pre_read_ready is None:
        return None
    row = pre_read_ready.encode()
    require(0 < len(row) < 2048 and b"\r" not in row and b"\n" not in row, "CAPTURE_READY_INVALID")
    ready = decode(row)
    require(type(ready) is dict and set(ready) == {"type", "started_ns"} and ready["type"] == "ready",
            "CAPTURE_READY_INVALID")
    from_ns(ready["started_ns"])
    return row + b"\n"


def save_phase3_request_evidence(receipt, audit, diagnostic):
    destination = Path(receipt)
    path = destination.with_name(destination.name + ".requests.json")
    count = len(audit["requests"])
    diagnostic.request_evidence = {"status": "WRITE_FAILED", "records": count}
    save_receipt(path, audit)
    diagnostic.request_evidence = {"file": path.name, "records": count}


def _parser():
    parser = ObserverArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--postgres-id", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--receipt", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("baseline")
    actions.add_parser("phase3-baseline")
    for action in ("observe", "phase3-observe"):
        observe = actions.add_parser(action)
        observe.add_argument("--edge-id", required=True)
        observe.add_argument("--baseline", required=True)
        observe.add_argument("--started-at", required=True)
        observe.add_argument("--finished-at", required=True)
    observe.add_argument("--socket", required=True)
    observe.add_argument("--autosave", required=True)
    observe.add_argument("--opened-at", required=True)
    observe.add_argument("--capture-ready-json")
    for marker in ("opened", "started", "finished"):
        observe.add_argument(f"--{marker}-monotonic-ns", type=int, required=True)
    for action in ("promotion-baseline", "promotion-observe"):
        promotion = actions.add_parser(action)
        for option in ("socket", "autosave", "bindings", "edge-name", "clock-receipt"):
            promotion.add_argument("--" + option, required=True)
        promotion.add_argument("--expected-edge-config-revision", type=int, required=True)
        for role in ("blue", "frontend"):
            promotion.add_argument(f"--legacy-{role}-manifest")
            promotion.add_argument(f"--legacy-{role}-sha256")
        if action == "promotion-observe":
            for option in ("edge-id", "baseline", "opened-at", "started-at", "finished-at", "committed-state-sha256"):
                promotion.add_argument("--" + option, required=True)
            promotion.add_argument("--capture-ready-json")
            for marker in ("opened", "started", "finished"):
                promotion.add_argument(f"--{marker}-monotonic-ns", type=int, required=True)
    return parser


def main(argv=None):
    """Seal stdin from a supervised collector, using live local read-only inputs."""
    diagnostic = ObserverDiagnostics()
    try:
        args = _parser().parse_args(argv)
        diagnostic.action = args.action
        diagnostic.destination = Path(args.receipt)
        diagnostic.advance("validate_scope")
        ids = (args.postgres_id, args.edge_id) if hasattr(args, "edge_id") else (args.postgres_id,)
        require(sys.platform.startswith("linux") and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in ids),
                "OBSERVER_REQUIRES_EXACT_LOCAL_IDENTITIES")
        if args.action.startswith("promotion-"):
            require(args.expected_edge_config_revision > 0, "OBSERVER_EDGE_CONFIG_REVISION_MISMATCH")
            from promotion_player_observer import run_promotion

            return run_promotion(args, diagnostic)
        diagnostic.advance("load_state_and_key")
        store = StateStore(args.state_dir)
        state = store.load()
        key = read_private(args.key)
        phase3 = args.action.startswith("phase3-")
        if phase3:
            context = phase3_context(state, args.postgres_id)
            backend_id = state["slots"]["BLUE"]["ready"]["api"]["identity"]["container_id"]
            ids = (*ids, backend_id)
        def read(sql):
            return postgres_rows(args.postgres_id, sql)

        def identities():
            values = decode(local_docker("container", "inspect", *ids).stdout)
            require(len(values) == len(ids) and {item["Id"] for item in values} == set(ids)
                    and all(item["State"]["Running"] is True for item in values), "OBSERVER_ENDPOINT_IDENTITY_INVALID")
            identity = {item["Id"]: item["State"] for item in values}
            if phase3:
                require(all(identity[name]["Status"] == "running" and identity[name]["Health"]["Status"] == "healthy"
                            and all(identity[name][flag] is False for flag in ("OOMKilled", "Paused", "Restarting", "Dead"))
                            for name in (backend_id, args.postgres_id)), "PHASE3_ENDPOINT_UNHEALTHY")
                expected = state["slots"]["BLUE"]["ready"]["api"]["identity"]
                backend = next(item for item in values if item["Id"] == backend_id)
                require(backend["Image"] == expected["image_id"] and backend["RestartCount"] == expected["restart_count"]
                        and identity[backend_id]["Pid"] == expected["pid"]
                        and identity[backend_id]["StartedAt"] == expected["started_at"], "PHASE3_BACKEND_EPOCH_CHANGED")
                return [(item["Id"], item["Image"], item["RestartCount"], item["State"]["Pid"], item["State"]["StartedAt"])
                        for item in sorted(values, key=lambda item: item["Id"])]
            return [(item, identity[item]["Pid"], identity[item]["StartedAt"]) for item in ids]

        diagnostic.advance("inspect_endpoints")
        before = identities()
        if args.action in ("baseline", "phase3-baseline"):
            diagnostic.advance("capture_baseline")
            if phase3:
                time_namespace = phase3_time_namespace()
                receipt = capture_phase3_baseline(read, state, key, args.postgres_id,
                                                  baseline_monotonic_ns=monotonic_ns(), time_namespace=time_namespace)
            else:
                receipt = capture_baseline(read, state, key)
            diagnostic.record_baseline_count(receipt["payload"])
            diagnostic.advance("recheck_endpoints_and_state")
            require(before == identities() and store.load() == state, "OBSERVER_IDENTITY_OR_ROUTING_CHANGED")
            diagnostic.advance("write_baseline_receipt")
            save_receipt(args.receipt, receipt)
            diagnostic.advance("report_baseline")
            print("PLAYER_BASELINE_RECEIPT_WRITTEN")
            return 0
        diagnostic.advance("load_baseline")
        baseline = decode(read_private(args.baseline))
        def query(pairs):
            return read_only_query(read, pairs)

        if phase3:
            from player_observer_monotonic import MonotonicWindow, adjudicate_monotonic

            diagnostic.advance("verify_initial_ingress")
            edge = CaddyEdge(args.socket, args.autosave)
            token = verify_phase3_ingress(edge, state, context)
            audit_times = {"opened_at": args.opened_at, "started_at": args.started_at, "finished_at": args.finished_at}
            diagnostic.set_window(marker_at=from_ns(token[-1]), **{name: utc(value) for name, value in audit_times.items()})
            window = MonotonicWindow(args.opened_monotonic_ns, args.started_monotonic_ns, args.finished_monotonic_ns)
            window.validate()
            require(args.started_monotonic_ns <= monotonic_ns(), "OBSERVER_WINDOW_START_IN_FUTURE")
            time_namespace = phase3_time_namespace()
            diagnostic.advance("read_capture")
            capture = capture_input(sys.stdin.buffer, opened_monotonic_ns=window.opened_ns, time_namespace=time_namespace,
                                    ready_line=phase3_ready_line(args.capture_ready_json))
            diagnostic.counts["capture_records"] = len(capture)
            def available_edge_logs():
                diagnostic.advance("collect_edge_logs")
                lines = edge_logs(args.edge_id, None, args.receipt, diagnostics=diagnostic)
                diagnostic.advance("adjudicate")
                yield from lines

            diagnostic.advance("adjudicate")
            audit = {"version": 1, "window_monotonic_ns": {"opened": args.opened_monotonic_ns,
                     "start": args.started_monotonic_ns, "finish": args.finished_monotonic_ns}, "requests": []}
            try:
                receipt, accepted = adjudicate_monotonic(capture, available_edge_logs(), query, baseline, context, key, window,
                                                        time_namespace=time_namespace, audit_times=audit_times,
                                                        diagnostics=diagnostic, audit=audit["requests"])
            except (Exception, KeyboardInterrupt):
                try:
                    save_phase3_request_evidence(args.receipt, audit, diagnostic)
                except Exception:
                    # The diagnostic still reports the original failed stage.
                    pass
                raise
            diagnostic.record_adjudication(receipt)
            diagnostic.advance("write_request_evidence")
            save_phase3_request_evidence(args.receipt, audit, diagnostic)
            diagnostic.advance("recheck_ingress")
            require(verify_phase3_ingress(edge, state, context) == token, "PHASE3_INGRESS_CHANGED")
        else:
            diagnostic.advance("validate_transition")
            transition = state["routing"]["api_transition"]
            require(state["pending"] is None and transition is not None
                    and state["routing"]["generation"] == transition["generation"]
                    and state["routing"]["active"]["frontend"] == transition["prior_slot"], "OBSERVER_REQUIRES_API_ONLY_SWITCH")
            diagnostic.advance("read_capture")
            capture = capture_input(sys.stdin.buffer)
            diagnostic.counts["capture_records"] = len(capture)
            diagnostic.advance("collect_edge_logs")
            logs = edge_logs(args.edge_id, args.started_at, args.receipt, diagnostics=diagnostic)
            diagnostic.advance("adjudicate")
            receipt, accepted = decide(capture, logs, query, baseline, transition, key, args.started_at, args.finished_at,
                                       diagnostics=diagnostic)
            diagnostic.record_adjudication(receipt)
        diagnostic.advance("recheck_endpoints_and_state")
        require(before == identities() and store.load() == state, "OBSERVER_IDENTITY_OR_ROUTING_CHANGED")
        diagnostic.advance("verify_window_end")
        window_ended = (args.finished_monotonic_ns <= monotonic_ns() if phase3
                        else utc(args.finished_at) <= from_ns(time_ns()))
        require(window_ended, "OBSERVER_WINDOW_END_IN_FUTURE")
        diagnostic.advance("write_player_receipt")
        save_receipt(args.receipt, receipt)
        diagnostic.advance("report_player_result")
        print("PLAYER_OBSERVATION=PASS" if accepted else "PLAYER_OBSERVATION=FAIL")
        if not accepted:
            return diagnostic.fail(2)
    except InvalidObserverArguments as error:
        return diagnostic.fail(2, error)
    except (Exception, KeyboardInterrupt) as error:
        return diagnostic.fail(1, error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
