"""Check strict ingress-latency evidence without changing Player continuity."""

from contextlib import redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

import promotion_player_observer as observer
from player_observer import edge_logs as read_edge_logs

from docker_logs import DockerLogCapture
from player_capture import Conversation, PLAYER_LATENCY_CAPTURE
from player_latency import INCOMPLETE_LOG_STREAM, evaluate_player_latency, sanitized_log_evidence, split_capture
from player_observer_monotonic import MonotonicWindow
from test_v267_monotonic_core import PromotionWindow


NS_PER_MS = 1_000_000
ROUTES = {
    "heartbeat": ("PLAYER_HEARTBEAT", "HEARTBEAT", 200, 5_000),
    "runtime-config": ("PLAYER_RUNTIME_CONFIG", "RUNTIME", 200, 8_000),
    "playback-events": ("PLAYER_PLAYBACK_EVENT", "PLAYBACK", 201, 5_000),
}


def encode(row):
    return json.dumps(row, separators=(",", ":")).encode()


class LatencyFixture:
    def __init__(self):
        self.promotion = PromotionWindow()
        self.clock = self.promotion.case
        self.logs = self.clock.fixture.logs
        self.wire = self.clock.fixture.wire
        self.window = MonotonicWindow(self.clock.opened, self.clock.started, self.clock.finished)
        self.capture_enabled = True

    def request(self, endpoint, seconds, *, started=None, status=None, changes=None):
        route_class, _, nominal_status, _ = ROUTES[endpoint]
        began = self.clock.started if started is None else started
        event = self.promotion.event(started=began, endpoint=endpoint,
                                     status=nominal_status if status is None else status)
        if endpoint == "runtime-config":
            event["type"] = "latency_response"
        edge = self.logs[-1]
        edge.update(route_class=route_class, slot=self.promotion.context["slot"], duration=seconds,
                    upstream_duration_ms="1", upstream_headers_ms="0")
        edge.update(changes or {})
        return event, edge

    def capture(self):
        done = {"type": "done", "ended_ns": self.clock.utc(self.clock.deadline),
                "ended_monotonic_ns": self.clock.deadline, "kernel_dropped": 0, "kernel_packets": 100}
        marker = [PLAYER_LATENCY_CAPTURE] if self.capture_enabled else []
        return [encode(row) for row in (*self.wire[:2], *marker, *self.wire[2:], done)]

    def log_lines(self):
        return [entry if isinstance(entry, bytes) else encode(entry) for entry in self.logs]

    def result(self):
        return evaluate_player_latency(self.capture(), self.log_lines(), self.promotion.context, self.window)


