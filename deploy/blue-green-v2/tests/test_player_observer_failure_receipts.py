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
import player_observer_monotonic as monotonic_observer
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
            "--opened-monotonic-ns", "1", "--started-monotonic-ns", "2", "--finished-monotonic-ns", "120000000002",
        ]
        expected = {"container_id": BACKEND, "image_id": "image-backend", "restart_count": 0,
                    "pid": 103, "started_at": ORIGIN.isoformat()}
        state = {"slots": {"BLUE": {"ready": {"api": {"identity": expected}}}}}
        state_reader = stack.enter_context(patch.object(observer, "StateStore"))
        state_reader.return_value.load.return_value = state
        self.state_reader = state_reader
        stack.enter_context(patch.object(observer, "phase3_context", return_value={}))
        stack.enter_context(patch.object(observer, "phase3_time_namespace", return_value="time:[1]"))
        stack.enter_context(patch.object(observer, "monotonic_ns", return_value=200_000_000_000))
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
        self.adjudicate = stack.enter_context(patch.object(monotonic_observer, "adjudicate_monotonic", return_value=(record, False)))

        def reject(_capture, logs, *_args, **_kwargs):
            list(logs)
            return self.adjudicate.return_value

        self.adjudicate.side_effect = reject
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

    def test_phase3_rejects_utc_only_window(self):
        argv = self.argv[:self.argv.index("--opened-monotonic-ns")]
        self.check_failure(self.run_observer(argv), stage="parse_arguments", reason="OBSERVER_ARGUMENTS_INVALID",
                           code=2, fallback=True)
        self.state_reader.assert_not_called()

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

    def test_pipe_watchdog_failure_uses_original_diagnostic_path(self):
        self.read_capture.side_effect = Stop("PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED")
        record = self.check_failure(self.run_observer(), stage="read_capture", reason="PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED")
        self.assertEqual(record["classification"], "OBSERVER_TIMEOUT_OR_CLOCK_FAILURE")
        self.assertFalse(self.destination.exists())

    def test_direct_descriptor_accepts_the_original_ready_frame_from_supervisor(self):
        ready = '{"type":"ready","started_ns":1}'
        self.assertEqual(self.run_observer(self.argv + ["--capture-ready-json", ready])[0], 2)
        self.assertEqual(self.read_capture.call_args.kwargs["ready_line"], ready.encode() + b"\n")

    def test_ready_argument_cannot_inject_a_second_frame(self):
        ready = '{"type":"ready","started_ns":1}\n{"type":"done"}'
        result = self.run_observer(self.argv + ["--capture-ready-json", ready])
        self.check_failure(result, stage="read_capture", reason="CAPTURE_READY_INVALID")
        self.read_capture.assert_not_called()

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
                self.root.joinpath("player.json.requests.json").unlink()

    def test_request_evidence_is_durable_before_failure_diagnostic(self):
        trace = "00000000-0000-4000-8000-000000000001"

        def fail(*_args, audit, **_kwargs):
            audit.append({"REQUEST_ID": trace, "ACK_RECEIVED_AT": ORIGIN.isoformat()})
            raise Stop("EDGE_WIRE_CORRELATION_INCOMPLETE")

        self.adjudicate.side_effect = fail
        record = self.check_failure(self.run_observer(), stage="adjudicate", reason="EDGE_WIRE_CORRELATION_INCOMPLETE")
        path = self.root / "player.json.requests.json"
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        evidence = json.loads(path.read_bytes())
        self.assertEqual(evidence["requests"], [{"REQUEST_ID": trace, "ACK_RECEIVED_AT": ORIGIN.isoformat()}])
        self.assertEqual(record["request_evidence"], {"file": path.name, "records": 1})

    def test_existing_request_evidence_blocks_and_is_preserved(self):
        path = self.root / "player.json.requests.json"
        path.write_bytes(b"earlier evidence")
        record = self.check_failure(self.run_observer(), stage="write_request_evidence",
                                    reason="OBSERVER_EVIDENCE_FILE_EXISTS")
        self.assertEqual(record["request_evidence"], {"status": "WRITE_FAILED", "records": 0})
        self.assertEqual(path.read_bytes(), b"earlier evidence")
        self.assertFalse(self.destination.exists())

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

    def test_live_baseline_signs_clock_sample_taken_before_query(self):
        key = b"k" * 32
        self.key.write_bytes(key)
        order = []

        def read_clock():
            order.append("clock")
            return 123

        def postgres(container, sql):
            self.assertEqual((container, sql), (PG, observer.BASELINE_QUERY))
            order.append("query")
            return [json.dumps({"as_of": ORIGIN.isoformat(), "devices": []}).encode()]

        with patch.object(observer, "monotonic_ns", side_effect=read_clock), \
             patch.object(observer, "postgres_rows", side_effect=postgres):
            code, _, _ = self.run_observer(self.baseline_args(self.destination))
        self.assertEqual((code, order), (0, ["clock", "query"]))
        payload = observer.verified_payload("PHASE3_BASELINE", json.loads(self.destination.read_bytes()), key, {},
                                            {"baseline_at", "baseline_refs", "baseline_monotonic_ns", "time_namespace"})
        self.assertEqual((payload["baseline_monotonic_ns"], payload["time_namespace"]), (123, "time:[1]"))

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


