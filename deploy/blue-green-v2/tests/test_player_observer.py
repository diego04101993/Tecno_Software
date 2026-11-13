from datetime import datetime, timedelta, timezone
import json
import unittest
from uuid import UUID

from gates import PlayerGate, device_ref, sign_receipt
from player_capture import Conversation, CaptureError
from player_observer import (
    BASELINE_QUERY, adjudicate, capture_baseline, capture_phase3_baseline, decide_phase3, phase3_context, read_only_query,
)
from release_state import Stop


NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
KEY = bytes(range(48))
CHANNEL = str(UUID(int=1))
DEVICES = tuple(str(UUID(int=i)) for i in range(2, 5))


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def nanos(seconds):
    return int((NOW.timestamp() + seconds) * 10**9)


def trace(i):
    return str(UUID(int=100 + i))


def lines(items):
    return [json.dumps(item) for item in items]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        release = {"revision": "a" * 40, "tree": "b" * 40,
                   "images": {"api": "sha256:" + "c" * 64, "frontend": "sha256:" + "d" * 64}}
        fields = {"slot": "GREEN", "prior_slot": "BLUE", "identity_hash": "e" * 64, "release": release}
        self.baseline = sign_receipt("BASELINE", {**fields, "previous_generation": 0, "baseline_at": stamp(-5),
                                      "baseline_refs": sorted(device_ref(device, KEY) for device in DEVICES[:2])}, KEY)
        self.transition = {**fields, "generation": 1, "switched_at": stamp(-1),
                           "baseline_hash": PlayerGate.baseline_hash(self.baseline["payload"])}
        self.rows = []
        self.capture = [{"type": "ready", "started_ns": nanos(-20)}]
        self.logs = []

    def traffic(self, i, device=None, seconds=1, status=200, slot="GREEN", generation=1, endpoint="heartbeat",
                capture=True, upstream=True):
        event = {"type": "response", "trace": trace(i), "endpoint": endpoint,
                 "started_ns": nanos(seconds), "status": status}
        if endpoint == "heartbeat" and status == 200:
            event.update(channel_id=CHANNEL, received_at=stamp(seconds))
            self.rows.append({"channel_id": CHANNEL, "received_at": stamp(seconds), "player_device_id": device})
        if capture:
            self.capture.append(event)
        self.logs.append({"logger": "http.log.access", "ingress": "public", "route_generation": str(generation),
                          "received_at_ms": str(nanos(seconds) // 1_000_000), "request_id": trace(i),
                          "endpoint": endpoint, "status": status, "upstream_status": status if upstream else None,
                          "upstream": f"backend-{slot.lower()}:8000"})

    def receipt(self, drops=0, tail=True):
        capture = self.capture + ([{"type": "done", "ended_ns": nanos(121), "kernel_dropped": drops,
                                    "kernel_packets": len(self.capture) * 2}] if tail else [])

        def query(pairs):
            return [row for row in self.rows if (row["channel_id"], row["received_at"]) in pairs]

        return adjudicate(lines(capture), lines(self.logs), query, self.baseline, self.transition, KEY, stamp(0), stamp(120))

    def passes(self, receipt):
        return PlayerGate(KEY, now=lambda: NOW + timedelta(seconds=121))(receipt, self.transition)

    def all(self):
        self.traffic(1, DEVICES[0], 1)
        self.traffic(2, DEVICES[1], 2)

    def test_all_baseline_with_playback_and_only_pseudonyms(self):
        self.all()
        self.traffic(3, seconds=3, status=201, endpoint="playback-events")
        receipt = self.receipt()
        self.assertTrue(self.passes(receipt))
        self.assertEqual(receipt["payload"]["playback_evidence"], "PASS")
        encoded = json.dumps(receipt)
        for raw in (*DEVICES, CHANNEL, trace(1), trace(2)):
            self.assertNotIn(raw, encoded)

    def test_missing_one_fails_even_with_duplicate_and_replacement(self):
        self.traffic(1, DEVICES[0], 1)
        self.assertFalse(self.passes(self.receipt()))
        self.traffic(2, DEVICES[0], 2)
        self.traffic(3, DEVICES[2], 3)
        self.assertFalse(self.passes(self.receipt()))

    def test_preswitch_and_old_blue_after_switch_do_not_count(self):
        self.traffic(1, DEVICES[0], 1)
        self.traffic(2, DEVICES[1], -2, generation=0, slot="BLUE", capture=False)
        self.traffic(3, DEVICES[1], 2, generation=0, slot="BLUE", capture=False)
        self.assertFalse(self.passes(self.receipt()))
        self.assertEqual(len(self.receipt()["payload"]["heartbeats"]), 1)

    def test_wrong_upstream_in_current_generation_is_incompatible(self):
        self.all()
        self.traffic(3, DEVICES[1], 3, slot="BLUE", capture=False)
        self.assertFalse(self.passes(self.receipt()))

    def test_401_does_not_count(self):
        self.traffic(1, DEVICES[0], 1)
        self.traffic(2, seconds=2, status=401)
        self.assertFalse(self.passes(self.receipt()))
        self.traffic(3, DEVICES[1], 3)
        self.assertTrue(self.passes(self.receipt()))

    def test_409_and_5xx_fail_even_if_all_baseline_observed(self):
        self.all()
        for status in (409, 500, 502):
            with self.subTest(status=status):
                self.traffic(3, seconds=3, status=status, capture=status != 502, upstream=status != 502)
                self.assertFalse(self.passes(self.receipt()))
                if status != 502:
                    self.capture.pop()
                self.logs.pop()

    def test_absent_playback_unverified(self):
        self.all()
        receipt = self.receipt()
        self.assertTrue(self.passes(receipt))
        self.assertEqual(receipt["payload"]["playback_evidence"], "UNVERIFIED")

    def test_wire_gap_missing_log_and_no_close_fail_closed(self):
        self.all()
        with self.assertRaisesRegex(Stop, "CAPTURE_KERNEL_ACCOUNTING_INVALID"):
            self.receipt(drops=1)
        with self.assertRaisesRegex(Stop, "CAPTURE_STREAM_INCOMPLETE"):
            self.receipt(tail=False)
        last = self.capture.pop()
        with self.assertRaisesRegex(Stop, "EDGE_WIRE_CORRELATION_INCOMPLETE"):
            self.receipt()
        self.capture.append(last)
        self.logs.pop()
        with self.assertRaisesRegex(Stop, "EDGE_LOG_COVERAGE_INCOMPLETE"):
            self.receipt()

    def test_duplicate_ack_or_changed_baseline_rejected(self):
        self.all()
        self.rows.append(self.rows[-1])
        with self.assertRaisesRegex(Stop, "HEARTBEAT_ACK_JOIN_NOT_UNIQUE"):
            self.receipt()
        self.rows.pop()
        self.transition["baseline_hash"] = "0" * 64
        with self.assertRaisesRegex(Stop, "BASELINE_DOES_NOT_MATCH_SWITCH"):
            self.receipt()

    def test_sql_lookup_refuses_unbounded_duplicate_or_untrusted_ids(self):
        for pairs in ([], [(CHANNEL, stamp(1))] * 2, [("bad'", stamp(1))]):
            with self.subTest(pairs=pairs), self.assertRaises(Stop):
                read_only_query(lambda sql: self.fail("Should not query"), pairs)
        self.assertEqual(read_only_query(lambda sql: ['{"ok":true}'], [(CHANNEL, stamp(1))]), [{"ok": True}])

    def test_baseline_reader_uses_eligible_devices_in_exact_120_second_window(self):
        state = {"pending": None, "rollback": None, "routing": {"generation": 0,
                 "active": {"api": "BLUE", "frontend": "BLUE"}}, "slots": {
                 "GREEN": {"phase": "READY", "release": self.transition["release"],
                           "ready": {"api": {"identity_hash": self.transition["identity_hash"]}}}}}

        def query(sql):
            self.assertEqual(sql, BASELINE_QUERY)
            return lines([{"as_of": stamp(-5), "devices": list(DEVICES[:2])}])

        self.assertEqual(capture_baseline(query, state, KEY), self.baseline)
        state["pending"] = {"stage": "DISPATCHED"}
        with self.assertRaisesRegex(Stop, "OBSERVER_REQUIRES_STEADY_PREDECESSOR"):
            capture_baseline(query, state, KEY)


class Phase3ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.capture = [{"type": "ready", "started_ns": nanos(-20)}]
        self.logs = []

    traffic = EvidenceTests.traffic

    def initial_state(self):
        return {"pending": None, "rollback": None, "legacy_blue": {"postgres_id": "b" * 64},
                "routing": {"generation": 0, "active": {"api": "BLUE", "frontend": "BLUE"},
                            "config_hash": "f" * 64, "api_transition": None},
                "slots": {"BLUE": {"phase": "ACTIVE", "ready": {"api": {
                    "identity_hash": "e" * 64, "identity": {"upstream": "172.30.250.3:8000"}}}}, "GREEN": None}}

    def phase3(self, state, statuses=(200, 200), before=-5):
        postgres = "b" * 64
        baseline = capture_phase3_baseline(lambda _: [json.dumps({"as_of": stamp(before), "devices": DEVICES[:2]})],
                                           state, KEY, postgres)
        for index, status in enumerate(statuses, 1):
            self.traffic(index, DEVICES[index - 1], index, status=status, slot="BLUE", generation=0)
            self.logs[-1]["upstream"] = "172.30.250.3:8000"
        tail = {"type": "done", "ended_ns": nanos(121), "kernel_dropped": 0, "kernel_packets": len(self.capture) * 2}
        return decide_phase3(lines(self.capture + [tail]), lines(self.logs), lambda pairs: self.rows, baseline,
                             phase3_context(state, postgres), KEY, stamp(0), stamp(120), stamp(-1))

    def test_initial_blue_same_legacy_endpoint_and_separate_receipt(self):
        receipt, accepted = self.phase3(self.initial_state())
        self.assertTrue(accepted)
        self.assertEqual(receipt["kind"], "PHASE3_PLAYER")
        self.assertEqual(receipt["payload"]["result"], "PASS")
        self.assertFalse(PlayerGate(KEY, now=lambda: NOW + timedelta(seconds=121))(receipt, {}))

    def test_initial_blue_missing_device_or_rejected_heartbeat(self):
        receipt, accepted = self.phase3(self.initial_state(), (200, 401))
        self.assertFalse(accepted)
        self.assertEqual(receipt["payload"]["result"], "FAIL")
        self.assertEqual(len(receipt["payload"]["heartbeats"]), 1)

    def test_initial_blue_rejects_stale_baseline_or_candidate(self):
        with self.assertRaisesRegex(Stop, "INVALID_PHASE3_OBSERVATION_WINDOW"):
            self.phase3(self.initial_state(), before=-122)
        state = self.initial_state()
        state["slots"]["GREEN"] = {"phase": "READY"}
        with self.assertRaisesRegex(Stop, "OBSERVER_REQUIRES_INITIAL_BLUE_BLUE"):
            phase3_context(state, "b" * 64)


class WireTests(unittest.TestCase):
    def test_segmented_tcp_and_retransmit_parse_only_expected_ack_fields(self):
        seen = []
        flow = Conversation(seen.append)
        flow.request.feed_tcp(100, True, b"")
        flow.response.feed_tcp(500, True, b"")
        request = (f"POST /api/player/heartbeat HTTP/1.1\r\nX-Request-ID: {trace(1)}\r\n"
                   "Authorization: Bearer SENSITIVE\r\nContent-Length: 9\r\n\r\nSENSITIVE").encode()
        body = json.dumps({"ok": True, "received_at": stamp(1), "channel_id": CHANNEL}).encode()
        response = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        flow.request.feed_tcp(121, False, request[20:])
        flow.request.feed_tcp(101, False, request[:20])
        flow.request.feed_tcp(101, False, request)
        flow.response.feed_tcp(501, False, response[:35])
        flow.response.feed_tcp(536, False, response[35:])
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0]), {"trace", "endpoint", "started_ns", "status", "channel_id", "received_at"})
        self.assertNotIn("SENSITIVE", json.dumps(seen))

    def test_nontelemetry_chunked_stream_can_share_keepalive(self):
        seen = []
        flow = Conversation(seen.append)
        flow.request.feed_http(b"GET /media/asset HTTP/1.1\r\n\r\n")
        flow.response.feed_http(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nHI\r\n0\r\n\r\n")
        flow.request.feed_http((f"POST /api/player/playback-events HTTP/1.1\r\nX-Request-ID: {trace(1)}\r\n"
                                "Content-Length: 2\r\n\r\n{}").encode())
        flow.response.feed_http(b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\n{}")
        self.assertEqual([event["status"] for event in seen], [201])

    def test_ambiguous_http_framing_stops_attribution(self):
        flow = Conversation(lambda value: self.fail("Ambiguous wire event must not emit"))
        with self.assertRaisesRegex(CaptureError, "AMBIGUOUS_HTTP_HEADER"):
            flow.request.feed_http(b"POST /api/player/heartbeat HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 5\r\n\r\n")


if __name__ == "__main__":
    unittest.main()
