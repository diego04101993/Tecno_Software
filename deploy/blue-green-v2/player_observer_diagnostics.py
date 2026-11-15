"""Private, bounded failure diagnostics for the Player observer CLI."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from subprocess import SubprocessError, TimeoutExpired
import sys
from tempfile import mkdtemp
from time import monotonic_ns, time_ns

from release_state import Stop, write_private_atomic


# Emit only codes chosen by the tooling. Arbitrary messages, traceback source
# lines, local variables, SQL, argv and subprocess streams can contain secrets.
SAFE_REASON_CODES = frozenset("""
ACK_QUERY_BOUNDS_EXCEEDED BASELINE_DOES_NOT_MATCH_SWITCH CAPTURE_COVERAGE_INCOMPLETE
CAPTURE_EVENT_FIELDS_INVALID CAPTURE_EVENT_INVALID CAPTURE_EVENT_PREDATES_CAPTURE
CAPTURE_FAILED CAPTURE_KERNEL_ACCOUNTING_INVALID CAPTURE_READY_INVALID CAPTURE_STATUS_INVALID
CAPTURE_STREAM_INCOMPLETE CAPTURE_STREAM_INVALID CAPTURE_TRACE_AMBIGUOUS
DOCKER_LOG_BOUNDS_EXCEEDED DOCKER_LOG_COLLECTION_FAILED DOCKER_LOG_COMMAND_MISMATCH
DOCKER_LOG_CONTAINER_ID_INVALID DOCKER_LOG_CONTAINER_MISMATCH DOCKER_LOG_STREAM_MISSING
DOCKER_LOG_WINDOW_INVALID DUPLICATE_JSON_KEY EDGE_ACCESS_LOG_NOT_REDACTED EDGE_AUTOSAVE_CHANGED
EDGE_AUTOSAVE_READ_FAILED EDGE_CAPTURE_COVERAGE_INCOMPLETE EDGE_ETAG_REQUIRED EDGE_READ_FAILED EDGE_READ_REJECTED
EDGE_LOG_BOUNDS_EXCEEDED EDGE_LOG_COVERAGE_INCOMPLETE EDGE_LOG_INVALID
EDGE_LOG_TIMESTAMP_INVALID EDGE_LOG_TRACE_OR_STATUS_INVALID EDGE_WIRE_CORRELATION_INCOMPLETE
HEARTBEAT_ACK_JOIN_NOT_UNIQUE HEARTBEAT_ACK_OUTSIDE_WINDOW INVALID_ACK_QUERY_CHANNEL
INVALID_CAPTURE_TIMESTAMP INVALID_JSON INVALID_JSON_CONSTANT INVALID_OBSERVATION_TARGET INVALID_OBSERVATION_WINDOW
INVALID_PHASE3_OBSERVATION_WINDOW INVALID_PLAYER_BASELINE_QUERY_RESULT INVALID_RECEIPT INVALID_RECEIPT_KEY
INVALID_STATE_ENVELOPE INVALID_TIMESTAMP
LOCAL_DOCKER_OBSERVER_FAILED OBSERVER_ENDPOINT_IDENTITY_INVALID OBSERVER_IDENTITY_OR_ROUTING_CHANGED
OBSERVER_READ_ONLY_TRANSACTION_FAILED OBSERVER_REQUIRES_API_ONLY_SWITCH
OBSERVER_REQUIRES_EXACT_LOCAL_IDENTITIES OBSERVER_REQUIRES_INITIAL_BLUE_BLUE
OBSERVER_REQUIRES_READY_TARGET OBSERVER_REQUIRES_STEADY_PREDECESSOR OBSERVER_WINDOW_END_IN_FUTURE
PHASE3_BACKEND_EPOCH_CHANGED PHASE3_ENDPOINT_UNHEALTHY PHASE3_INGRESS_CHANGED
PHASE3_INGRESS_TIME_INVALID PHASE3_POSTGRES_ID_MISMATCH PLAYER_BASELINE_INVALID PRIVATE_PATH_PERMISSIONS
PRIVATE_PATH_WRONG_TYPE RECEIPT_CONTEXT_MISMATCH RELEASE_STATE_MISSING ROUTE_SLOT_ENDPOINT_MISMATCH
STATE_CHECKSUM_MISMATCH UNAUTHENTICATED_RECEIPT WIRE_REQUEST_OUTSIDE_WINDOW
""".split())
CLOCK_REASONS = frozenset("""
CAPTURE_COVERAGE_INCOMPLETE CAPTURE_EVENT_PREDATES_CAPTURE DOCKER_LOG_WINDOW_INVALID
EDGE_LOG_TIMESTAMP_INVALID HEARTBEAT_ACK_OUTSIDE_WINDOW INVALID_CAPTURE_TIMESTAMP
INVALID_OBSERVATION_WINDOW INVALID_PHASE3_OBSERVATION_WINDOW INVALID_TIMESTAMP
OBSERVER_WINDOW_END_IN_FUTURE PHASE3_INGRESS_TIME_INVALID WIRE_REQUEST_OUTSIDE_WINDOW
""".split())
RECEIPT_STAGES = {"write_baseline_receipt", "write_player_receipt", "write_edge_log_evidence"}
BASELINE_INPUT_REASONS = {"BASELINE_DOES_NOT_MATCH_SWITCH", "INVALID_RECEIPT", "INVALID_RECEIPT_KEY",
                          "PLAYER_BASELINE_INVALID", "RECEIPT_CONTEXT_MISMATCH", "UNAUTHENTICATED_RECEIPT"}
TOOL_SOURCES = {"player_observer.py", "player_observer_diagnostics.py", "docker_logs.py",
                "release_state.py", "gates.py", "routectl.py", "edge_boot.py"}


class InvalidObserverArguments(ValueError):
    pass


class ObserverArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise InvalidObserverArguments from None


def _reason(error):
    if isinstance(error, InvalidObserverArguments):
        return "OBSERVER_ARGUMENTS_INVALID"
    if isinstance(error, Stop):
        value = str(error)
        return value if value in SAFE_REASON_CODES else "OBSERVER_VALIDATION_FAILED"
    if isinstance(error, FileExistsError):
        return "OBSERVER_EVIDENCE_FILE_EXISTS"
    if isinstance(error, PermissionError):
        return "OBSERVER_PERMISSION_DENIED"
    if isinstance(error, FileNotFoundError):
        return "OBSERVER_PATH_MISSING"
    if isinstance(error, TimeoutExpired):
        return "OBSERVER_SUBPROCESS_TIMEOUT"
    if isinstance(error, SubprocessError):
        return "OBSERVER_SUBPROCESS_FAILED"
    if isinstance(error, KeyboardInterrupt):
        return "OBSERVER_INTERRUPTED"
    if isinstance(error, OSError):
        return "OBSERVER_IO_FAILED"
    return "OBSERVER_INTERNAL_EXCEPTION"


def _classification(error, stage, reason):
    if stage in RECEIPT_STAGES or reason in {"OBSERVER_EVIDENCE_FILE_EXISTS"}:
        return "OBSERVER_RECEIPT_WRITE_FAILURE"
    if reason in CLOCK_REASONS or isinstance(error, TimeoutExpired):
        return "OBSERVER_TIMEOUT_OR_CLOCK_FAILURE"
    if stage == "collect_edge_logs":
        return "OBSERVER_LOG_COLLECTION_FAILURE"
    if (isinstance(error, (SubprocessError, KeyboardInterrupt)) or reason == "LOCAL_DOCKER_OBSERVER_FAILED"
            or stage == "inspect_endpoints" and isinstance(error, OSError)):
        return "OBSERVER_EXTERNAL_PROCESS_FAILURE"
    if reason in BASELINE_INPUT_REASONS:
        return "OBSERVER_INPUT_VALIDATION_FAILURE"
    if stage == "adjudicate" and isinstance(error, Stop):
        return "OBSERVER_CORRELATION_FAILURE"
    if isinstance(error, (Stop, InvalidObserverArguments, FileNotFoundError, PermissionError)):
        return "OBSERVER_INPUT_VALIDATION_FAILURE"
    return "OBSERVER_CODE_BUG"


def _frames(error):
    frames = []
    trace = error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        name = Path(code.co_filename).name
        if name in TOOL_SOURCES:
            frames.append({"file": name, "line": trace.tb_lineno, "function": code.co_name})
        trace = trace.tb_next
    return frames


class ObserverDiagnostics:
    def __init__(self):
        self.action = None
        self.destination = None
        self.stage = "parse_arguments"
        self.last_valid_stage = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.wall_started_ns = time_ns()
        self.monotonic_started_ns = monotonic_ns()
        self.counts = {}
        self.edge_log_streams = None
        self.window = None

    def advance(self, stage):
        self.last_valid_stage, self.stage = self.stage, stage

    def set_window(self, **times):
        if self.window is None:
            self.window = {}
        self.window.update({name: stamp.isoformat() for name, stamp in times.items()})

    def record_baseline_count(self, data):
        self.counts["baseline"] = len(data["baseline_refs"])

    def record_edge_streams(self, logs):
        self.edge_log_streams = {"stdout_bytes": len(logs.stdout), "stderr_bytes": len(logs.stderr),
                                 "returncode": logs.returncode, "command_sha256": logs.command_sha256}

    def record_adjudication(self, receipt):
        payload = receipt["payload"]
        baseline = set(payload["baseline_refs"])
        credited = {row["device_ref"] for row in payload["heartbeats"]}
        self.counts.update(baseline=len(baseline), observed=len(baseline & credited),
                           missing=len(baseline - credited), http_409=payload["http_409"],
                           http_5xx=payload["http_5xx"], incompatible=payload["incompatible"],
                           playback_201=payload["playback_201"])

    def fail(self, code, error=None):
        reason = _reason(error) if error is not None else "PLAYER_GATE_REJECTED"
        classification = (_classification(error, self.stage, reason) if error is not None
                          else "OBSERVER_CORRELATION_FAILURE")
        if (self.stage == "collect_edge_logs" and reason == "DOCKER_LOG_COLLECTION_FAILED"
                and self.edge_log_streams is not None and self.edge_log_streams["returncode"] is None):
            classification = "OBSERVER_TIMEOUT_OR_CLOCK_FAILURE"
        elapsed_ns = monotonic_ns() - self.monotonic_started_ns
        record = {"version": 1, "result": "FAIL", "reason": reason, "classification": classification,
                  "action": self.action, "stage": self.stage, "last_valid_stage": self.last_valid_stage,
                  "exit_code": code, "exception_type": type(error).__name__ if error is not None else None,
                  "traceback": _frames(error) if error is not None else [],
                  "observer_started_at": self.started_at, "observer_exited_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed_monotonic_ns": elapsed_ns,
                  "wall_minus_monotonic_ns": time_ns() - self.wall_started_ns - elapsed_ns,
                  "observation_window": self.window, "counts": dict(self.counts),
                  "primary_receipt_durability": ("UNCONFIRMED" if self.stage in {"write_baseline_receipt", "write_player_receipt"}
                                                 else "WRITTEN" if self.stage in {"report_baseline", "report_player_result"}
                                                 else "NOT_ATTEMPTED"),
                  "edge_log_streams": self.edge_log_streams, "diagnostic_write_errors": []}
        path = self._persist(record)
        try:
            if path is None:
                # When neither filesystem location is writable, the operator's
                # existing stderr capture can still preserve the full receipt.
                print("PLAYER_OBSERVER_FAILURE_RECEIPT=" + json.dumps(record, sort_keys=True), file=sys.stderr)
            if code == 1:
                print("PLAYER_OBSERVATION_STOP", file=sys.stderr)
            print(f"PLAYER_OBSERVER_RESULT=FAIL\nPLAYER_OBSERVER_REASON={reason}\n"
                  f"PLAYER_OBSERVER_STAGE={self.stage}\nPLAYER_OBSERVER_EXIT_CODE={code}\n"
                  f"PLAYER_OBSERVER_EVIDENCE_PATH={path if path is not None else 'stderr'}",
                  file=sys.stderr, flush=True)
        except (OSError, ValueError):
            # A closed stderr does not change the primary exit or disk receipt.
            pass
        return code

    def _persist(self, record):
        destination = self.destination
        if destination is not None:
            try:
                path = destination.with_name(destination.name + ".failure.json")
                write_private_atomic(path, self._encoded(record), replace=False)
                return path
            except Exception as error:
                record["diagnostic_write_errors"].append(_reason(error))
        try:
            # The new directory is private and unique. Its publication is
            # independent of the normal receipt directory and link.
            path = Path(mkdtemp(prefix="bgv2-player-observer-")) / "failure.json"
            write_private_atomic(path, self._encoded(record))
            return path
        except Exception as error:
            record["diagnostic_write_errors"].append(_reason(error))
            return None

    @staticmethod
    def _encoded(record):
        return (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode()
