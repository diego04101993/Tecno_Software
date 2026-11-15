"""Capture exact Docker log bytes, keeping stdout and stderr distinct."""

from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from subprocess import TimeoutExpired

from release_state import Stop, decode, digest, require


_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_CONTAINER = re.compile(r"[0-9a-f]{64}")
_TRACE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_BYTES = 64 * 1024 * 1024


def _timestamp_ns(value):
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("Invalid RFC3339Nano timestamp")
    second, fraction, offset = match.groups()
    delta = datetime.fromisoformat(second + ("+00:00" if offset == "Z" else offset)) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


@dataclass(frozen=True)
class DockerLogLine:
    stream: str
    timestamp: str
    timestamp_ns: int
    payload: bytes


@dataclass(frozen=True)
class DockerLogCapture:
    container_id: str
    since: str | None
    until: str
    collected_at: str
    command_sha256: str
    stdout: bytes
    stderr: bytes
    returncode: int | None = 0

    def evidence(self):
        """Return lossless evidence for a private sidecar, never for console output."""
        return {
            "container_id": self.container_id,
            "time_window": {"since_inclusive": self.since, "until_exclusive": self.until},
            "collected_at_utc": self.collected_at,
            "command_sha256": self.command_sha256,
            "returncode": self.returncode,
            "raw_encoding": "base64",
            "RAW_DOCKER_LOG_STDOUT": b64encode(self.stdout).decode("ascii"),
            "RAW_DOCKER_LOG_STDERR": b64encode(self.stderr).decode("ascii"),
        }

    def timeline(self, expected_container_id):
        require(self.container_id == expected_container_id, "DOCKER_LOG_CONTAINER_MISMATCH")
        require(self.returncode == 0, "DOCKER_LOG_COLLECTION_FAILED")
        start = _timestamp_ns(self.since) if self.since is not None else None
        finish = _timestamp_ns(self.until)
        merged = []
        for stream, raw in (("stdout", self.stdout), ("stderr", self.stderr)):
            for raw_line in raw.splitlines():
                prefix, separator, payload = raw_line.partition(b" ")
                if not separator:
                    continue
                try:
                    timestamp = prefix.decode("ascii")
                    stamp_ns = _timestamp_ns(timestamp)
                except (UnicodeError, ValueError):
                    # Untimestamped CLI diagnostics do not prove a container event.
                    continue
                if (start is None or start <= stamp_ns) and stamp_ns < finish:
                    merged.append(DockerLogLine(stream, timestamp, stamp_ns, payload))
        # Sort a view only. Equal timestamps retain the order in each stream.
        return sorted(merged, key=lambda line: line.timestamp_ns)

    def lines(self, expected_container_id):
        return (line.payload for line in self.timeline(expected_container_id))


def collect_docker_logs(invoke, container_id, *, since=None, until=None, preserve=None):
    """Invoke a local Docker wrapper that returns the binary CompletedProcess."""
    require(type(container_id) is str and _CONTAINER.fullmatch(container_id) is not None,
            "DOCKER_LOG_CONTAINER_ID_INVALID")
    until = datetime.now(timezone.utc).isoformat() if until is None else until
    try:
        finish = _timestamp_ns(until)
        start = _timestamp_ns(since) if since is not None else None
    except (TypeError, ValueError):
        raise Stop("DOCKER_LOG_WINDOW_INVALID") from None
    require(start is None or start < finish, "DOCKER_LOG_WINDOW_INVALID")
    args = ["container", "logs", "--timestamps"]
    if since is not None:
        args.extend(("--since", since))
    args.extend(("--until", until, container_id))
    try:
        result = invoke(*args)
        command, stdout, stderr = result.args, result.stdout, result.stderr
        returncode = result.returncode
    except TimeoutExpired as exc:
        command, stdout, stderr = exc.cmd, exc.output or b"", exc.stderr or b""
        returncode = None
    require(isinstance(command, (list, tuple)) and list(command[-len(args):]) == args, "DOCKER_LOG_COMMAND_MISMATCH")
    require(type(stdout) is bytes and type(stderr) is bytes, "DOCKER_LOG_STREAM_MISSING")
    require(len(stdout) + len(stderr) <= _MAX_BYTES, "DOCKER_LOG_BOUNDS_EXCEEDED")
    capture = DockerLogCapture(container_id, since, until, datetime.now(timezone.utc).isoformat(),
                               digest(list(command)), stdout, stderr, returncode)
    if preserve is not None:
        preserve(capture)
    require(returncode == 0, "DOCKER_LOG_COLLECTION_FAILED")
    return capture


def validate_caddy_request_id(request_id):
    require(type(request_id) is str and _TRACE.fullmatch(request_id) is not None,
            "CADDY_PROXY_REQUEST_ID_INVALID")


def verify_caddy_proxy_log(capture, container_id, *, request_id, generation, upstream, ingress="public"):
    """Require one exact, redacted Edge access record for a probe's response ID."""
    validate_caddy_request_id(request_id)
    matches = []
    for line in capture.timeline(container_id):
        try:
            row = decode(line.payload)
        except Stop:
            continue
        if (type(row) is dict and str(row.get("logger", "")).startswith("http.log.access")
                and row.get("request_id") == request_id):
            matches.append((line, row))
    require(len(matches) == 1, "CADDY_PROXY_LOG_NOT_UNIQUE")
    line, row = matches[0]
    require(row.get("ingress") == ingress and row.get("route_generation") == str(generation)
            and row.get("upstream") == upstream and type(row.get("status")) is int and row["status"] == 200
            and not ({"request", "resp_headers", "headers", "body"} & set(row)), "CADDY_PROXY_LOG_MISMATCH")
    return line