class Phase3PipeWatchdogTests(unittest.TestCase):
    def test_stalled_start_is_limited_by_opened_time(self):
        opened = 20_000_000_000
        stream = SimpleNamespace(fileno=lambda: 8)
        with patch.object(observer, "monotonic_ns", return_value=opened + 2_000_000_000), \
             patch.object(observer, "select", return_value=([], [], [])) as select_call:
            with self.assertRaisesRegex(Stop, "PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED"):
                observer.capture_input(stream, opened_monotonic_ns=opened, time_namespace="time:[1]")
        select_call.assert_called_once_with([8], [], [], 178.0)

    def test_ready_clock_tightens_deadline_and_wait_remains_absolute(self):
        opened = 20_000_000_000
        clock = {"type": "monotonic_clock", "ready_monotonic_ns": opened - 7_000_000_000,
                 "deadline_monotonic_ns": opened + 158_000_000_000, "time_namespace": "time:[1]"}
        ready = observer.phase3_ready_line('{"type":"ready","started_ns":1}')
        clock_line = json.dumps(clock).encode() + b"\n"
        stream = SimpleNamespace(fileno=lambda: 8)
        for pre_read, block in ((None, ready + clock_line), (ready, clock_line)):
            with self.subTest(pre_read=pre_read is not None):
                with patch.object(observer, "monotonic_ns", return_value=opened + 2_000_000_000), \
                     patch.object(observer, "select", side_effect=[([8], [], []), ([], [], [])]) as select_call, \
                     patch.object(observer.os, "read", return_value=block):
                    with self.assertRaisesRegex(Stop, "PLAYER_CAPTURE_PIPE_WATCHDOG_EXPIRED"):
                        observer.capture_input(stream, opened_monotonic_ns=opened, time_namespace="time:[1]", ready_line=pre_read)
                self.assertEqual([call.args[-1] for call in select_call.call_args_list], [178.0, 171.0])

    def test_fragmented_binary_pipe_preserves_exact_lines(self):
        stream = SimpleNamespace(fileno=lambda: 8)
        with patch.object(observer, "monotonic_ns", return_value=1), \
             patch.object(observer, "select", return_value=([8], [], [])), \
             patch.object(observer.os, "read", side_effect=[b"fi", b"rst\nsecond\nlas", b"t", b""]):
            lines = observer.capture_input(stream, opened_monotonic_ns=1, time_namespace="time:[1]")
        self.assertEqual(lines, [b"first\n", b"second\n", b"last"])
