"""Start the monotonic observer in the verified collector's Linux time namespace."""

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from select import select
import signal
import stat
import subprocess
import sys
from time import monotonic_ns

from player_observer import InvalidObserverArguments, _parser as observer_parser, local_docker
from release_state import StateStore, Stop, canonical, decode, digest, read_private, require, require_private, write_private_atomic


NSENTER = Path("/usr/bin/nsenter")
INVALID_CLOCK = "PLAYER_MONOTONIC_CLOCK_INVALID"
TIME_NS = re.compile(r"time:\[[1-9][0-9]*\]")
OTHER_NAMESPACES = ("net", "mnt", "pid", "pid_for_children", "user", "uts", "cgroup", "ipc")
EPOCH_FIELDS = ("COLLECTOR_ID", "COLLECTOR_IMAGE", "COLLECTOR_HOST_PID", "COLLECTOR_STARTED_AT",
                "COLLECTOR_RESTART_COUNT", "COLLECTOR_PID_START_TICKS", "COLLECTOR_TIME_NS",
                "COLLECTOR_TIMENS_OFFSETS", "OBSERVER_ORIGINAL_TIME_NS", "OBSERVER_ORIGINAL_TIMENS_OFFSETS")
NANOSECONDS = 1_000_000_000
HANDSHAKE_SECONDS = 5
WINDOW_FLAGS = ("--started-at", "--finished-at", "--opened-monotonic-ns", "--started-monotonic-ns",
                "--finished-monotonic-ns")
PROMOTION_WINDOW_FLAGS = ("--opened-at", "--committed-state-sha256", *WINDOW_FLAGS)
PROMOTION_ACTIONS = ("promotion-baseline", "promotion-observe")


def _timens_offsets(raw):
    offsets = {}
    for line in raw.splitlines():
        fields = line.split()
        require(len(fields) == 3 and fields[0] in ("monotonic", "boottime") and fields[0] not in offsets
                and re.fullmatch(r"-?[0-9]+", fields[1]) and re.fullmatch(r"[0-9]+", fields[2]),
                "TIMENS_OFFSETS_INVALID")
        seconds, nanos = map(int, fields[1:])
        require(0 <= nanos < NANOSECONDS, "TIMENS_OFFSETS_INVALID")
        offsets[fields[0]] = [seconds, nanos]
    require(set(offsets) == {"monotonic", "boottime"}, "TIMENS_OFFSETS_INVALID")
    return offsets


def _proc_clock(proc):
    try:
        namespace = os.readlink(proc / "ns/time")
        offsets = _timens_offsets((proc / "timens_offsets").read_text(encoding="ascii"))
        require(os.readlink(proc / "ns/time") == namespace and TIME_NS.fullmatch(namespace), "TIME_NAMESPACE_CHANGED")
    except (OSError, UnicodeError) as exc:
        raise Stop("TIMENS_OFFSETS_OR_NAMESPACE_UNREADABLE") from exc
    return namespace, offsets


def _proc_start_ticks(proc):
    try:
        # /proc/PID/stat field 22. The parenthesized command can contain spaces and ")".
        value = int((proc / "stat").read_bytes().rsplit(b")", 1)[1].split()[19])
        require(value > 0, "COLLECTOR_PROCESS_EPOCH_INVALID")
    except (OSError, ValueError, IndexError) as exc:
        raise Stop("COLLECTOR_PROCESS_EPOCH_INVALID") from exc
    return value