class PlayerLatencyTests(unittest.TestCase):
    def test_absolute_thresholds_at_ms_precision(self):
        for endpoint, (_, label, _, limit_ms) in ROUTES.items():
            for offset, expected in ((-1, "PASS"), (0, "FAIL"), (1, "FAIL")):
                with self.subTest(endpoint=endpoint, offset=offset):
                    fixture = LatencyFixture()
                    ms = limit_ms + offset
                    _, edge = fixture.request(endpoint, 0)
                    seconds = f"{ms // 1_000}.{ms % 1_000:03d}".encode()
                    fixture.logs[0] = encode(edge).replace(b'"duration":0', b'"duration":' + seconds)
                    report = fixture.result()
                    prefix = f"REAL_{label}_LATENCY"
                    self.assertEqual(report[prefix + "_COUNT"], 1)
                    self.assertEqual(report[prefix + "_MAX"], ms * NS_PER_MS)
                    self.assertEqual(report[prefix + "_STATUS"], expected)

    def test_lexical_decimal_duration_avoids_boundary_coercion(self):
        fixture = LatencyFixture()
        _, edge = fixture.request("runtime-config", 0)
        fixture.logs[0] = encode(edge).replace(b'"duration":0', b'"duration":7.999999999')
        report = fixture.result()
        self.assertEqual(report["REAL_RUNTIME_LATENCY_MAX"], 7_999_999_999)
        self.assertEqual(report["REAL_RUNTIME_LATENCY_STATUS"], "PASS")

    def test_native_caddy_fractional_milliseconds_and_seconds_round_trip(self):
        fixture = LatencyFixture()
        _, edge = fixture.request("runtime-config", 0)
        line = encode(edge).replace(b'"duration":0', b'"duration":6.8818342260000005')
        fixture.logs[0] = line.replace(b'"upstream_duration_ms":"1"', b'"upstream_duration_ms":5.273156')
        fixture.logs[0] = fixture.logs[0].replace(b'"upstream_headers_ms":"0"', b'"upstream_headers_ms":4.17218')
        report = fixture.result()
        self.assertEqual(report["REAL_RUNTIME_LATENCY_MAX"], 6_881_834_226)
        row, = report["requests"]
        self.assertEqual(row["UPSTREAM_DURATION_MS"], "5.273156")
        self.assertEqual(row["UPSTREAM_HEADER_LATENCY_MS"], "4.17218")

    def test_no_traffic_is_reported_per_endpoint(self):
        fixture = LatencyFixture()
        fixture.request("heartbeat", 0.005)
        report = fixture.result()
        self.assertEqual(report["REAL_HEARTBEAT_LATENCY_STATUS"], "PASS")
        self.assertEqual(report["PLAYER_TIMEOUT_CONTRACTS_REAL"], "UNVERIFIED_NO_TRAFFIC")
        for label in ("RUNTIME", "PLAYBACK"):
            self.assertEqual(report[f"REAL_{label}_LATENCY_COUNT"], 0)
            self.assertIsNone(report[f"REAL_{label}_LATENCY_MAX"])
            self.assertEqual(report[f"REAL_{label}_LATENCY_STATUS"], "UNVERIFIED_NO_TRAFFIC")

    def test_missing_log_missing_id_duplicate_and_bad_lines_never_pass(self):
        cases = ("missing_log", "missing_id", "duplicate_id", "missing_wire", "malformed", "incomplete",
                 "missing_capability", "missing_class", "wrong_class", "wrong_generation", "wrong_slot",
                 "wrong_upstream", "wrong_status", "missing_timing")
        for case in cases:
            with self.subTest(case=case):
                fixture = LatencyFixture()
                _, entry = fixture.request("runtime-config", 0.005)
                if case == "missing_log":
                    fixture.logs.clear()
                elif case == "missing_id":
                    entry.pop("request_id")
                elif case == "duplicate_id":
                    fixture.logs.append(entry.copy())
                elif case == "missing_wire":
                    fixture.wire.pop()
                elif case == "malformed":
                    fixture.logs.append(b"bad log data")
                elif case == "incomplete":
                    fixture.logs[0] = encode(entry)[:-1]
                elif case == "missing_capability":
                    fixture.capture_enabled = False
                elif case == "missing_class":
                    entry.pop("route_class")
                elif case == "wrong_class":
                    entry["route_class"] = "OTHER"
                elif case == "wrong_generation":
                    entry["route_generation"] = "4"
                elif case == "wrong_slot":
                    entry["slot"] = "BLUE"
                elif case == "wrong_upstream":
                    entry["upstream"] = "backend-blue:8000"
                elif case == "wrong_status":
                    entry["status"] = 201
                else:
                    entry.pop("upstream_duration_ms")
                result = fixture.result()
                self.assertEqual(result["PLAYER_TIMEOUT_CONTRACTS_REAL"], "FAIL_EVIDENCE")
                self.assertTrue(result["evidence_errors"])

    def test_unmatched_current_generation_runtime_5xx_is_not_silently_ignored(self):
        fixture = LatencyFixture()
        _, edge = fixture.request("runtime-config", 0.005, status=502)
        fixture.wire.pop()
        edge.pop("upstream_status")
        report = fixture.result()
        self.assertEqual(report["REAL_RUNTIME_LATENCY_STATUS"], "FAIL_EVIDENCE")

    def test_latency_uses_only_monotonic_membership_and_exact_route(self):
        fixture = LatencyFixture()
        for stamp in (fixture.window.start_ns - 1, fixture.window.finish_ns):
            fixture.request("runtime-config", 20, started=stamp)
        fixture.request("runtime-config", 0.020, started=fixture.window.finish_ns - 1)
        fixture.request("runtime-config", 0.010)
        report = fixture.result()
        self.assertEqual(report["REAL_RUNTIME_LATENCY_COUNT"], 2)
        self.assertEqual(report["REAL_RUNTIME_LATENCY_MAX"], 20 * NS_PER_MS)
        self.assertEqual(report["REAL_RUNTIME_LATENCY_STATUS"], "PASS")

    def test_native_rows_are_split_without_changing_continuity_envelope(self):
        fixture = LatencyFixture()
        fixture.request("heartbeat", 0.005)
        fixture.request("runtime-config", 0.005)
        fixture.request("playback-events", 0.005)
        frames = fixture.capture()
        continuity, enabled = split_capture(frames)
        self.assertTrue(enabled)
        self.assertEqual(continuity, [*frames[:2], frames[3], *frames[-2:]])

    def test_sensitive_row_is_not_copied_into_receipt(self):
        fixture = LatencyFixture()
        fixture.request("runtime-config", 0.005, changes={"Authorization": "example-fixture-secret"})
        report = fixture.result()
        self.assertEqual(report["REAL_RUNTIME_LATENCY_STATUS"], "FAIL_EVIDENCE")
        self.assertNotIn("example-fixture-secret", str(report))

    def test_impossible_duration_does_not_pass(self):
        for seconds, upstream in ((0, 0), (0.005, 9)):
            with self.subTest(seconds=seconds, upstream=upstream):
                fixture = LatencyFixture()
                fixture.request("runtime-config", seconds, changes={"upstream_duration_ms": upstream,
                                                                     "upstream_headers_ms": 0})
                self.assertEqual(fixture.result()["REAL_RUNTIME_LATENCY_STATUS"], "FAIL_EVIDENCE")

    def test_archived_streams_keep_technical_order_and_discard_sensitive_content(self):
        fixture = LatencyFixture()
        _, first = fixture.request("runtime-config", 0.005)
        _, second = fixture.request("heartbeat", 0.006)
        first.update(request={"uri": "/api/player/runtime-config?secret=qa-secret-a"},
                     headers={"Authorization": ["Bearer qa-secret-b"]},
                     player_id="09876543-1234-1234-1234-012345678987")
        second.update(body="qa-secret-c", cookies={"session": "qa-secret-d"})
        result = sanitized_log_evidence(DockerLogCapture(
            "a" * 64, "2026-11-16T01:00:00Z", "2026-11-16T01:00:01Z", "2026-11-16T01:00:02Z", "b" * 64,
            b"2026-11-16T01:00:00.001Z " + encode(first) + b"\n",
            b"2026-11-16T01:00:00.000Z " + encode(second) +
            b"\n2026-11-16T01:00:00.002Z {\"request\":\"qa-secret-e\"\n",
        ))
        self.assertEqual(result["merged_timeline"], [{"stream": "stderr", "record": 0},
                                                    {"stream": "stdout", "record": 0},
                                                    {"stream": "stderr", "record": 1}])
        self.assertEqual(result["streams"]["stdout"]["records"][0]["route_class"], "PLAYER_RUNTIME_CONFIG")
        self.assertEqual(result["streams"]["stderr"]["records"][1]["kind"], "MALFORMED_REDACTED")
        rendered = json.dumps(result)
        self.assertNotIn("qa-secret-", rendered)
        self.assertNotIn(first["player_id"], rendered)
        self.assertNotIn("RAW_DOCKER_LOG", rendered)

    def test_partial_docker_streams_cannot_create_unverified_no_traffic(self):
        for broken in (b"partial-timestamp credential-marker\n", b"2026-11-16T01:00:00Z {}"):
            with self.subTest(broken=broken):
                fixture = LatencyFixture()
                capture = DockerLogCapture(
                    "a" * 64, "2026-11-16T01:00:00Z", "2026-11-16T01:00:01Z", "2026-11-16T01:00:02Z",
                    "b" * 64, b"", broken,
                )

                def collect(_, edge, *, since, until, preserve):
                    self.assertEqual(edge, capture.container_id)
                    preserve(capture)
                    return capture

                with (patch("player_observer.collect_docker_logs", side_effect=collect),
                      patch("player_observer.save_receipt") as archive):
                    fixture.logs.extend(read_edge_logs(capture.container_id, None, "receipt",
                                                           promotion_latency=True))
                self.assertEqual(fixture.logs[-1], INCOMPLETE_LOG_STREAM)
                _, saved = archive.call_args.args
                self.assertEqual(saved["stream_integrity"], "FAIL_EVIDENCE")
                self.assertNotIn("credential-marker", str(saved))
                self.assertEqual(fixture.result()["PLAYER_TIMEOUT_CONTRACTS_REAL"], "FAIL_EVIDENCE")

    def test_capture_native_runtime_reports_no_body_path_or_query(self):
        trace = str(UUID(int=40))
        request = ("/api/player/runtime-config?secret=qa-query-marker HTTP/1.1\r\n"
                   "Authorization: Bearer qa-token-marker\r\nX-Request-ID: " + trace + "\r\n\r\n").encode()
        body = b"qa-private-response-marker"
        response = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                rows = []
                connection = Conversation(rows.append, phase3_monotonic=True, player_latency=enabled)
                connection.request.feed_http(b"GET " + request, (1_800_000_000_000_000_000, 100))
                connection.response.feed_http(response, (1_800_000_000_000_000_001, 101))
                if enabled:
                    self.assertEqual(len(rows), 1)
                    event, = rows
                    self.assertEqual((event["endpoint"], event["trace"], event["status"]), ("runtime-config", trace, 200))
                    self.assertNotIn("marker", str(event))
                else:
                    self.assertEqual(rows, [])


