"""Fail-closed controls for the Phase 3 time-only observer launcher."""

from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
import player_observer
import player_observer_launcher as launcher
from release_state import Stop, canonical, decode, write_private_atomic


ID = "a" * 64
IMAGE = "sha256:" + "b" * 64
HOST = "time:[1234]"
COLLECTOR = "time:[5678]"
ZERO = {"monotonic": [0, 0], "boottime": [0, 0]}
NONZERO = {"monotonic": [7, 15], "boottime": [3, 0]}
EPOCH = {"COLLECTOR_ID": ID, "COLLECTOR_IMAGE": IMAGE, "COLLECTOR_HOST_PID": 410,
         "COLLECTOR_STARTED_AT": "2026-11-15T01:02:03.456Z", "COLLECTOR_RESTART_COUNT": 0,
         "COLLECTOR_PID_START_TICKS": 140, "COLLECTOR_TIME_NS": COLLECTOR, "COLLECTOR_TIMENS_OFFSETS": ZERO}


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(TemporaryDirectory()))
        self.receipt = self.work / "clock.json"
        self.addCleanup(self.check_stop_cleanup)
        self.epoch = dict(EPOCH)
        self.child = {"pid": 481, "namespace": COLLECTOR, "offsets": ZERO}
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(launcher.sys, "platform", "linux"))
        self.inspect = stack.enter_context(patch.object(launcher, "_inspect_collector", return_value=self.epoch))
        self.clock = stack.enter_context(patch.object(launcher, "_proc_clock",
                                                       side_effect=lambda proc: (HOST, ZERO) if proc.name == "self"
                                                       else (self.child["namespace"], self.child["offsets"])))
        self.others = stack.enter_context(patch.object(launcher, "_other_namespaces", return_value={"net": "net:[25]"}))
        self.version = stack.enter_context(patch.object(launcher, "_nsenter_version",
                                                        return_value="nsenter from util-linux 2.41.3"))
        self.report = stack.enter_context(patch.object(launcher, "_child_clock", return_value=self.child))
        self.popen = stack.enter_context(patch.object(launcher.subprocess, "Popen"))
        self.popen.return_value.pid = 480
        self.popen.return_value.wait.return_value = 0
        stack.enter_context(patch.object(os, "getpgid", return_value=480, create=True))
        stack.enter_context(patch.object(launcher.signal, "SIGKILL", 9, create=True))
        self.kill = stack.enter_context(patch.object(os, "killpg", create=True))
        self.release = stack.enter_context(patch.object(os, "write"))

    def check_stop_cleanup(self):
        if self.popen.called and not self.release.called:
            self.kill.assert_called_once()
            self.popen.return_value.wait.assert_called_once()

    def launch(self, expected=None):
        return launcher.launch_observer_in_collector_time_namespace(ID, IMAGE, 0, [sys.executable, "-V"],
                                                                    self.receipt, expected=expected)

    def assert_block(self, reason):
        with self.assertRaisesRegex(Stop, "^PLAYER_MONOTONIC_CLOCK_INVALID$"):
            self.launch()
        evidence = decode(self.receipt.read_bytes())
        self.assertEqual((evidence["TIME_NAMESPACE_MATCH"], evidence["REASON"]), ("NO", reason))
        self.release.assert_not_called()
        return evidence

    def test_unjoined_observer_with_equal_offsets_blocks(self):
        self.child["namespace"] = HOST
        self.assert_block("OBSERVER_COLLECTOR_CLOCK_MISMATCH")

    def test_joined_observer_passes_and_releases_after_reinspect(self):
        def allow(_fd, token):
            self.assertEqual(token, b"1")
            self.assertEqual(self.inspect.call_count, 3)
            self.assertEqual(decode(self.receipt.read_bytes())["TIME_NAMESPACE_MATCH"], "YES")
        self.release.side_effect = allow
        self.assertEqual(self.launch(), 0)
        row = decode(self.receipt.read_bytes())
        self.assertEqual((row["COLLECTOR_TIME_NS"], row["OBSERVER_TIME_NS"]), (COLLECTOR, COLLECTOR))
        self.assertEqual((row["COLLECTOR_TIMENS_OFFSETS"], row["OBSERVER_TIMENS_OFFSETS"]), (ZERO, ZERO))
        self.assertEqual((row["COLLECTOR_HOST_PID"], row["OBSERVER_PID"]), (410, 481))
        self.assertEqual(self.popen.call_args.args[0][:5],
                         [str(launcher.NSENTER), "--target", "410", "--time", "--"])

    def test_same_namespace_still_uses_nsenter(self):
        self.epoch["COLLECTOR_TIME_NS"] = self.child["namespace"] = HOST
        self.assertEqual(self.launch(), 0)
        self.assertIn("--time", self.popen.call_args.args[0])

    def test_collector_pid_changes_before_launch(self):
        self.inspect.side_effect = [self.epoch, {**self.epoch, "COLLECTOR_HOST_PID": 411}]
        self.assert_block("COLLECTOR_CHANGED_BEFORE_LAUNCH")
        self.popen.assert_not_called()

    def test_collector_pid_changes_after_launch(self):
        self.inspect.side_effect = [self.epoch, self.epoch, {**self.epoch, "COLLECTOR_HOST_PID": 411}]
        self.assert_block("COLLECTOR_CHANGED_AFTER_LAUNCH")

    def test_collector_started_at_changes_after_launch(self):
        changed = {**self.epoch, "COLLECTOR_STARTED_AT": "2026-11-15T01:02:08Z"}
        self.inspect.side_effect = [self.epoch, self.epoch, changed]
        self.assert_block("COLLECTOR_CHANGED_AFTER_LAUNCH")

    def test_collector_namespace_changes_after_launch(self):
        self.inspect.side_effect = [self.epoch, self.epoch, {**self.epoch, "COLLECTOR_TIME_NS": "time:[1235]"}]
        self.assert_block("COLLECTOR_CHANGED_AFTER_LAUNCH")

    def test_nsenter_permission_denied(self):
        self.popen.side_effect = PermissionError(13, "Permission denied")
        with self.assertRaisesRegex(Stop, "^PLAYER_MONOTONIC_CLOCK_INVALID$"):
            self.launch()
        self.release.assert_not_called()
        self.assertEqual(decode(self.receipt.read_bytes())["REASON"], "PermissionError")
        self.popen.reset_mock()

    def test_nsenter_setns_failure_leaves_no_clock_report(self):
        self.report.side_effect = Stop("NSENTER_SETNS_OR_HANDSHAKE_FAILED")
        self.assert_block("NSENTER_SETNS_OR_HANDSHAKE_FAILED")

    def test_nsenter_unavailable(self):
        self.version.side_effect = Stop("NSENTER_UNAVAILABLE")
        self.assert_block("NSENTER_UNAVAILABLE")
        self.popen.assert_not_called()

    def test_timens_offsets_read_failure(self):
        self.clock.side_effect = Stop("TIMENS_OFFSETS_OR_NAMESPACE_UNREADABLE")
        self.assert_block("TIMENS_OFFSETS_OR_NAMESPACE_UNREADABLE")
        self.popen.assert_not_called()

    def test_equal_namespace_but_different_offsets_blocks(self):
        self.child["offsets"] = NONZERO
        row = self.assert_block("OBSERVER_COLLECTOR_CLOCK_MISMATCH")
        self.assertEqual(row["COLLECTOR_TIME_NS"], row["OBSERVER_TIME_NS"])
        self.assertNotEqual(row["COLLECTOR_TIMENS_OFFSETS"], row["OBSERVER_TIMENS_OFFSETS"])

    def test_nsenter_extra_namespace_flags_block_before_spawn(self):
        command = launcher._command
        for flag in ("--net", "--mount"):
            with self.subTest(flag=flag), patch.object(launcher, "_command") as builder:
                def with_extra_flag(*args):
                    argv = command(*args)
                    return [*argv[:4], flag, *argv[4:]]
                builder.side_effect = with_extra_flag
                self.assert_block("NSENTER_SCOPE_INVALID")
                self.receipt.unlink()
        self.popen.assert_not_called()

    def test_child_other_namespace_change_blocks(self):
        self.others.side_effect = [{"net": "net:[25]"}, {"net": "net:[26]"}]
        self.assert_block("OBSERVER_PROCESS_CLOCK_OR_SCOPE_CHANGED")

    def test_restarted_collector_cannot_reuse_probe(self):
        expected = {**self.epoch, "TIME_NAMESPACE_MATCH": "YES", "OBSERVER_ORIGINAL_TIME_NS": HOST,
                    "OBSERVER_ORIGINAL_TIMENS_OFFSETS": ZERO}
        self.epoch["COLLECTOR_PID_START_TICKS"] += 1
        with self.assertRaisesRegex(Stop, "^PLAYER_MONOTONIC_CLOCK_INVALID$"):
            self.launch(expected=expected)
        self.assertEqual(decode(self.receipt.read_bytes())["REASON"], "COLLECTOR_OR_ORIGINAL_CLOCK_CHANGED_SINCE_PROBE")
        self.popen.assert_not_called()


