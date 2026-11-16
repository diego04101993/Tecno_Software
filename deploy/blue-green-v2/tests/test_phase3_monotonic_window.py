"""Offline tests for monotonic Phase 3 admission and terminal ACK evidence."""

from datetime import timedelta, timezone
import unittest
from uuid import UUID

from gates import sign_receipt, verified_payload
from player_observer import from_ns
from release_state import Stop
from test_phase3_player_windows import NOW, PlayerWindow, SECOND, UPSTREAM, lines


MS = 1_000_000
NS = "time:[123]"
OBSERVATION_NS = 120 * SECOND
CAPTURE_NS = 165 * SECOND


class MonotonicFixture:
    def __init__(self, seed, *, opened=None, started=None, ready=None, started_utc=None, ready_utc=None, time_namespace=NS):
        self.started = started if started is not None else seed * 200 * SECOND
        self.opened = opened if opened is not None else self.started - 500 * MS
        self.ready = ready if ready is not None else self.started - SECOND
        self.finished = self.started + OBSERVATION_NS
        self.deadline = self.ready + CAPTURE_NS
        self.started_utc = (started_utc if started_utc is not None else int(NOW.timestamp() + seed * 200) * SECOND)
        self.ready_utc = ready_utc if ready_utc is not None else self.started_utc - SECOND
        self.time_namespace = time_namespace
        self.fixture = PlayerWindow(seed, self.started_utc - 5 * SECOND, self.ready_utc)
        baseline = {**self.fixture.baseline["payload"], "baseline_monotonic_ns": self.opened - 5 * SECOND,
                    "time_namespace": time_namespace}
        self.fixture.baseline = sign_receipt("PHASE3_BASELINE", baseline, self.fixture.key)
        self.fixture.wire.append({"type": "monotonic_clock", "ready_monotonic_ns": self.ready,
                                  "deadline_monotonic_ns": self.deadline, "time_namespace": time_namespace})
        self.devices = self.fixture.devices
        self.queries = []

    def utc(self, monotonic):
        return self.started_utc + (monotonic - self.started)

    def event(self, started, device=None, *, complete=None, started_utc=None, completed_utc=None, ack_utc=None,
              ack_text=None, edge_utc=None, status=200, endpoint="heartbeat", generation=0, terminal=True):
        index = len(self.fixture.wire)
        trace = str(UUID(int=90_000_000 + self.fixture.seed * 100 + index))
        when = self.utc(started) if started_utc is None else started_utc
        capture = {"type": "response" if terminal else "incomplete", "trace": trace, "endpoint": endpoint,
                   "started_ns": when, "request_start_monotonic_ns": started}
        if terminal:
            complete = started + 2 * MS if complete is None else complete
            completed = self.utc(complete) if completed_utc is None else completed_utc
            capture.update(status=status, response_complete_monotonic_ns=complete, response_complete_ns=completed)
            if endpoint == "heartbeat" and status == 200:
                ack = from_ns(completed - MS if ack_utc is None else ack_utc)
                capture.update(channel_id=self.fixture.channel, received_at=ack.isoformat() if ack_text is None else ack_text)
                self.fixture.rows.append({"channel_id": self.fixture.channel, "received_at": ack.isoformat(),
                                          "player_device_id": device})
            self.fixture.logs.append({"logger": "http.log.access", "ingress": "public", "route_generation": str(generation),
                                      "received_at_ms": str((when if edge_utc is None else edge_utc) // MS),
                                      "request_id": trace, "endpoint": endpoint, "status": status,
                                      "upstream_status": status, "upstream": UPSTREAM})
        self.fixture.wire.append(capture)
        return capture

    def normal(self, devices=None):
        cohort = self.devices if devices is None else devices
        for index, device in enumerate(cohort, 1):
            self.event(self.started + index * SECOND, device)

    def adjudicate(self, *, time_namespace=None, diagnostics=None, audit=None):
        from player_observer_monotonic import MonotonicWindow, adjudicate_monotonic

        case = self.fixture
        capture = case.wire + [{"type": "done", "ended_ns": self.utc(self.deadline), "ended_monotonic_ns": self.deadline,
                                "kernel_dropped": 0, "kernel_packets": 2 * len(case.wire)}]

        def query(pairs):
            self.queries.extend(pairs)
            return [row for row in case.rows if (row["channel_id"], row["received_at"]) in pairs]

        window = MonotonicWindow(self.opened, self.started, self.finished)
        receipt, accepted = adjudicate_monotonic(lines(capture), lines(case.logs), query, case.baseline, case.context,
                                                 case.key, window, time_namespace=time_namespace or self.time_namespace,
                                                 diagnostics=diagnostics,
                                                 audit=audit)
        payload = verified_payload("PHASE3_PLAYER", receipt, case.key, case.context, set(receipt["payload"]) - set(case.context))
        return accepted, payload

    def credited(self, payload):
        return {item["device_ref"] for item in payload["heartbeats"]} & set(payload["baseline_refs"])


class Phase3MonotonicWindowTests(unittest.TestCase):
    def check(self, fixture, expected):
        accepted, payload = fixture.adjudicate()
        expected_refs = fixture.fixture.refs(expected)
        self.assertIs(accepted, len(expected_refs) == 17)
        self.assertEqual(fixture.credited(payload), expected_refs)
        self.assertEqual(payload["result"], "PASS" if accepted else "FAIL")
        self.assertEqual((payload["generation"], payload["slot"], payload["upstream"]), (0, "BLUE", UPSTREAM))
        return payload

    def rejects(self, fixture, reason=None):
        if reason is None:
            try:
                accepted, _ = fixture.adjudicate()
            except Stop:
                return
            self.assertIs(accepted, False)
        else:
            with self.assertRaisesRegex(Stop, reason):
                fixture.adjudicate()

    def test_hundred_boundary_start_windows(self):
        for index in range(100):
            with self.subTest(index=index):
                case = MonotonicFixture(100 + index)
                case.normal(case.devices[:-1])
                offset = (index % 5) - 2
                case.event(case.started + offset, case.devices[-1], edge_utc=case.started_utc - MS + index,
                           complete=case.started + (index + 2) * MS,
                           ack_utc=case.started_utc + index * MS)
                self.check(case, case.devices if offset >= 0 else case.devices[:-1])

    def test_hundred_boundary_end_windows(self):
        for index in range(100):
            with self.subTest(index=index):
                case = MonotonicFixture(300 + index)
                case.normal(case.devices[:-1])
                offset = (index % 5) - 2
                case.event(case.finished + offset, case.devices[-1], edge_utc=case.utc(case.finished - MS),
                           complete=case.finished + (index + 3) * MS,
                           ack_utc=case.utc(case.finished + (index + 1) * MS))
                self.check(case, case.devices if offset < 0 else case.devices[:-1])

    def test_hundred_inflight_cross_finish_windows(self):
        for index in range(100):
            with self.subTest(index=index):
                case = MonotonicFixture(500 + index)
                case.normal(case.devices[:-1])
                event = case.event(case.finished - index - 1, case.devices[-1], complete=case.finished + (index + 5) * MS,
                                   ack_utc=case.utc(case.finished + (index + 1) * MS))
                self.assertLess(event["request_start_monotonic_ns"], case.finished)
                self.assertGreater(event["response_complete_monotonic_ns"], case.finished)
                self.check(case, case.devices)

    def test_twenty_all_baseline_windows(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(700 + index)
                case.normal()
                case.event(case.finished - 1, complete=case.finished + MS, endpoint="playback-events", status=201)
                self.assertEqual(self.check(case, case.devices)["playback_evidence"], "PASS")

    def test_twenty_missing_one_windows(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(800 + index)
                cohort = case.devices[:index % 17] + case.devices[index % 17 + 1:]
                case.normal(cohort)
                self.check(case, cohort)

    def test_twenty_replacement_windows(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(900 + index)
                case.normal((*case.devices[:-1], case.fixture.replacement))
                self.check(case, case.devices[:-1])

    def test_twenty_preswitch_windows_have_zero_credit(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(1000 + index)
                for position, device in enumerate(case.devices, 1):
                    case.event(case.started - (18 - position) * MS, device, complete=case.started + position * MS)
                self.check(case, ())
                self.assertEqual(case.queries, [])

    def test_twenty_inflight_valid_windows(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(1100 + index)
                case.normal(case.devices[:-1])
                case.event(case.finished - index - 1, case.devices[-1], complete=case.finished + 5 * MS)
                self.check(case, case.devices)

    def test_twenty_postwindow_heartbeats_cannot_add_credit(self):
        for index in range(20):
            with self.subTest(index=index):
                case = MonotonicFixture(1200 + index)
                case.normal(case.devices[:-1])
                after = case.event(case.finished + index, case.devices[-1])
                self.check(case, case.devices[:-1])
                self.assertNotIn((case.fixture.channel, after["received_at"]), case.queries)

    def test_terminal_deadline_and_incomplete_requests_fail_closed(self):
        case = MonotonicFixture(1300)
        case.normal()
        case.event(case.finished - 1, case.devices[-1], terminal=False)
        self.rejects(case)
        for complete in ("after", "at"):
            with self.subTest(complete=complete):
                case = MonotonicFixture(1301)
                case.normal(case.devices[:-1])
                case.event(case.finished - 1, case.devices[-1], complete=case.deadline + (1 if complete == "after" else 0))
                self.rejects(case)

    def test_401_is_no_credit_and_409_5xx_fail(self):
        case = MonotonicFixture(1400)
        case.normal(case.devices[:-1])
        case.event(case.finished - 1, status=401)
        self.check(case, case.devices[:-1])
        for status in (409, 500, 502):
            with self.subTest(status=status):
                case = MonotonicFixture(1401)
                case.normal()
                case.event(case.finished - 1, status=status)
                self.rejects(case)

    def test_invalid_ack_timestamp_and_nonunique_ack_fail(self):
        for offset in (None, timezone(timedelta(hours=1))):
            with self.subTest(offset=offset):
                case = MonotonicFixture(1500)
                case.normal(case.devices[:-1])
                began = case.started + SECOND
                ack = from_ns(case.utc(began))
                stamp = (ack.replace(tzinfo=None) if offset is None else ack.astimezone(offset)).isoformat()
                case.event(began, case.devices[-1], ack_text=stamp)
                self.rejects(case, "INVALID_TIMESTAMP")
        case = MonotonicFixture(1501)
        case.normal()
        case.fixture.rows.append(dict(case.fixture.rows[-1]))
        self.rejects(case, "HEARTBEAT_ACK_JOIN_NOT_UNIQUE")

    def test_generation_mismatch_and_ack_trace_mismatch_fail_closed(self):
        case = MonotonicFixture(1600)
        case.normal(case.devices[:-1])
        case.event(case.started + 60 * SECOND, case.devices[-1], generation=1)
        self.rejects(case)
        case = MonotonicFixture(1601)
        case.normal()
        case.fixture.logs[-1]["request_id"] = str(UUID(int=999))
        self.rejects(case)

    def test_crossing_audit_records_exact_wire_and_ack_identity(self):
        case = MonotonicFixture(1700)
        case.normal(case.devices[:-1])
        device = case.devices[-1]
        event = case.event(case.finished - 1, device, complete=case.finished + 5 * MS)
        after = case.event(case.finished, case.fixture.replacement)
        audit = []
        self.assertTrue(case.adjudicate(audit=audit)[0])
        rows = {row["REQUEST_ID"]: row for row in audit}
        row = rows[event["trace"]]
        self.assertEqual(row["PLAYER_PSEUDONYM"], next(iter(case.fixture.refs([device]))))
        self.assertEqual(row["ACK_RECEIVED_AT"], event["received_at"])
        self.assertEqual((row["REQUEST_START_MONOTONIC_NS"], row["RESPONSE_COMPLETE_MONOTONIC_NS"]),
                         (case.finished - 1, case.finished + 5 * MS))
        self.assertEqual((row["WINDOW_START_MONOTONIC_NS"], row["WINDOW_FINISH_MONOTONIC_NS"]),
                         (case.started, case.finished))
        self.assertEqual((row["ROUTE_GENERATION"], row["SLOT"], row["UPSTREAM"], row["HTTP_STATUS"]),
                         (0, "BLUE", UPSTREAM, 200))
        self.assertEqual((row["REQUEST_ELIGIBLE"], row["ACK_JOIN_RESULT"], row["FINAL_ADJUDICATION"]),
                         (True, "UNIQUE", "CREDITED"))
        self.assertEqual((rows[after["trace"]]["REQUEST_ELIGIBLE"], rows[after["trace"]]["FINAL_ADJUDICATION"]),
                         (False, "NO_CREDIT"))
        for field in ("REQUEST_START_UTC", "RESPONSE_COMPLETE_UTC"):
            self.assertRegex(row[field], r"T\d{2}:\d{2}:\d{2}\.\d{9}Z$")

    def test_correlation_failure_keeps_request_id_and_ack(self):
        case = MonotonicFixture(1701)
        case.normal(case.devices[:-1])
        event = case.event(case.finished - 1, case.devices[-1], complete=case.finished + MS, generation=1)
        audit = []
        with self.assertRaisesRegex(Stop, "EDGE_WIRE_CORRELATION_INCOMPLETE"):
            case.adjudicate(audit=audit)
        row, = [row for row in audit if row["REQUEST_ID"] == event["trace"]]
        self.assertEqual((row["ACK_RECEIVED_AT"], row["FINAL_ADJUDICATION"]), (event["received_at"], "FAIL_CORRELATION"))
        self.assertIsNone(row["PLAYER_PSEUDONYM"])
        self.assertEqual(len(case.queries), 16)
        independently_verified = [entry for entry in audit if entry["PLAYER_PSEUDONYM"] is not None]
        self.assertEqual(len(independently_verified), 16)
        self.assertTrue(all(entry["FINAL_ADJUDICATION"] == "VERIFIED_NO_WINDOW_RECEIPT"
                            for entry in independently_verified))

    def test_wrong_time_namespace_fails_closed(self):
        case = MonotonicFixture(1702)
        case.normal()
        with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_BASELINE_INVALID"):
            case.adjudicate(time_namespace="time:[456]")


if __name__ == "__main__":
    unittest.main()