class PromotionLatencyReceiptTests(unittest.TestCase):
    def test_good_continuity_cannot_publish_receipt_when_latency_evidence_fails(self):
        stamp = "2026-11-16T08:00:00+00:00"
        for result, expected in (("FAIL", 2), ("FAIL_EVIDENCE", 2), ("UNVERIFIED_NO_TRAFFIC", 0)):
            with self.subTest(result=result):
                args = SimpleNamespace(bindings="bindings", committed_state_sha256="state", opened_monotonic_ns=1,
                                       started_monotonic_ns=2, finished_monotonic_ns=120_000_000_002,
                                       opened_at=stamp, started_at=stamp, finished_at=stamp, receipt="receipt",
                                       clock_receipt="clock")
                routing = SimpleNamespace(context={"generation": 3, "slot": "GREEN", "upstream": "backend-green:8000",
                                                   "backend_container": "a" * 64},
                                          check_state=Mock(), recheck=Mock())
                diagnostic = Mock()
                diagnostic.fail.return_value = 2
                clock = {"time_namespace": "time:[123]"}
                baseline = {"baseline_at": stamp, "baseline_refs": ["f" * 64]}

                def observe_window(*_, latency_audit):
                    latency_audit["PLAYER_TIMEOUT_CONTRACTS_REAL"] = result
                    heartbeats = [{"device_ref": "f" * 64}]
                    return {"heartbeats": heartbeats}, True, args.finished_monotonic_ns + 1

                output = StringIO()
                with (patch.object(observer.FrozenPromotion, "capture", return_value=routing),
                      patch.object(observer, "monotonic_ns", return_value=args.finished_monotonic_ns),
                      patch.object(observer, "_attest_baseline",
                                   return_value=(baseline, {"switched_at": stamp}, Mock(return_value=True))),
                      patch.object(observer, "_endpoints", return_value=()),
                      patch.object(observer, "_clock_identity", return_value=clock),
                      patch.object(observer, "_read_window", side_effect=observe_window),
                      patch.object(observer, "save_phase3_request_evidence") as audit,
                      patch.object(observer, "save_receipt") as write, redirect_stdout(output)):
                    self.assertEqual(observer._observe(args, diagnostic, Mock(), clock, Mock(), b"a" * 48), expected)
                audit.assert_called_once()
                self.assertIn("PLAYER_CONTINUITY_GATE=PASS_ALL_BASELINE", output.getvalue().splitlines())
                self.assertIn(f"PLAYER_TIMEOUT_CONTRACTS_REAL={result}", output.getvalue().splitlines())
                if expected:
                    write.assert_not_called()
                else:
                    write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