def _inspect_collector(collector_id, image_id, restart_count):
    rows = decode(local_docker("container", "inspect", collector_id).stdout)
    require(type(rows) is list and len(rows) == 1 and type(rows[0]) is dict, "COLLECTOR_INSPECT_INVALID")
    row = rows[0]
    state = row.get("State")
    require(row.get("Id") == collector_id and row.get("Image") == image_id
            and type(row.get("RestartCount")) is int and row["RestartCount"] == restart_count
            and type(state) is dict and state.get("Running") is True and state.get("Status") == "running"
            and all(state.get(flag) is False for flag in ("OOMKilled", "Restarting", "Paused", "Dead"))
            and type(state.get("Pid")) is int and state["Pid"] > 0
            and type(state.get("StartedAt")) is str
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", state["StartedAt"])
            and not state["StartedAt"].startswith("0001-"), "COLLECTOR_IDENTITY_INVALID")
    pid = state["Pid"]
    proc = Path("/proc") / str(pid)
    ticks = _proc_start_ticks(proc)
    namespace, offsets = _proc_clock(proc)
    require(_proc_start_ticks(proc) == ticks, "COLLECTOR_PROCESS_EPOCH_CHANGED")
    return {"COLLECTOR_ID": collector_id, "COLLECTOR_IMAGE": image_id, "COLLECTOR_HOST_PID": pid,
            "COLLECTOR_STARTED_AT": state["StartedAt"], "COLLECTOR_RESTART_COUNT": restart_count,
            "COLLECTOR_PID_START_TICKS": ticks, "COLLECTOR_TIME_NS": namespace, "COLLECTOR_TIMENS_OFFSETS": offsets}


def _nsenter_version():
    require(NSENTER.is_file() and os.access(NSENTER, os.X_OK), "NSENTER_UNAVAILABLE")
    env = {**os.environ, "LC_ALL": "C"}
    help_text = subprocess.run([str(NSENTER), "--help"], capture_output=True, check=True, env=env,
                               timeout=HANDSHAKE_SECONDS).stdout
    require(b"--target" in help_text and (b"--time" in help_text or b"-T," in help_text), "NSENTER_TIME_UNAVAILABLE")
    version = subprocess.run([str(NSENTER), "--version"], capture_output=True, check=True, env=env,
                             timeout=HANDSHAKE_SECONDS).stdout.decode("ascii").strip()
    require(re.fullmatch(r"nsenter from util-linux [0-9A-Za-z.+~-]+", version), "NSENTER_VERSION_INVALID")
    return version


def _command(pid, report, release, argv):
    # Keep util-linux's fork. A time setns changes the namespace for children.
    return [str(NSENTER), "--target", str(pid), "--time", "--", sys.executable, "-E", "-B",
            str(Path(__file__).resolve()), "_child", str(report), str(release), *argv]


def _other_namespaces(proc):
    return {name: os.readlink(proc / "ns" / name) for name in OTHER_NAMESPACES}


def _child_clock(report):
    readable, _, _ = select([report], [], [], HANDSHAKE_SECONDS)
    require(bool(readable), "NSENTER_HANDSHAKE_TIMEOUT")
    raw = os.read(report, 4096)
    require(0 < len(raw) < 4096 and raw.endswith(b"\n"), "NSENTER_SETNS_OR_HANDSHAKE_FAILED")
    clock = decode(raw)
    require(type(clock) is dict and set(clock) == {"pid", "namespace", "offsets"}
            and type(clock["pid"]) is int and clock["pid"] > 0, "OBSERVER_CLOCK_REPORT_INVALID")
    return clock


def _join_and_run(collector, original_others, inspect, argv, evidence, evidence_path):
    report, report_child = os.pipe()
    release_child, release = os.pipe()
    process = None
    try:
        pid = collector["COLLECTOR_HOST_PID"]
        command = _command(pid, report_child, release_child, argv)
        require(command[:5] == [str(NSENTER), "--target", str(pid), "--time", "--"], "NSENTER_SCOPE_INVALID")
        process = subprocess.Popen(command, pass_fds=(report_child, release_child), start_new_session=True)
        os.close(report_child)
        report_child = None
        os.close(release_child)
        release_child = None
        clock = _child_clock(report)
        evidence.update(OBSERVER_PID=clock["pid"], OBSERVER_TIME_NS=clock["namespace"],
                        OBSERVER_TIMENS_OFFSETS=clock["offsets"])
        child = Path("/proc") / str(clock["pid"])
        require(os.getpgid(clock["pid"]) == process.pid
                and (clock["namespace"], clock["offsets"]) == _proc_clock(child)
                and _other_namespaces(child) == original_others, "OBSERVER_PROCESS_CLOCK_OR_SCOPE_CHANGED")
        require(clock["namespace"] == collector["COLLECTOR_TIME_NS"]
                and clock["offsets"] == collector["COLLECTOR_TIMENS_OFFSETS"], "OBSERVER_COLLECTOR_CLOCK_MISMATCH")
        require(inspect() == collector, "COLLECTOR_CHANGED_AFTER_LAUNCH")
        evidence.update(TIME_NAMESPACE_MATCH="YES", TIME_NAMESPACE_ONLY="YES", REASON=None)
        write_private_atomic(evidence_path, canonical(evidence), replace=False)
        os.write(release, b"1")
        return process.wait()
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    finally:
        for fd in (report, report_child, release_child, release):
            if fd is not None:
                os.close(fd)


