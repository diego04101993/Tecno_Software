"""Checks for promotion launch using the existing time-only namespace mechanism."""

from copy import deepcopy
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

import player_observer
import player_observer_launcher as launcher
from release_state import Stop, canonical, decode, digest, write_private_atomic


ID = "a" * 64
IMAGE = "sha256:" + "b" * 64
HOST = {"monotonic": [11, 48], "boottime": [4, 0]}
COLLECTOR = {"monotonic": [-2, 14], "boottime": [9, 0]}
NS = "time:[5678]"
STAMP = "2026-11-16T08:06:07.215384+00:00"


def committed(generation=3, slot="GREEN"):
    predecessor = "BLUE" if slot == "GREEN" else "GREEN"
    active = {"api": slot, "frontend": predecessor}
    config_hash = "4" * 64
    transition = {"slot": slot, "prior_slot": predecessor, "generation": generation, "switched_at": STAMP}
    return {
        "routing": {"active": active, "generation": generation, "api_transition": transition, "config_hash": config_hash},
        "pending": None,
        "history": [{"intent": {"operation": "API", "to_slot": active, "generation": generation, "next_hash": config_hash},
                     "result": "COMMITTED", "recorded_at": STAMP}],
    }


class SwitchMarkerTests(unittest.TestCase):
    def test_marker_binds_distinct_generations_and_future_blue_promotion(self):
        for generation, slot in ((1, "GREEN"), (3, "GREEN"), (7, "GREEN"), (9, "BLUE")):
            with self.subTest(generation=generation, slot=slot):
                state = committed(generation, slot)
                self.assertEqual(launcher.promotion_switch_marker(state, 27), {
                    "event": "T_API_SWITCH_COMMITTED", "monotonic_ns": 27, "utc": STAMP, "state_sha256": digest(state),
                })

    def test_marker_rejects_pending_non_api_rollback_or_inconsistent_receipts(self):
        original = committed()
        changed = []
        pending = deepcopy(original)
        pending["pending"] = {}
        changed.append(pending)
        for field, value in (("operation", "ROLLBACK"), ("generation", 4), ("next_hash", "5" * 64)):
            state = deepcopy(original)
            state["history"][-1]["intent"][field] = value
            changed.append(state)
        for field, value in (("result", "COMMITTED_FAILED"), ("recorded_at", "wrong time")):
            state = deepcopy(original)
            state["history"][-1][field] = value
            changed.append(state)
        for state in changed:
            with self.subTest(state=state), self.assertRaisesRegex(Stop, "^PLAYER_API_SWITCH_NOT_COMMITTED$"):
                launcher.promotion_switch_marker(state, 27)
        for timestamp in (0, -7, True, "27"):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(Stop, "^PLAYER_API_SWITCH_NOT_COMMITTED$"):
                launcher.promotion_switch_marker(original, timestamp)

    def test_stale_marker_rejects_new_generation_before_window(self):
        store = Mock()
        store.load.return_value = committed(4)
        marker = launcher.promotion_switch_marker(committed(3), 40)
        with self.assertRaisesRegex(Stop, "^OBSERVER_ROUTING_CONTEXT_CHANGED$"):
            launcher._promotion_opening([marker], store)

    def test_duplicate_and_modified_markers_reject(self):
        state = committed()
        marker = launcher.promotion_switch_marker(state, 25)
        store = Mock()
        store.load.return_value = state
        for markers in ([marker, marker], [{**marker, "utc": "wrong timestamp"}], [{**marker, "generation": 7}]):
            with self.subTest(markers=markers), self.assertRaisesRegex(Stop, "^PLAYER_WINDOW_TIMELINE_INVALID$"):
                launcher._promotion_opening(markers, store)

    @unittest.skipUnless(os.name == "posix", "Linux private timeline file descriptors")
    def test_joined_child_uses_committed_switch_and_translates_only_monotonic_anchor(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            timeline, receipt = work / "timeline", work / "clock"
            state = committed(7)
            store = Mock()
            store.load.return_value = state
            mark = launcher.promotion_switch_marker(state, 40 * launcher.NANOSECONDS)
            before = canonical(mark) + b"\n"
            write_private_atomic(timeline, before, replace=False)
            clock = {"TIME_NAMESPACE_MATCH": "YES", "OBSERVER_TIME_NS": NS, "OBSERVER_TIMENS_OFFSETS": COLLECTOR,
                     "OBSERVER_ORIGINAL_TIMENS_OFFSETS": HOST}
            write_private_atomic(receipt, canonical(clock), replace=False)
            shift = launcher._offset_ns(COLLECTOR) - launcher._offset_ns(HOST)
            opened = mark["monotonic_ns"] + shift
            started = opened + 55
            with (patch.object(launcher, "_proc_clock", return_value=(NS, COLLECTOR)),
                  patch.object(launcher, "monotonic_ns", return_value=started),
                  patch.object(player_observer, "main", return_value=2) as observe):
                self.assertEqual(launcher._window_observer(timeline, receipt, ["promotion-observe"], store=store), 2)
                with self.assertRaisesRegex(Stop, "^PLAYER_WINDOW_TIMELINE_INVALID$"):
                    launcher._window_observer(timeline, receipt, ["promotion-observe"], store=store)
            argv = observe.call_args.args[0]
            arguments = dict(zip(argv[1::2], argv[2::2]))
            self.assertEqual(arguments["--opened-at"], STAMP)
            self.assertEqual(arguments["--committed-state-sha256"], digest(state))
            self.assertEqual(arguments["--clock-receipt"], str(receipt))
            self.assertEqual(int(arguments["--opened-monotonic-ns"]), opened)
            self.assertEqual(int(arguments["--started-monotonic-ns"]), started)
            self.assertEqual(int(arguments["--finished-monotonic-ns"]), started + 120 * launcher.NANOSECONDS)
            self.assertTrue(timeline.read_bytes().startswith(before))
            observed = decode(timeline.read_bytes().splitlines()[-1])
            self.assertEqual(observed["state_sha256"], digest(state))
            self.assertEqual(observed["monotonic_ns"], started - shift)
            observe.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "Linux private timeline file descriptors")
    def test_joined_child_rejects_different_time_namespace(self):
        with TemporaryDirectory() as directory:
            receipt = Path(directory) / "clock"
            row = {"TIME_NAMESPACE_MATCH": "YES", "OBSERVER_TIME_NS": NS, "OBSERVER_TIMENS_OFFSETS": COLLECTOR,
                   "OBSERVER_ORIGINAL_TIMENS_OFFSETS": HOST}
            write_private_atomic(receipt, canonical(row), replace=False)
            with (patch.object(launcher, "_proc_clock", return_value=("time:[5700]", COLLECTOR)),
                  patch.object(player_observer, "main") as observe,
                  self.assertRaisesRegex(Stop, "^PLAYER_MONOTONIC_CLOCK_INVALID$")):
                launcher._window_observer(receipt.with_name("unopened-timeline"), receipt, ["promotion-observe"],
                                          store=Mock())
            observe.assert_not_called()