class ClockDataTest(unittest.TestCase):
    def test_offset_parser_preserves_signed_seconds(self):
        self.assertEqual(launcher._timens_offsets("monotonic -1 999999999\nboottime 0 2\n"),
                         {"monotonic": [-1, 999999999], "boottime": [0, 2]})
        self.assertEqual(launcher._offset_ns({"monotonic": [-1, 999999999]}), -1)
        for raw in ("monotonic 0 0\n", "monotonic 0 1000000000\nboottime 0 0\n",
                    "monotonic 0 -2\nboottime 0 0\n", "monotonic 0 0\nmonotonic 0 0\nboottime 0 0\n"):
            with self.subTest(raw=raw), self.assertRaisesRegex(Stop, "^TIMENS_OFFSETS_INVALID$"):
                launcher._timens_offsets(raw)

    @unittest.skipUnless(os.name == "posix", "Linux private timeline file descriptors")
    def test_nonzero_offset_preserves_timeline_and_window_duration(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            receipt = work / "clock.json"
            timeline = work / "timeline.jsonl"
            begin = 20 * launcher.NANOSECONDS
            delta = launcher._offset_ns(NONZERO)
            clock = {"TIME_NAMESPACE_MATCH": "YES", "OBSERVER_TIME_NS": COLLECTOR, "OBSERVER_TIMENS_OFFSETS": NONZERO,
                     "OBSERVER_ORIGINAL_TIMENS_OFFSETS": ZERO}
            rows = [{"event": "T_INGRESS_READY", "monotonic_ns": begin},
                    {"event": "T_FIRST_HTTP_200", "monotonic_ns": begin + 30}]
            original = b"".join(canonical(row) + b"\n" for row in rows)
            write_private_atomic(receipt, canonical(clock))
            write_private_atomic(timeline, original)
            joined_start = begin + delta + 400
            with (patch.object(launcher, "_proc_clock", return_value=(COLLECTOR, NONZERO)),
                  patch.object(launcher, "monotonic_ns", return_value=joined_start),
                  patch.object(player_observer, "main", return_value=2) as observe):
                self.assertEqual(launcher._window_observer(timeline, receipt, ["phase3-observe"]), 2)
            argv = observe.call_args.args[0]
            self.assertEqual(int(argv[argv.index("--opened-monotonic-ns") + 1]), begin + delta)
            self.assertEqual(int(argv[argv.index("--started-monotonic-ns") + 1]), joined_start)
            self.assertEqual(int(argv[argv.index("--finished-monotonic-ns") + 1]),
                             joined_start + 120 * launcher.NANOSECONDS)
            self.assertTrue(timeline.read_bytes().startswith(original))
            marker = decode(timeline.read_bytes().splitlines()[-1])
            self.assertEqual((marker["monotonic_ns"], marker["player_monotonic_ns"]),
                             (joined_start - delta, joined_start))

    def test_cli_rejects_generic_baseline_even_if_receipt_contains_phase3_action(self):
        args = ["--collector-id", ID, "--collector-image", IMAGE, "--collector-restart-count", "0",
                "--clock-receipt", "/unused/start.json", "--preflight-receipt", "/unused/probe.json"]
        with (patch.object(launcher, "launch_observer_in_collector_time_namespace") as launch,
              self.assertRaisesRegex(Stop, "^CLOCK_OBSERVER_COMMAND_INVALID$")):
            launcher.main([*args, "baseline", "--", "--state-dir", "/unused/state", "--postgres-id", ID,
                           "--key", "/unused/key", "--receipt", "phase3-baseline", "baseline"])
        launch.assert_not_called()

    def test_cli_observe_sets_only_launcher_owned_window_markers(self):
        args = ["--collector-id", ID, "--collector-image", IMAGE, "--collector-restart-count", "0",
                "--clock-receipt", "/unused/start.json", "--preflight-receipt", "/unused/probe.json",
                "observe", "--timeline", "/unused/timeline.jsonl", "--",
                "--state-dir", "/unused/state", "--postgres-id", ID, "--key", "/unused/key",
                "--receipt", "/unused/observe.json", "phase3-observe", "--edge-id", ID,
                "--baseline", "/unused/baseline.json", "--opened-at", "2026-11-15T00:00:00Z",
                "--socket", "/unused/socket", "--autosave", "/unused/autosave.json"]
        with (patch.object(launcher, "read_private", return_value=canonical({"TIME_NAMESPACE_MATCH": "YES"})),
              patch.object(launcher, "launch_observer_in_collector_time_namespace", return_value=2) as launch):
            self.assertEqual(launcher.main(args), 2)
            self.assertIn("_observe_window", launch.call_args.args[3])
            self.assertNotIn("--started-at", launch.call_args.args[3])
            with self.assertRaisesRegex(Stop, "^PLAYER_WINDOW_ARGUMENT_OVERRIDE$"):
                launcher.main([*args, "--started-at", "2026-11-15T00:00:01Z"])
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
