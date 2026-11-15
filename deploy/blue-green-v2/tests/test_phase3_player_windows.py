"""Exercise the unchanged Phase 3 adjudicator with 17-device continuity fixtures."""

from datetime import timedelta
from hashlib import sha256
import unittest
from uuid import UUID

from gates import device_ref, verified_payload
from player_observer import BASELINE_QUERY, capture_phase3_baseline, decide_phase3, from_ns, phase3_context
import test_player_observer
from test_player_observer import NOW, lines


SECOND = 1_000_000_000
UPSTREAM = "172.30.250.3:8000"
PG = "b" * 64


class PlayerWindow:
    """Disposable signed baseline, capture, Edge log, and exact DB ACK join."""

    def __init__(self, seed, baseline_ns, collector_ns):
        self.seed = seed
        self.key = sha256(f"phase3-window-{seed}".encode()).digest() + b"\0" * 16
        self.devices = tuple(str(UUID(int=seed * 100 + i)) for i in range(1, 18))
        self.replacement = str(UUID(int=seed * 100 + 99))
        self.channel = str(UUID(int=seed * 100 + 80))
        self.wire = [{"type": "ready", "started_ns": collector_ns}]
        self.logs = []
        self.rows = []
        state = test_player_observer.Phase3ContinuityTests().initial_state()
        self.context = phase3_context(state, PG)

        def read(sql):
            if sql != BASELINE_QUERY:
                raise AssertionError("unexpected baseline query")
            return lines([{"as_of": from_ns(baseline_ns).isoformat(), "devices": self.devices}])

        self.baseline = capture_phase3_baseline(read, state, self.key, PG)

    def event(self, ns, device=None, *, endpoint="heartbeat", status=200):
        trace = str(UUID(int=10_000_000 + self.seed * 100 + len(self.logs)))
        event = {"type": "response", "trace": trace, "endpoint": endpoint, "started_ns": ns, "status": status}
        if endpoint == "heartbeat" and status == 200:
            received = from_ns(ns).isoformat()
            event.update(channel_id=self.channel, received_at=received)
            self.rows.append({"channel_id": self.channel, "received_at": received, "player_device_id": device})
        self.wire.append(event)
        self.logs.append({"logger": "http.log.access", "ingress": "public", "route_generation": "0",
                          "received_at_ms": str(ns // 1_000_000), "request_id": trace, "endpoint": endpoint,
                          "status": status, "upstream_status": status, "upstream": UPSTREAM})

    def decide(self, opened, started, finished, done):
        capture = self.wire + [{"type": "done", "ended_ns": done, "kernel_dropped": 0,
                                "kernel_packets": 2 * len(self.wire)}]

        def query(pairs):
            return [row for row in self.rows if (row["channel_id"], row["received_at"]) in pairs]

        timings = [from_ns(value).isoformat() for value in (started, finished, opened)]
        receipt, accepted = decide_phase3(lines(capture), lines(self.logs), query, self.baseline,
                                         self.context, self.key, *timings)
        payload = verified_payload("PHASE3_PLAYER", receipt, self.key, self.context,
                                   set(receipt["payload"]) - set(self.context))
        return accepted, payload

    def refs(self, devices):
        return {device_ref(device, self.key) for device in devices}


def simulated_window(name, trial):
    offset = {"all_baseline": 0, "missing_one": 30, "replacement": 60, "pre_switch": 80}[name]
    seed = offset + trial + 1
    opened = int((NOW + timedelta(minutes=3 * seed)).timestamp()) * SECOND
    started = opened + SECOND
    finished = started + 120 * SECOND
    window = PlayerWindow(seed, opened - 5 * SECOND, opened - 10 * SECOND)
    missing = trial % len(window.devices)
    if name == "all_baseline":
        expected = list(window.devices)
    elif name == "pre_switch":
        expected = []
        for index, device in enumerate(window.devices, 1):
            window.event(opened - (len(window.devices) + 1 - index) * 100_000_000, device)
    else:
        expected = [device for index, device in enumerate(window.devices) if index != missing]
        if name == "replacement":
            expected.append(window.replacement)
    for index, device in enumerate(expected, 1):
        window.event(started + index * SECOND, device)
    playback = name == "all_baseline" and trial % 2 == 0
    if playback:
        window.event(started + 100 * SECOND, endpoint="playback-events", status=201)
    accepted, payload = window.decide(opened, started, finished, finished + SECOND)
    return window, accepted, payload, expected, playback


class Phase3PlayerWindowTests(unittest.TestCase):
    def check_windows(self, name, count):
        for index in range(count):
            with self.subTest(index=index + 1):
                window, accepted, payload, expected, playback = simulated_window(name, index)
                observed = {row["device_ref"] for row in payload["heartbeats"]}
                baseline = window.refs(window.devices)
                self.assertEqual(len(baseline), 17)
                self.assertEqual(set(payload["baseline_refs"]), baseline)
                self.assertEqual(observed, window.refs(expected))
                self.assertIs(accepted, name == "all_baseline")
                self.assertEqual(payload["result"], "PASS" if accepted else "FAIL")
                self.assertEqual(payload["playback_evidence"], "PASS" if playback else "UNVERIFIED")
                self.assertEqual((payload["generation"], payload["slot"], payload["upstream"]), (0, "BLUE", UPSTREAM))
                for raw in (*window.devices, window.replacement, window.channel):
                    self.assertNotIn(raw, str(payload))

    def test_twenty_all_baseline_windows_pass(self):
        self.check_windows("all_baseline", 20)

    def test_twenty_missing_one_windows_reject(self):
        self.check_windows("missing_one", 20)

    def test_ten_replacement_windows_reject(self):
        self.check_windows("replacement", 10)

    def test_ten_pre_switch_only_windows_receive_no_credit(self):
        self.check_windows("pre_switch", 10)


if __name__ == "__main__":
    unittest.main()