class PromotionLauncherCLITests(unittest.TestCase):
    controls = ["--collector-id", ID, "--collector-image", IMAGE, "--collector-restart-count", "0",
                "--clock-receipt", "/unused/clock.json", "--preflight-receipt", "/unused/probe.json"]
    shared = ["--state-dir", "/unused/state", "--postgres-id", ID, "--key", "/unused/key", "--receipt", "/unused/player"]
    routing = ["--socket", "/unused/socket", "--autosave", "/unused/autosave",
               "--bindings", "/unused/bindings.json", "--edge-name", "private-edge",
               "--expected-edge-config-revision", "1"]
    observe = ["--edge-id", ID, "--baseline", "/unused/baseline"]

    def test_cli_baseline_enters_verified_clock_with_injected_receipt(self):
        args = [*self.controls, "promotion-baseline", "--", *self.shared, "promotion-baseline", *self.routing]
        with (patch.object(launcher, "read_private", return_value=canonical({"TIME_NAMESPACE_MATCH": "YES"})),
              patch.object(launcher, "launch_observer_in_collector_time_namespace", return_value=0) as launch):
            self.assertEqual(launcher.main(args), 0)
        argv = launch.call_args.args[3]
        self.assertIn("promotion-baseline", argv)
        self.assertEqual(argv[argv.index("--clock-receipt") + 1], str(Path("/unused/clock.json")))

    def test_cli_refuses_promotion_without_expected_config_revision(self):
        for action, extra, tail in (("promotion-baseline", [], []),
                                    ("promotion-observe", ["--timeline", "/unused/timeline"], self.observe)):
            args = [*self.controls, action, *extra, "--", *self.shared, action, *self.routing[:-2], *tail]
            with (self.subTest(action=action), patch.object(launcher, "launch_observer_in_collector_time_namespace") as launch,
                  self.assertRaisesRegex(Stop, "^CLOCK_OBSERVER_COMMAND_INVALID$")):
                launcher.main(args)
            launch.assert_not_called()

    def test_cli_observe_uses_joined_child_and_refuses_override(self):
        args = [*self.controls, "promotion-observe", "--timeline", "/unused/timeline", "--", *self.shared,
                "promotion-observe", *self.routing, *self.observe]
        with (patch.object(launcher, "read_private", return_value=canonical({"TIME_NAMESPACE_MATCH": "YES"})),
              patch.object(launcher, "launch_observer_in_collector_time_namespace", return_value=0) as launch):
            self.assertEqual(launcher.main(args), 0)
            argv = launch.call_args.args[3]
            start = argv.index("_observe_promotion_window")
            self.assertEqual(argv[start + 1:start + 4], [str(Path("/unused/timeline")), str(Path("/unused/clock.json")),
                                                         "/unused/state"])
            self.assertNotIn("--started-at", argv)
            for flag in (*launcher.PROMOTION_WINDOW_FLAGS, "--clock-receipt", "--started-mo"):
                with self.subTest(flag=flag), self.assertRaisesRegex(Stop, "^PLAYER_WINDOW_ARGUMENT_OVERRIDE$"):
                    launcher.main([*args, flag + "=0"])
            launch.assert_called_once()

    def test_cannot_route_utc_observer_through_monotonic_action(self):
        args = [*self.controls, "promotion-observe", "--timeline", "/unused/timeline", "--",
                *self.shared, "observe", *self.observe]
        with (patch.object(launcher, "launch_observer_in_collector_time_namespace") as launch,
              self.assertRaisesRegex(Stop, "^CLOCK_OBSERVER_COMMAND_INVALID$")):
            launcher.main(args)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
