"""Exercise generic routing attribution without changing monotonic admission."""

import unittest

from player_observer_monotonic import MonotonicWindow, observe_committed_slot
from release_state import Stop
from test_phase3_monotonic_window import MS, NS, MonotonicFixture
from test_phase3_player_windows import SECOND, UPSTREAM, lines


class PromotionWindow:
    def __init__(self, generation=3, slot="GREEN", previous_upstream=UPSTREAM):
        self.case = MonotonicFixture(1800)
        self.context = {"generation": generation, "slot": slot,
                        "upstream": f"backend-{slot.lower()}:8000",
                        "prior_slot": "BLUE" if slot == "GREEN" else "GREEN",
                        "prior_upstream": previous_upstream}
        self.devices = self.case.devices
        self.audit = []

    def event(self, device=None, *, started=None, generation=None, upstream=None, edge_only=False, **kwargs):
        if started is None:
            started = self.case.started + len(self.case.fixture.wire) * SECOND
        event = self.case.event(started, device, generation=self.context["generation"] if generation is None else generation,
                                **kwargs)
        if event["type"] == "response":
            self.case.fixture.logs[-1]["upstream"] = self.context["upstream"] if upstream is None else upstream
        if edge_only:
            self.case.fixture.wire.pop()
        return event

    def all_devices(self, devices=None):
        for device in self.devices if devices is None else devices:
            self.event(device)

    def row(self, event):
        row, = [row for row in self.audit if row["REQUEST_ID"] == event["trace"]]
        return row

    def run(self, time_namespace=NS):
        case = self.case
        fixture = case.fixture
        capture = fixture.wire + [{"type": "done", "ended_ns": case.utc(case.deadline),
                                   "ended_monotonic_ns": case.deadline, "kernel_dropped": 0, "kernel_packets": 100}]

        def query(pairs):
            case.queries.extend(pairs)
            return [record for record in fixture.rows if (record["channel_id"], record["received_at"]) in pairs]

        return observe_committed_slot(lines(capture), lines(fixture.logs), query, fixture.refs(self.devices),
                                      self.context, fixture.key, MonotonicWindow(case.opened, case.started, case.finished),
                                      time_namespace=time_namespace, audit=self.audit)