def launch_observer_in_collector_time_namespace(collector_id, image_id, restart_count, argv, evidence_path, *, expected=None):
    """Verify and recheck the collector while the joined child waits for release."""
    path = Path(evidence_path)
    require_private(path.parent, directory=True)
    require(not path.exists() and not path.is_symlink(), "CLOCK_RECEIPT_ALREADY_EXISTS")
    evidence = {"TIME_NAMESPACE_MATCH": "NO", "TIME_NAMESPACE_ONLY": "UNVERIFIED", "OBSERVER_TIME_NS": None,
                "OBSERVER_TIMENS_OFFSETS": None, "OBSERVER_PID": None}
    stage = "validate_input"
    try:
        require(sys.platform == "linux" and re.fullmatch(r"[0-9a-f]{64}", collector_id)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                and type(restart_count) is int and restart_count >= 0
                and argv and Path(argv[0]).is_absolute(), "CLOCK_LAUNCH_INPUT_INVALID")
        stage = "inspect_original"
        proc = Path("/proc/self")
        original, offsets = _proc_clock(proc)
        original_others = _other_namespaces(proc)
        evidence.update(OBSERVER_ORIGINAL_TIME_NS=original, OBSERVER_ORIGINAL_TIMENS_OFFSETS=offsets,
                        OBSERVER_ORIGINAL_MONOTONIC_OFFSET=offsets["monotonic"])
        stage = "inspect_collector"

        def inspect():
            return _inspect_collector(collector_id, image_id, restart_count)

        collector = inspect()
        evidence.update(collector, COLLECTOR_MONOTONIC_OFFSET=collector["COLLECTOR_TIMENS_OFFSETS"]["monotonic"])
        require(expected is None or (expected.get("TIME_NAMESPACE_MATCH") == "YES"
                and all(field in expected and expected[field] == evidence[field] for field in EPOCH_FIELDS)),
                "COLLECTOR_OR_ORIGINAL_CLOCK_CHANGED_SINCE_PROBE")
        stage = "nsenter_preflight"
        evidence["NSENTER_VERSION"] = _nsenter_version()
        stage = "recheck_before_launch"
        require(inspect() == collector, "COLLECTOR_CHANGED_BEFORE_LAUNCH")
        stage = "join_and_recheck"
        return _join_and_run(collector, original_others, inspect, argv, evidence, path)
    except (Stop, OSError, UnicodeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        if not path.exists():
            evidence.update(RESULT=INVALID_CLOCK, STAGE=stage, REASON=str(exc) if isinstance(exc, Stop) else type(exc).__name__)
            write_private_atomic(path, canonical(evidence), replace=False)
        raise Stop(INVALID_CLOCK) from exc


def _confirm_child(report, release, argv):
    namespace, offsets = _proc_clock(Path("/proc/self"))
    row = canonical({"pid": os.getpid(), "namespace": namespace, "offsets": offsets}) + b"\n"
    with os.fdopen(report, "wb", buffering=0) as channel:
        channel.write(row)
    with os.fdopen(release, "rb", buffering=0) as channel:
        require(channel.read(1) == b"1", "CLOCK_LAUNCH_NOT_RELEASED")
    os.execv(argv[0], argv)


def _offset_ns(offsets):
    sec, ns = offsets["monotonic"]
    return sec * NANOSECONDS + ns


def promotion_switch_marker(state, observed_monotonic_ns):
    """Record the host clock immediately after the controller returns a committed API switch."""
    routes = state["routing"]
    transition = routes["api_transition"]
    require(transition is not None and state["pending"] is None and state["history"]
            and type(observed_monotonic_ns) is int and observed_monotonic_ns > 0,
            "PLAYER_API_SWITCH_NOT_COMMITTED")
    record = state["history"][-1]
    intent = record["intent"]
    active = routes["active"]
    require(record["result"] == "COMMITTED" and intent["operation"] == "API"
            and intent["to_slot"] == active == {"api": transition["slot"], "frontend": transition["prior_slot"]}
            and active["api"] != active["frontend"] and intent["generation"] == routes["generation"]
            == transition["generation"] and intent["next_hash"] == routes["config_hash"]
            and type(record["recorded_at"]) is str and record["recorded_at"] == transition["switched_at"],
            "PLAYER_API_SWITCH_NOT_COMMITTED")
    return {"event": "T_API_SWITCH_COMMITTED", "monotonic_ns": observed_monotonic_ns,
            "utc": transition["switched_at"], "state_sha256": digest(state)}


def _promotion_opening(rows, store):
    markers = [row for row in rows if row["event"] == "T_API_SWITCH_COMMITTED"]
    require(len(markers) == 1 and set(markers[0]) == {"event", "monotonic_ns", "utc", "state_sha256"},
            "PLAYER_WINDOW_TIMELINE_INVALID")
    marker = markers[0]
    state = store.load()
    require(marker["state_sha256"] == digest(state), "OBSERVER_ROUTING_CONTEXT_CHANGED")
    require(marker == promotion_switch_marker(state, marker["monotonic_ns"]), "PLAYER_WINDOW_TIMELINE_INVALID")
    return marker


def _window_arguments(started_at, opened, started):
    return ["--started-at", started_at.isoformat(), "--finished-at", (started_at + timedelta(seconds=120)).isoformat(),
            "--opened-monotonic-ns", str(opened), "--started-monotonic-ns", str(started),
            "--finished-monotonic-ns", str(started + 120 * NANOSECONDS)]


def _window_observer(timeline, clock_receipt, argv, *, store=None):
    clock = decode(read_private(clock_receipt))
    require(clock["TIME_NAMESPACE_MATCH"] == "YES"
            and _proc_clock(Path("/proc/self")) == (clock["OBSERVER_TIME_NS"], clock["OBSERVER_TIMENS_OFFSETS"]),
            INVALID_CLOCK)
    # Both timelines retain host coordinates. Sample the observer window in the joined clock.
    shift = _offset_ns(clock["OBSERVER_TIMENS_OFFSETS"]) - _offset_ns(clock["OBSERVER_ORIGINAL_TIMENS_OFFSETS"])
    if store is not None:
        require_private(timeline.parent, directory=True)
    fd = os.open(timeline, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "a+b") as log:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600,
                "PLAYER_WINDOW_TIMELINE_PERMISSIONS")
        log.seek(0)
        rows = [decode(line) for line in log]
        require(not any(row["event"] == "T_PLAYER_OBSERVATION_STARTED" for row in rows),
                "PLAYER_WINDOW_TIMELINE_INVALID")
        if store is None:
            opens = [row["monotonic_ns"] for row in rows if row["event"] == "T_INGRESS_READY"]
            first = [row["monotonic_ns"] for row in rows if row["event"] == "T_FIRST_HTTP_200"]
            require(len(opens) == len(first) == 1 and type(opens[0]) is type(first[0]) is int
                    and 0 < opens[0] <= first[0], "PLAYER_WINDOW_TIMELINE_INVALID")
            opened = opens[0] + shift
            verified = first[0] + shift
            opening = None
        else:
            opening = _promotion_opening(rows, store)
            opened = verified = opening["monotonic_ns"] + shift
        started = monotonic_ns()
        require(0 < opened <= verified <= started, "PLAYER_WINDOW_TIMELINE_INVALID")
        started_at = datetime.now(timezone.utc)
        marker = {"event": "T_PLAYER_OBSERVATION_STARTED", "utc": started_at.isoformat(),
                  "monotonic_ns": started - shift, "player_monotonic_ns": started,
                  "player_time_namespace": clock["OBSERVER_TIME_NS"]}
        if opening is not None:
            marker["state_sha256"] = opening["state_sha256"]
        log.write(canonical(marker) + b"\n")
        log.flush()
        os.fsync(log.fileno())
    from player_observer import main
    if opening is not None:
        argv = [*argv, "--opened-at", opening["utc"], "--committed-state-sha256", opening["state_sha256"],
                "--clock-receipt", str(clock_receipt)]
    return main([*argv, *_window_arguments(started_at, opened, started)])


