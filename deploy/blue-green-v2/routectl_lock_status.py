"""Inspect Linux writer locks without acquiring a lock or changing a lock file."""

import os
from pathlib import Path
import re
import stat
import sys

from release_state import Stop, require


WRITER_COMMANDS = frozenset({
    "initialize", "preview", "prepare-handoff", "execute-handoff", "recover-ingress", "close-ingress",
    "supervise-ingress", "prepare", "start", "invalidate", "retire", "ready", "switch-api", "switch-frontend",
    "reconcile", "rollback",
})
VALUE_OPTIONS = frozenset({
    "--state-dir", "--socket", "--autosave", "--bindings", "--key", "--edge-name",
    "--legacy-blue-manifest", "--legacy-blue-sha256", "--legacy-frontend-manifest", "--legacy-frontend-sha256",
})


def _lock_inode(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "WRITER_LOCK_FILE_INVALID")
    return os.major(info.st_dev), os.minor(info.st_dev), info.st_ino


def _owners(paths):
    wanted = {_lock_inode(path) for path in paths} - {None}
    if not wanted:
        return set()
    result = set()
    with Path("/proc/locks").open(encoding="ascii") as kernel_locks:
        for line in kernel_locks:
            fields = line.split()
            if len(fields) < 8 or fields[1:3] != ["FLOCK", "ADVISORY"]:
                continue
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) in wanted:
                require(fields[3] == "WRITE", "WRITER_LOCK_MODE_INVALID")
                result.add(int(fields[4]))
    return result


def _action(argv, script):
    # Read only enough argv to classify the program. Never return its arguments.
    program = next((index for index, arg in enumerate(argv[1:], 1)
                    if arg and not arg.startswith("-")), None)
    if program is None or not Path(argv[program]).is_absolute() or Path(argv[program]).resolve() != script:
        return None
    args = iter(argv[program + 1:])
    for arg in args:
        name, separator, _ = arg.partition("=")
        if name not in VALUE_OPTIONS:
            return arg if arg in WRITER_COMMANDS else None
        if not separator:
            next(args, None)
    return None


def _identity(pid, script):
    if pid <= 0:
        return None
    proc = Path("/proc") / str(pid)
    first = _ticks(proc)
    process = (proc / "exe").readlink().name
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", process):
        return None
    with (proc / "cmdline").open("rb") as file:
        raw = file.read(65537)
    if len(raw) > 65536 or not raw.endswith(b"\0"):
        return None
    action = _action(os.fsdecode(raw[:-1]).split("\0"), script)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if action is None or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id):
        return None
    return {"action": action, "boot_id": boot_id, "start_ticks": first} if first == _ticks(proc) else None


def _ticks(proc):
    # The process command in /proc/PID/stat can contain spaces and parentheses.
    fields = (proc / "stat").read_bytes().rsplit(b")", 1)[1].split()
    ticks = int(fields[19])
    require(fields[0] != b"Z" and ticks > 0, "WRITER_PROCESS_IDENTITY_INVALID")
    return ticks


def writer_status(paths, script):
    """Return sanitized ownership and any reason that prevents a verified status."""
    unlocked = {"WRITER_LOCKED": "NO", "WRITER_OWNER": None, "WRITER_PID": None, "WRITER_IDENTITY": None}
    if sys.platform != "linux":
        return {**unlocked, "WRITER_LOCKED": "UNKNOWN"}, None
    try:
        for _ in range(2):
            owners = _owners(paths)
            if not owners:
                return unlocked, None
            pid = next(iter(owners)) if len(owners) == 1 else None
            try:
                identity = _identity(pid, script) if pid is not None else None
            except (OSError, Stop, IndexError, ValueError):
                identity = None
            if _owners(paths) != owners:
                continue
            owner = "UNKNOWN"
            if identity is not None:
                owner = "Edge supervisor" if identity["action"] == "supervise-ingress" else "routectl"
            return {"WRITER_LOCKED": "YES", "WRITER_OWNER": owner, "WRITER_PID": pid if pid and pid > 0 else None,
                    "WRITER_IDENTITY": identity}, None if identity is not None else "WRITER_LOCK_OWNER_UNVERIFIED"
    except (OSError, ValueError, Stop):
        return {**unlocked, "WRITER_LOCKED": "UNKNOWN", "WRITER_OWNER": "UNKNOWN"}, "WRITER_LOCK_EVIDENCE_INVALID"
    return {**unlocked, "WRITER_LOCKED": "UNKNOWN", "WRITER_OWNER": "UNKNOWN"}, "WRITER_LOCK_OBSERVATION_CHANGED"
