from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import player_observer as observer
import player_observer_diagnostics as diagnostics
from release_state import Stop, write_private_atomic


PG, BACKEND, EDGE = ("a" * 64, "b" * 64, "c" * 64)
SECRET = "eyJhbGciOiJIUzI1NiJ9.fixtureOnly.signature PASSWORD=never-log-2Z"
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ObserverFailureReceiptTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(TemporaryDirectory())).resolve()
        self.destination = self.root / "player.json"
        self.key = self.root / "key"
        self.baseline = self.root / "baseline.json"
        write_private_atomic(self.key, b"secret fixture key bytes\n", replace=False)
        write_private_atomic(self.baseline, b'{"signed_fixture":true}', replace=False)
        self.baseline_bytes = self.baseline.read_bytes()
        self.argv = [
            "--state-dir", str(self.root), "--postgres-id", PG, "--key", str(self.key),
            "--receipt", str(self.destination), "phase3-observe",
            "--edge-id", EDGE, "--baseline", str(self.baseline), "--socket", str(self.root / "edge.sock"),
            "--autosave", str(self.root / "autosave.json"), "--opened-at", ORIGIN.isoformat(),
            "--started-at", ORIGIN.isoformat(), "--finished-at", (ORIGIN + timedelta(seconds=120)).isoformat(),
        ]
        expected = {"container_id": BACKEND, "image_id": "image-backend", "restart_count": 0,
                    "pid": 103, "started_at": ORIGIN.isoformat()}
        state = {"slots": {"BLUE": {"ready": {"api": {"identity": expected}}}}}
        state_reader = stack.enter_context(patch.object(observer, "StateStore"))
        state_reader.return_value.load.return_value = state
        self.state_reader = state_reader
        stack.enter_context(patch.object(observer, "phase3_context", return_value={}))
        stack.enter_context(patch.object(observer, "CaddyEdge"))
        self.ingress = stack.enter_context(patch.object(observer, "verify_phase3_ingress",
            return_value=("boot", 4, 5, int(ORIGIN.timestamp()) * 1_000_000_000)))
        rows = []
        for identity in (PG, BACKEND, EDGE):
            rows.append({"Id": identity, "Image": expected["image_id"], "RestartCount": 0,
                         "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy"},
                                   "OOMKilled": False, "Paused": False, "Restarting": False, "Dead": False,
                                   "Pid": 103, "StartedAt": expected["started_at"]}})
        self.docker = stack.enter_context(patch.object(observer, "local_docker"))
        self.docker.side_effect = lambda *args, **_: (
            CompletedProcess(["docker", *args], 0, json.dumps([row for row in rows if row["Id"] in args[2:]]).encode(), b"")
            if args[:2] == ("container", "inspect") else
            CompletedProcess(["docker", *args], 0, b"", b""))
        local_sys = SimpleNamespace(platform="linux", stdin=SimpleNamespace(buffer=io.BytesIO(b"fixture\n")))
        stack.enter_context(patch.object(observer, "sys", local_sys))
        self.read_capture = stack.enter_context(patch.object(observer, "capture_input", return_value=[b"fixture\n"]))
        record = {"payload": {"baseline_refs": ["hmac1", "hmac2"], "heartbeats": [{"device_ref": "hmac1"}],
                             "http_409": 0, "http_5xx": 0, "incompatible": 0, "playback_201": 0, "result": "FAIL"}}
        self.adjudicate = stack.enter_context(patch.object(observer, "decide_phase3", return_value=(record, False)))
        self.fallback = self.root / "fallback"
        self.fallback.mkdir(mode=0o700)
        stack.enter_context(patch.object(diagnostics, "mkdtemp", return_value=str(self.fallback)))

    def run_observer(self, argv=None):
        stderr, stdout = io.StringIO(), io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(stdout):
            status = observer.main(self.argv if argv is None else argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def check_failure(self, result, *, stage, reason, code=1, fallback=False, emergency=False):
        status, stdout, stderr = result
        self.assertEqual(status, code)
        self.assertIn("PLAYER_OBSERVER_RESULT=FAIL\n", stderr)
        self.assertIn(f"PLAYER_OBSERVER_REASON={reason}\n", stderr)
        self.assertIn(f"PLAYER_OBSERVER_STAGE={stage}\n", stderr)
        self.assertIn(f"PLAYER_OBSERVER_EXIT_CODE={code}\n", stderr)
        label = next(line for line in stderr.splitlines() if line.startswith("PLAYER_OBSERVER_EVIDENCE_PATH="))
        path = label.split("=", 1)[1]
        if emergency:
            self.assertEqual(path, "stderr")
            emergency_line = next(line for line in stderr.splitlines()
                                  if line.startswith("PLAYER_OBSERVER_FAILURE_RECEIPT="))
            record = json.loads(emergency_line.split("=", 1)[1])
        else:
            path = Path(path)
            expected = self.fallback / "failure.json" if fallback else self.destination.with_name("player.json.failure.json")
            self.assertEqual(path, expected)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            record = json.loads(path.read_bytes())
        self.assertEqual((record["result"], record["stage"], record["reason"], record["exit_code"]),
                         ("FAIL", stage, reason, code))
        self.assertGreaterEqual(record["elapsed_monotonic_ns"], 0)
        self.assertEqual(self.baseline.read_bytes(), self.baseline_bytes)
        for value in SECRET.split():
            self.assertNotIn(value, stdout + stderr + json.dumps(record))
        self.assertNotIn("secret fixture key bytes", stdout + stderr + json.dumps(record))
        self.assertNotIn("hmac1", json.dumps(record))
        return record

    def test_bad_arguments_have_a_private_diagnostic_without_echoing_input(self):
        result = self.run_observer(self.argv + [SECRET])
        self.check_failure(result, stage="parse_arguments", reason="OBSERVER_ARGUMENTS_INVALID", code=2, fallback=True)

    def test_invalid_scope_is_rejected_before_loading_state(self):
        argv = self.argv.copy()
        argv[argv.index(PG)] = "invalid-id"
        result = self.run_observer(argv)
        self.check_failure(result, stage="validate_scope", reason="OBSERVER_REQUIRES_EXACT_LOCAL_IDENTITIES")
        self.state_reader.assert_not_called()

    def test_bad_state_preserves_reason_and_baseline(self):
        self.state_reader.return_value.load.side_effect = Stop("STATE_CHECKSUM_MISMATCH")
        record = self.check_failure(self.run_observer(), stage="load_state_and_key", reason="STATE_CHECKSUM_MISMATCH")
        self.assertEqual(record["last_valid_stage"], "validate_scope")
        self.assertEqual(record["traceback"][0]["file"], "player_observer.py")

    def test_engine_error_diagnostics_do_not_echo_sensitive_exception_text(self):
        self.docker.side_effect = OSError(SECRET)
        self.check_failure(self.run_observer(), stage="inspect_endpoints", reason="OBSERVER_IO_FAILED")

    def test_ingress_failure_retains_original_guard(self):
        self.ingress.side_effect = Stop("PHASE3_INGRESS_CHANGED")
        self.check_failure(self.run_observer(), stage="verify_initial_ingress", reason="PHASE3_INGRESS_CHANGED")
        self.read_capture.assert_not_called()

    def test_capture_failure_never_creates_a_gate_receipt(self):
        self.read_capture.side_effect = Stop("CAPTURE_STREAM_INVALID")
        self.check_failure(self.run_observer(), stage="read_capture", reason="CAPTURE_STREAM_INVALID")
        self.assertFalse(self.destination.exists())

    def test_logs_command_error_preserves_streams_without_mixing_them_in_the_diagnostic(self):
        inspect = self.docker.side_effect
        self.docker.side_effect = lambda *a, **k: (CompletedProcess(["docker", *a], 18, b"stdout", b"stderr")
                                                  if a[:2] == ("container", "logs") else inspect(*a, **k))
        record = self.check_failure(self.run_observer(), stage="collect_edge_logs", reason="DOCKER_LOG_COLLECTION_FAILED")
        self.assertEqual(record["edge_log_streams"]["returncode"], 18)
        self.assertEqual(record["edge_log_streams"]["stdout_bytes"], 6)
        self.assertTrue(self.root.joinpath("player.json.docker-logs.json").exists())
        self.assertEqual(record["classification"], "OBSERVER_LOG_COLLECTION_FAILURE")

    def test_timeout_from_log_command_is_distinct_from_log_write(self):
        inspect = self.docker.side_effect
        def time_out(*args, **kwargs):
            if args[:2] == ("container", "logs"):
                raise TimeoutExpired(["docker", *args], 30, output=b"partial")
            return inspect(*args, **kwargs)
        self.docker.side_effect = time_out
        record = self.check_failure(self.run_observer(), stage="collect_edge_logs", reason="DOCKER_LOG_COLLECTION_FAILED")
        self.assertIsNone(record["edge_log_streams"]["returncode"])
        self.assertEqual(record["classification"], "OBSERVER_TIMEOUT_OR_CLOCK_FAILURE")

    def test_edge_evidence_write_failure_has_its_own_stage(self):
        self.root.joinpath("player.json.docker-logs.json").write_bytes(b"previous")
        record = self.check_failure(self.run_observer(), stage="write_edge_log_evidence",
                                    reason="OBSERVER_EVIDENCE_FILE_EXISTS")
        self.assertEqual(record["classification"], "OBSERVER_RECEIPT_WRITE_FAILURE")
        self.assertEqual(self.root.joinpath("player.json.docker-logs.json").read_bytes(), b"previous")

    def test_correlation_stop_and_unexpected_exception_both_preserve_receipts(self):
        for error, reason in ((Stop("EDGE_WIRE_CORRELATION_INCOMPLETE"), "EDGE_WIRE_CORRELATION_INCOMPLETE"),
                              (RuntimeError(SECRET), "OBSERVER_INTERNAL_EXCEPTION")):
            with self.subTest(reason=reason):
                self.adjudicate.side_effect = error
                self.check_failure(self.run_observer(), stage="adjudicate", reason=reason)
                self.root.joinpath("player.json.failure.json").unlink()
                self.root.joinpath("player.json.docker-logs.json").unlink()

    def test_failed_recheck_preserves_counts_already_adjudicated(self):
        token = self.ingress.return_value
        self.ingress.side_effect = [token, ("other", *token[1:])]
        record = self.check_failure(self.run_observer(), stage="recheck_ingress", reason="PHASE3_INGRESS_CHANGED")
        self.assertEqual((record["counts"]["baseline"], record["counts"]["observed"], record["counts"]["missing"]), (2, 1, 1))
        self.assertFalse(self.destination.exists())

    def test_present_signed_gate_rejection_keeps_exit_two_and_diagnostics(self):
        record = self.check_failure(self.run_observer(), stage="report_player_result", reason="PLAYER_GATE_REJECTED", code=2)
        self.assertEqual(record["counts"]["missing"], 1)
        primary = json.loads(self.destination.read_bytes())
        self.assertEqual(primary, self.adjudicate.return_value[0])
        self.assertEqual(record["primary_receipt_durability"], "WRITTEN")

    def baseline_args(self, path):
        argv = self.argv[:self.argv.index("phase3-observe")]
        argv[argv.index(str(self.destination))] = str(path)
        return argv + ["phase3-baseline"]

    def test_missing_receipt_directory_uses_private_fallback(self):
        path = self.root / "absent" / "baseline-result.json"
        with patch.object(observer, "capture_phase3_baseline", return_value={"payload": {"baseline_refs": []}}):
            result = self.run_observer(self.baseline_args(path))
        self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_PATH_MISSING", fallback=True)

    def test_invalid_primary_object_and_serialization_failure(self):
        with patch.object(observer, "capture_phase3_baseline",
                          return_value={"payload": {"baseline_refs": [], "invalid": object()}}):
            result = self.run_observer(self.baseline_args(self.destination))
        record = self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_INTERNAL_EXCEPTION")
        self.assertEqual(record["exception_type"], "TypeError")
        self.assertEqual(record["classification"], "OBSERVER_RECEIPT_WRITE_FAILURE")

    def test_existing_primary_file_is_never_overwritten(self):
        self.destination.write_bytes(b"existing")
        with patch.object(observer, "capture_phase3_baseline", return_value={"payload": {"baseline_refs": []}}):
            result = self.run_observer(self.baseline_args(self.destination))
        self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_EVIDENCE_FILE_EXISTS")
        self.assertEqual(self.destination.read_bytes(), b"existing")

    def test_permissions_and_fallback_atomic_rename_failure(self):
        path = self.root / "absent" / "baseline-result.json"
        with patch.object(observer, "capture_phase3_baseline", return_value={"payload": {"baseline_refs": []}}), \
             patch.object(observer, "save_receipt", side_effect=PermissionError(SECRET)), \
             patch("release_state.durable_replace", side_effect=OSError(SECRET)) as rename:
            result = self.run_observer(self.baseline_args(path))
        self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_PERMISSION_DENIED", emergency=True)
        rename.assert_called_once()

    def test_primary_fsync_failure_persists_diagnostic_without_overwriting_evidence(self):
        real_fsync = os.fsync
        fail_once = Mock(side_effect=OSError(SECRET))
        def sync(fd):
            if not fail_once.called:
                fail_once(fd)
            else:
                real_fsync(fd)
        with patch.object(observer, "capture_phase3_baseline", return_value={"payload": {"baseline_refs": []}}), \
             patch.object(os, "fsync", side_effect=sync):
            result = self.run_observer(self.baseline_args(self.destination))
        record = self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_IO_FAILED")
        self.assertEqual(record["primary_receipt_durability"], "UNCONFIRMED")
        self.assertFalse(self.destination.exists())

    def test_diagnostic_storage_failure_cannot_replace_primary_cause(self):
        self.read_capture.side_effect = Stop("CAPTURE_STREAM_INVALID")
        with patch.object(diagnostics, "write_private_atomic", side_effect=RuntimeError(SECRET)):
            result = self.run_observer()
        record = self.check_failure(result, stage="read_capture", reason="CAPTURE_STREAM_INVALID", emergency=True)
        self.assertEqual(record["diagnostic_write_errors"], ["OBSERVER_INTERNAL_EXCEPTION"] * 2)

    def test_directory_fsync_after_link_leaves_receipt_unconfirmed(self):
        receipt = {"payload": {"baseline_refs": []}}
        with patch.object(observer, "capture_phase3_baseline", return_value=receipt), \
             patch.object(observer, "sync_dir", side_effect=OSError(SECRET)):
            result = self.run_observer(self.baseline_args(self.destination))
        record = self.check_failure(result, stage="write_baseline_receipt", reason="OBSERVER_IO_FAILED")
        self.assertEqual(record["primary_receipt_durability"], "UNCONFIRMED")
        self.assertEqual(json.loads(self.destination.read_bytes()), receipt)

    def test_existing_diagnostic_is_preserved_and_fallback_marks_the_collision(self):
        existing = self.destination.with_name(self.destination.name + ".failure.json")
        existing.write_bytes(b"preserved")
        self.read_capture.side_effect = Stop("CAPTURE_STREAM_INVALID")
        record = self.check_failure(self.run_observer(), stage="read_capture", reason="CAPTURE_STREAM_INVALID", fallback=True)
        self.assertEqual(existing.read_bytes(), b"preserved")
        self.assertEqual(record["diagnostic_write_errors"], ["OBSERVER_EVIDENCE_FILE_EXISTS"])