def main(argv):
    if argv and argv[0] == "_child":
        _confirm_child(int(argv[1]), int(argv[2]), argv[3:])
    if argv and argv[0] == "_observe_window":
        return _window_observer(Path(argv[1]), Path(argv[2]), argv[3:])
    if argv and argv[0] == "_observe_promotion_window":
        return _window_observer(Path(argv[1]), Path(argv[2]), argv[4:], store=StateStore(argv[3]))
    split = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector-id", required=True)
    parser.add_argument("--collector-image", required=True)
    parser.add_argument("--collector-restart-count", required=True, type=int)
    parser.add_argument("--clock-receipt", required=True, type=Path)
    parser.add_argument("--preflight-receipt", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("probe")
    actions.add_parser("baseline")
    actions.add_parser("observe").add_argument("--timeline", required=True, type=Path)
    actions.add_parser("promotion-baseline")
    actions.add_parser("promotion-observe").add_argument("--timeline", required=True, type=Path)
    args = parser.parse_args(argv[:split])
    observer = argv[split + 1:]
    here = Path(__file__).resolve()
    python = [sys.executable, "-E", "-B"]
    expected = None
    if args.action == "probe":
        require(not observer and args.preflight_receipt is None, "CLOCK_PROBE_SCOPE_INVALID")
        command = [*python, "-c", "pass"]
    else:
        require(args.preflight_receipt is not None, "CLOCK_OBSERVER_COMMAND_INVALID")
        promotion = args.action in PROMOTION_ACTIONS
        flags = {"observe": WINDOW_FLAGS, "promotion-observe": PROMOTION_WINDOW_FLAGS}.get(args.action, ())
        owned = (*flags, "--clock-receipt") if promotion else flags
        options = {arg.split("=", 1)[0] for arg in observer if arg.startswith("--")}
        override = any(flag in options for flag in owned)
        if promotion:
            override = override or any(flag.startswith(option) for option in options for flag in owned)
        require(not override, "PLAYER_WINDOW_ARGUMENT_OVERRIDE")
        child_args = [*observer, "--clock-receipt", str(args.clock_receipt)] if promotion else observer
        # Placeholder types only. The joined child supplies every window boundary.
        validation = [*child_args, *(arg for flag in flags for arg in (flag, "0"))]
        try:
            parsed = observer_parser().parse_args(validation)
        except InvalidObserverArguments as exc:
            raise Stop("CLOCK_OBSERVER_COMMAND_INVALID") from exc
        scope = args.action if promotion else f"phase3-{args.action}"
        require(parsed.action == scope, "CLOCK_OBSERVER_COMMAND_INVALID")
        expected = decode(read_private(args.preflight_receipt))
        if args.action in ("baseline", "promotion-baseline"):
            command = [*python, str(here.with_name("player_observer.py")), *child_args]
        elif promotion:
            command = [*python, str(here), "_observe_promotion_window", str(args.timeline), str(args.clock_receipt),
                       parsed.state_dir, *observer]
        else:
            command = [*python, str(here), "_observe_window", str(args.timeline), str(args.clock_receipt), *observer]
    result = launch_observer_in_collector_time_namespace(args.collector_id, args.collector_image,
                                                        args.collector_restart_count, command, args.clock_receipt,
                                                        expected=expected)
    if args.action == "probe" and result == 0:
        print("TIME_NAMESPACE_MATCH=YES")
    return result


def _interrupted(_signum, _frame):
    raise Stop("CLOCK_LAUNCH_INTERRUPTED")


if __name__ == "__main__":
    try:
        if sys.platform == "linux" and sys.argv[1:2] not in (["_child"], ["_observe_window"], ["_observe_promotion_window"]):
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, _interrupted)
        sys.exit(main(sys.argv[1:]))
    except Stop as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