class PromotionMonotonicCoreTests(unittest.TestCase):
    def test_valid_green_generations_one_three_seven(self):
        for generation in (1, 3, 7):
            with self.subTest(generation=generation):
                case = PromotionWindow(generation)
                case.all_devices()
                event = case.event(endpoint="playback-events", status=201)
                data, accepted, deadline = case.run()
                self.assertTrue(accepted)
                self.assertEqual(deadline, case.case.deadline)
                self.assertEqual(len(data["heartbeats"]), 17)
                self.assertEqual(data["playback_evidence"], "PASS")
                self.assertEqual(case.row(event)["FINAL_ADJUDICATION"], "VERIFIED_PLAYBACK")
                self.assertEqual(len(case.audit), 18)
                self.assertTrue(all((row["ROUTE_GENERATION"], row["SLOT"], row["UPSTREAM"])
                                    == (generation, "GREEN", "backend-green:8000") for row in case.audit))

    def test_verified_future_blue_and_nonstandard_bound_upstream(self):
        case = PromotionWindow(11, slot="BLUE", previous_upstream="backend-green:8000")
        case.context["upstream"] = "validated-blue-backend:8200"
        case.all_devices()
        data, accepted, _ = case.run()
        self.assertTrue(accepted)
        self.assertEqual(len(data["heartbeats"]), 17)

    def test_late_blue_is_audited_and_cannot_supply_missing_green(self):
        case = PromotionWindow()
        case.all_devices(case.devices[:-1])
        late = case.event(case.devices[-1], generation=2, upstream=UPSTREAM, edge_only=True)
        data, accepted, _ = case.run()
        self.assertFalse(accepted)
        self.assertEqual(len(data["heartbeats"]), 16)
        row = case.row(late)
        self.assertEqual(row["FINAL_ADJUDICATION"], "WRONG_SLOT_FOR_OBSERVATION")
        self.assertEqual(row["SLOT"], "BLUE")
        self.assertIsNone(row["PLAYER_PSEUDONYM"])
        self.assertEqual(len(case.case.queries), 16)

    def test_stale_green_is_not_credited(self):
        case = PromotionWindow()
        case.all_devices(case.devices[:-1])
        stale = case.event(case.devices[-1], generation=2)
        data, accepted, _ = case.run()
        self.assertFalse(accepted)
        self.assertEqual(len(data["heartbeats"]), 16)
        self.assertEqual(case.row(stale)["FINAL_ADJUDICATION"], "STALE_ROUTE_GENERATION")

    def test_unexpected_future_route_fails_even_with_healthy_cohort(self):
        for edge_only, after_window in ((False, False), (True, False), (False, True)):
            with self.subTest(edge_only=edge_only, after_window=after_window):
                case = PromotionWindow()
                case.all_devices()
                future = case.event(case.devices[-1], generation=4, edge_only=edge_only,
                                    started=case.case.finished if after_window else None)
                with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_FUTURE_GENERATION"):
                    case.run()
                self.assertEqual(case.row(future)["FINAL_ADJUDICATION"], "FAIL_FUTURE_ROUTE_GENERATION")

    def test_malformed_generation_is_not_normalized(self):
        case = PromotionWindow()
        event = case.event(case.devices[-1], generation="03")
        with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_ROUTE_GENERATION_INVALID"):
            case.run()
        self.assertIsNone(case.row(event)["ROUTE_GENERATION"])

    def test_current_generation_wrong_upstream_fails(self):
        for upstream, disposition in (("wrong-backend:8000", "FAIL_WRONG_UPSTREAM"),
                                      (UPSTREAM, "WRONG_SLOT_FOR_OBSERVATION")):
            with self.subTest(upstream=upstream):
                case = PromotionWindow()
                wrong = case.event(case.devices[-1], upstream=upstream)
                with self.assertRaisesRegex(Stop, "EDGE_WIRE_CORRELATION_INCOMPLETE"):
                    case.run()
                self.assertEqual(case.row(wrong)["FINAL_ADJUDICATION"], disposition)
                self.assertFalse(case.case.queries)

    def test_missing_monotonic_capture_for_current_generation_fails(self):
        case = PromotionWindow()
        missing = case.event(case.devices[-1], edge_only=True)
        with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_EDGE_WITHOUT_WIRE"):
            case.run()
        self.assertEqual(case.row(missing)["FINAL_ADJUDICATION"], "FAIL_MISSING_MONOTONIC_EVIDENCE")

    def test_wrong_time_namespace_fails(self):
        case = PromotionWindow()
        case.all_devices()
        with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_CLOCK_INVALID"):
            case.run(time_namespace="time:[456]")

    def test_pre_window_and_finish_boundary_cannot_add_credit(self):
        case = PromotionWindow()
        window = case.case
        case.all_devices(case.devices[:-1])
        before = case.event(case.devices[-1], started=window.started - 1)
        after = case.event(case.devices[-1], started=window.finished)
        data, accepted, _ = case.run()
        self.assertFalse(accepted)
        self.assertEqual(len(data["heartbeats"]), 16)
        for event in (before, after):
            self.assertEqual(case.row(event)["FINAL_ADJUDICATION"], "NO_CREDIT")
            self.assertFalse(case.row(event)["REQUEST_ELIGIBLE"])

    def test_eligible_request_can_drain_across_finish(self):
        case = PromotionWindow()
        window = case.case
        case.all_devices(case.devices[:-1])
        crossing = case.event(case.devices[-1], started=window.finished - 1,
                              complete=window.finished + 5 * MS, ack_utc=window.utc(window.finished + 3 * MS))
        data, accepted, _ = case.run()
        self.assertTrue(accepted)
        row = case.row(crossing)
        self.assertTrue(row["REQUEST_ELIGIBLE"])
        self.assertEqual(row["FINAL_ADJUDICATION"], "CREDITED")
        self.assertEqual(row["ACK_RECEIVED_AT"], crossing["received_at"])
        self.assertEqual(len(data["heartbeats"]), 17)

    def test_crossing_terminal_deadline_still_fails(self):
        case = PromotionWindow()
        case.event(case.devices[-1], started=case.case.finished - 1, complete=case.case.deadline)
        with self.assertRaisesRegex(Stop, "PLAYER_MONOTONIC_TERMINAL_INVALID"):
            case.run()

    def test_substitute_player_does_not_fill_a_missing_baseline(self):
        case = PromotionWindow()
        case.all_devices(case.devices[:-1])
        replacement = case.event(case.case.fixture.replacement)
        _, accepted, _ = case.run()
        self.assertFalse(accepted)
        self.assertEqual(case.row(replacement)["FINAL_ADJUDICATION"], "NEW_DEVICE_NO_BASELINE_CREDIT")

    def test_status_401_is_no_credit_and_relevant_409_and_5xx_fail(self):
        case = PromotionWindow()
        case.all_devices(case.devices[:-1])
        rejected = case.event(status=401)
        _, accepted, _ = case.run()
        self.assertFalse(accepted)
        self.assertEqual(case.row(rejected)["FINAL_ADJUDICATION"], "NO_CREDIT")
        for status, field in ((409, "http_409"), (500, "http_5xx"), (502, "http_5xx")):
            with self.subTest(status=status):
                case = PromotionWindow()
                case.all_devices()
                case.event(status=status)
                evidence, accepted, _ = case.run()
                self.assertEqual(evidence[field], 1)
                self.assertFalse(accepted)
