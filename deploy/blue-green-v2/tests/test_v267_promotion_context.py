"""Verify durable routing context and the monotonic-only promotion gate."""

from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gates import PlayerGate, sign_receipt, utc
from player_observer_monotonic import MonotonicWindow
from promotion_observer_context import FrozenPromotion, committed_routing_context
import promotion_player_observer as observer
from release_state import StateStore, Stop, canonical, digest, now, write_private_atomic
from routectl import Controller
import test_routectl as route_fixture


KEY = bytes(range(48))


class OpenWire(route_fixture.Wire):
    def status_identity(self):
        return ("stable-edge",), ("open-admission",)


class Probe:
    def __init__(self, identities):
        self.identities = identities
        self.bindings = {"api:GREEN": {"name": "backend-green"}}

    def __call__(self, role, slot):
        return self.identities[role, slot]


class PromotionContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.edge = OpenWire()
        self.store = StateStore(self.root / "state")
        self.blue, blue = route_fixture.fixtures("BLUE")
        self.green, green = route_fixture.fixtures("GREEN")
        self.probe = Probe(blue | green)
        self.bindings = self.root / "bindings.json"
        write_private_atomic(self.bindings, canonical(self.probe.bindings), replace=False)
        self.gate = PlayerGate(KEY, require_monotonic=True)
        self.controller = Controller(self.store, self.edge, self.probe,
                                     route_fixture.RouteControllerTests.ready_gate, self.gate)
        self.controller.initialize(self.blue, self.ready("BLUE"), baseline_hash=digest(self.edge.live))
        self.controller.prepare("GREEN", self.green)
        self.controller.start("GREEN")
        for role, proof in self.ready("GREEN").items():
            self.controller.register_ready(role, "GREEN", proof)

    def ready(self, slot):
        return {role: {"identity_hash": digest(self.probe(role, slot)), "role": role} for role in ("api", "frontend")}

    def baseline(self):
        state = self.store.load()
        baseline = sign_receipt("BASELINE", {"slot": "GREEN", "prior_slot": "BLUE",
            "previous_generation": state["routing"]["generation"],
            "identity_hash": state["slots"]["GREEN"]["ready"]["api"]["identity_hash"], "release": self.green,
            "baseline_at": now(), "baseline_refs": ["1" * 64]}, KEY)
        clock = sign_receipt("PROMOTION_BASELINE_CLOCK", {"baseline_sha256": digest(baseline),
            "baseline_monotonic_ns": 500, "time_namespace": "time:[101]", "collector_id": "2" * 64,
            "collector_pid": 200, "collector_started_at": now(), "edge_id": "3" * 64, "postgres_id": "4" * 64}, KEY)
        return baseline, clock

    def split(self):
        baseline, clock = self.baseline()
        state = self.controller.switch_api("GREEN", baseline, clock_receipt=clock)
        return baseline, state

    def freeze(self, state):
        return FrozenPromotion.capture(self.controller, self.bindings, digest(state))

    @staticmethod
    def receipt(baseline, state, context, *, with_monotonic=True):
        transition = state["routing"]["api_transition"]
        started = utc(transition["switched_at"]) + timedelta(seconds=1)
        finished = started + timedelta(seconds=120)
        ack = finished + timedelta(seconds=2) if with_monotonic else started
        hmac_ref = baseline["payload"]["baseline_refs"][0]
        data = {**transition, "baseline_at": baseline["payload"]["baseline_at"], "baseline_refs": [hmac_ref],
                "started_at": started.isoformat(), "finished_at": finished.isoformat(),
                "heartbeats": [{"device_ref": hmac_ref, "trace_ref": "5" * 64, "received_at": ack.isoformat()}],
                "source": "EDGE_ACK_EXACT_DB_JOIN", "access_complete": True, "http_409": 0, "http_5xx": 0,
                "incompatible": 0, "playback_201": 0, "playback_evidence": "UNVERIFIED"}
        if with_monotonic:
            start = 10_000_000_000
            data["observation"] = {"OBSERVER_MODE": "PROMOTION_MONOTONIC", "EXPECTED_GENERATION": context["generation"],
                "EXPECTED_SLOT": context["slot"], "EXPECTED_UPSTREAM": context["upstream"],
                "EXPECTED_BACKEND_CONTAINER": context["backend_container"], "ROUTING_CONTEXT_SHA256": digest(context),
                "WINDOW_OPENED_MONOTONIC": start - 1, "WINDOW_START_MONOTONIC": start,
                "WINDOW_FINISH_MONOTONIC": start + 120_000_000_000, "TERMINAL_DEADLINE_MONOTONIC": start + 130_000_000_000,
                "TIME_NAMESPACE_MATCH": "YES", "TIME_NAMESPACE": "time:[101]", "PLAYER_OBSERVED_COUNT": 1,
                "PLAYER_MISSING_COUNT": 0, "REQUEST_EVIDENCE_SHA256": "9" * 64}
        return sign_receipt("PLAYER", data, KEY)

    def test_bindings_drift_is_rejected_before_window(self):
        _, state = self.split()
        write_private_atomic(self.bindings, canonical({"api:GREEN": {"name": "different-backend"}}))
        with self.assertRaisesRegex(Stop, "OBSERVER_COMMITTED_READY_BINDING_MISMATCH"):
            self.freeze(state)

    def test_effective_config_drift_is_rejected_before_window(self):
        _, state = self.split()
        self.edge.live = route_fixture.edge_config()
        with self.assertRaisesRegex(Stop, "OBSERVER_ROUTING_STATUS_NOT_PASS"):
            self.freeze(state)

    def test_observer_freezes_generation_and_state_epoch_during_window(self):
        _, state = self.split()
        frozen = self.freeze(state)
        self.assertEqual(frozen.context["generation"], 1)
        frozen.recheck()
        self.controller.rollback()
        with self.assertRaisesRegex(Stop, "OBSERVER_ROUTING_CONTEXT_CHANGED"):
            frozen.check_state()

    def test_replacement_with_identical_state_bytes_is_still_drift(self):
        _, state = self.split()
        frozen = self.freeze(state)
        self.store.save(state)
        with self.assertRaisesRegex(Stop, "OBSERVER_ROUTING_CONTEXT_CHANGED"):
            frozen.check_state()

    def test_baseline_requires_authentic_collector_clock_before_api_mutation(self):
        baseline, clock = self.baseline()
        with self.assertRaises(Stop):
            self.controller.switch_api("GREEN", baseline)
        clock["payload"]["time_namespace"] = "time:[102]"
        with self.assertRaises(Stop):
            self.controller.switch_api("GREEN", baseline, clock_receipt=clock)
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_only_exact_context_accepts_monotonic_drained_ack_and_switches_frontend(self):
        baseline, state = self.split()
        context = self.freeze(state).context
        receipt = self.receipt(baseline, state, context)
        transition = state["routing"]["api_transition"]
        self.assertFalse(self.gate(receipt, transition, routing_context={**context, "generation": 2}))
        self.assertFalse(self.gate(receipt, transition, routing_context={**context, "backend_container": "3" * 64}))
        self.assertTrue(self.gate(receipt, transition, routing_context=context))
        result = self.controller.switch_frontend("GREEN", receipt)
        self.assertEqual(result["routing"]["active"], {"api": "GREEN", "frontend": "GREEN"})

    def test_old_utc_receipt_cannot_switch_frontend_and_triggers_rollback(self):
        baseline, state = self.split()
        receipt = self.receipt(baseline, state, self.freeze(state).context, with_monotonic=False)
        legacy_gate = PlayerGate(KEY, now=lambda: utc(receipt["payload"]["finished_at"]) + timedelta(seconds=1))
        self.assertTrue(legacy_gate(receipt, state["routing"]["api_transition"]))
        with self.assertRaisesRegex(Stop, "PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK"):
            self.controller.switch_frontend("GREEN", receipt)
        final = self.controller.status()
        self.assertEqual(final["routing"]["active"], {"api": "BLUE", "frontend": "BLUE"})
        self.assertEqual(final["routing"]["generation"], 2)
        self.assertIsNone(final["routing"]["api_transition"])
        with self.assertRaises(Stop):
            committed_routing_context(final, self.edge.effective()[0])

    def test_prior_green_receipt_is_rejected_after_new_committed_transition(self):
        baseline, state = self.split()
        old_receipt = self.receipt(baseline, state, self.freeze(state).context)
        self.controller.rollback()
        self.controller.prepare("GREEN", self.green)
        self.controller.start("GREEN")
        for role, proof in self.ready("GREEN").items():
            self.controller.register_ready(role, "GREEN", proof)
        _, next_state = self.split()
        self.assertEqual(next_state["routing"]["generation"], 3)
        self.assertFalse(self.gate(old_receipt, next_state["routing"]["api_transition"],
                                   routing_context=self.freeze(next_state).context))

    def test_window_passes_iso_timestamp_to_the_existing_docker_log_reader(self):
        args = SimpleNamespace(capture_ready_json=None, edge_id="3" * 64, receipt="test.json",
                               opened_at="2026-11-16T20:00:00+00:00")
        upstream = [b'{"type":"ready","started_ns":1794859199000000000}', b'{"type":"monotonic_clock"}',
                    b'{"type":"done"}']
        window = MonotonicWindow(1, 2, 120_000_000_002)
        routing = SimpleNamespace(context={"slot": "GREEN"}, check_state=Mock())
        diagnostic = SimpleNamespace(advance=Mock(), counts={})

        def logs(edge_id, started, path, *, diagnostics):
            self.assertEqual(edge_id, args.edge_id)
            self.assertEqual(utc(started).isoformat(), "2026-11-16T19:59:59+00:00")
            self.assertEqual(path, args.receipt)
            self.assertIs(diagnostics, diagnostic)
            return ()

        with patch.object(observer, "capture_input", return_value=upstream), patch.object(observer, "edge_logs", logs), \
                patch.object(observer, "monotonic_ns", return_value=window.finish_ns), \
                patch.object(observer, "observe_committed_slot", return_value=({}, True, window.finish_ns)) as core:
            observed = observer._read_window(args, diagnostic, routing, window, "time:[101]", Mock(), set(), KEY, [])
        self.assertEqual(observed, ({}, True, window.finish_ns))
        self.assertEqual(core.call_args.args[0], upstream)
