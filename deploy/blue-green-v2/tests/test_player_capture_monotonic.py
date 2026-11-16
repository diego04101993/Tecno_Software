"""Exercise monotonic event stamps through the passive collector's TCP parser."""

import json
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch

from player_capture import CaptureError, Conversation, observe
from test_player_observer import CHANNEL, stamp, trace


WINDOW_FINISH = 200
UTC_BASE = 1_800_000_000_000_000_000
SECOND = 1_000_000_000


def request(index, body=b""):
    return (f"POST /api/player/heartbeat HTTP/1.1\r\nX-Request-ID: {trace(index)}\r\n"
            f"Authorization: Bearer SYNTHETIC_SECRET\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body


def response(index):
    payload = json.dumps({"ok": True, "received_at": stamp(index), "channel_id": CHANNEL}).encode()
    return f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload


class CollectorMonotonicTests(unittest.TestCase):
    def setUp(self):
        self.events = []

    def flow(self):
        flow = Conversation(self.events.append, phase3_monotonic=True)
        flow.request.feed_tcp(1000, True, b"", observed=(UTC_BASE + 100, 100))
        flow.response.feed_tcp(2000, True, b"", observed=(UTC_BASE + 100, 100))
        return flow

    def send(self, stream, ns, seq, data):
        stream.feed_tcp(seq, False, data, observed=(UTC_BASE + ns, ns))

    def check_response(self, event, *, index, start, complete):
        self.assertEqual(event, {"trace": trace(index), "endpoint": "heartbeat", "status": 200,
                                 "started_ns": UTC_BASE + start, "request_start_monotonic_ns": start,
                                 "response_complete_ns": UTC_BASE + complete,
                                 "response_complete_monotonic_ns": complete,
                                 "channel_id": CHANNEL, "received_at": stamp(index)})
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(event))

    def test_first_request_byte_inside_even_when_headers_and_response_finish_later(self):
        flow = self.flow()
        req, ack = request(1), response(1)
        self.send(flow.request, WINDOW_FINISH - 1, 1001, req[:1])
        self.send(flow.request, WINDOW_FINISH + 3, 1002, req[1:])
        self.send(flow.response, WINDOW_FINISH + 5, 2001, ack[:-1])
        self.assertEqual(self.events, [])
        self.assertEqual(flow.outstanding(), [{"trace": trace(1), "endpoint": "heartbeat",
                                              "started_ns": UTC_BASE + WINDOW_FINISH - 1,
                                              "request_start_monotonic_ns": WINDOW_FINISH - 1}])
        self.send(flow.response, WINDOW_FINISH + 6, 2001 + len(ack) - 1, ack[-1:])
        event, = self.events
        self.check_response(event, index=1, start=WINDOW_FINISH - 1, complete=WINDOW_FINISH + 6)

    def test_early_tail_does_not_backdate_later_first_byte(self):
        flow = self.flow()
        req = request(2)
        self.send(flow.request, WINDOW_FINISH - 1, 1011, req[10:])
        self.send(flow.request, WINDOW_FINISH + 1, 1001, req[:10])
        self.send(flow.response, WINDOW_FINISH + 5, 2001, response(2))
        event, = self.events
        self.check_response(event, index=2, start=WINDOW_FINISH + 1, complete=WINDOW_FINISH + 5)

    def test_buffered_next_request_retains_capture_stamp_across_previous_body_gap(self):
        flow = self.flow()
        first, second = request(3, b"somebody"), request(4)
        split = len(first) - 3
        self.send(flow.request, 180, 1001, first[:split])
        self.send(flow.request, 198, 1001 + len(first), second)
        self.send(flow.request, 199, 1001 + len(first), second)  # Retransmit preserves first capture stamp.
        self.send(flow.request, 202, 1001 + split, first[split:])
        self.send(flow.response, 210, 2001, response(3) + response(4))
        one, two = self.events
        self.check_response(one, index=3, start=180, complete=210)
        self.check_response(two, index=4, start=198, complete=210)

    def test_response_completion_waits_for_last_missing_bytes(self):
        flow = self.flow()
        self.send(flow.request, 190, 1001, request(5))
        ack = response(5)
        self.send(flow.response, 200, 2001, ack[:10])
        self.send(flow.response, 205, 2021, ack[20:])
        self.assertEqual(self.events, [])
        self.send(flow.response, 210, 2011, ack[10:20])
        event, = self.events
        self.check_response(event, index=5, start=190, complete=210)

    def test_legacy_default_protocol_remains_exact(self):
        flow = Conversation(self.events.append)
        flow.request.feed_tcp(1000, True, b"")
        flow.response.feed_tcp(2000, True, b"")
        req = request(6)
        with patch("player_capture.time_ns", return_value=UTC_BASE + 205):
            flow.request.feed_tcp(1001, False, req[:5])
            flow.request.feed_tcp(1006, False, req[5:])
        self.assertEqual(flow.outstanding(), [{"trace": trace(6), "endpoint": "heartbeat", "started_ns": UTC_BASE + 205}])
        flow.response.feed_tcp(2001, False, response(6))
        event, = self.events
        self.assertEqual(event, {"trace": trace(6), "endpoint": "heartbeat", "started_ns": UTC_BASE + 205,
                                 "status": 200, "channel_id": CHANNEL, "received_at": stamp(6)})

    def test_capture_deadline_reports_pending_request_and_preserves_ready_protocol(self):
        ready = 10 * SECOND
        deadline = ready + 165 * SECOND
        local_ip, upstream_ip = "172.30.250.2", "172.30.250.3"
        local, upstream = socket.inet_aton(local_ip), socket.inet_aton(upstream_ip)
        req = request(7)
        ack = response(7)
        clock = ready
        events = iter((
            (ready + 1, (local, 32000, 8000, 1000, 2, b"")),
            (ready + 2, (upstream, 8000, 32000, 2000, 2, b"")),
            (ready + 3, (local, 32000, 8000, 1001, 0, req)),
            (ready + 119 * SECOND, (local, 32000, 8000, 1001 + len(req), 0, request(8))),
            (ready + 125 * SECOND, (upstream, 8000, 32000, 2001, 0, ack)),
            (deadline, (upstream, 8000, 32000, 2001 + len(ack), 0, response(8))),
        ))

        def receive(_limit):
            nonlocal clock
            clock, frame = next(events)
            return frame

        capture = MagicMock()
        capture.__enter__.return_value = capture
        capture.recv.side_effect = receive
        capture.getsockopt.return_value = struct.pack("II", 6, 0)
        with (patch("player_capture.socket.AF_PACKET", 17, create=True),
              patch("player_capture.socket.socket", return_value=capture),
              patch("player_capture.constrain") as constrain,
              patch("player_capture.parse_packet", side_effect=lambda frame: frame) as parse,
              patch("player_capture.os.readlink", return_value="time:[123]"),
              patch("player_capture.monotonic_ns", side_effect=lambda: clock),
              patch("player_capture.time_ns", side_effect=lambda: UTC_BASE + clock)):
            observe("eth0", local_ip, upstream_ip, 8000, 165, self.events.append, phase3_monotonic=True)
        constrain.assert_called_once_with(capture, "eth0", local, upstream, 8000)
        self.assertEqual(parse.call_count, 5)
        ready_frame, domain, completed, incomplete, done = self.events
        self.assertEqual(ready_frame, {"type": "ready", "started_ns": UTC_BASE + ready})
        self.assertEqual(domain, {"type": "monotonic_clock", "ready_monotonic_ns": ready,
                                  "deadline_monotonic_ns": deadline, "time_namespace": "time:[123]"})
        self.assertEqual(completed.pop("type"), "response")
        self.check_response(completed, index=7, start=ready + 3, complete=ready + 125 * SECOND)
        self.assertEqual(incomplete, {"type": "incomplete", "trace": trace(8), "endpoint": "heartbeat",
                                      "started_ns": UTC_BASE + ready + 119 * SECOND,
                                      "request_start_monotonic_ns": ready + 119 * SECOND})
        self.assertEqual(done, {"type": "done", "ended_ns": UTC_BASE + deadline, "ended_monotonic_ns": deadline,
                                "kernel_packets": 6, "kernel_dropped": 0})

    def test_other_lifetimes_are_not_authorized_for_phase3(self):
        with patch("player_capture.socket.socket") as raw, self.assertRaisesRegex(CaptureError, "INVALID_PHASE3_CAPTURE_LIFETIME"):
            observe("eth0", "172.30.250.2", "172.30.250.3", 8000, 166, self.events.append, phase3_monotonic=True)
        raw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
