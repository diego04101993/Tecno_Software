"""Private, durable state for the Blue-Green V2 routing controller."""

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

SLOTS = ("BLUE", "GREEN")
ROLES = ("api", "frontend")
PHASES = ("PREPARED", "STARTING", "READY", "ACTIVE", "STANDBY", "FAILED", "RETIRED")
HEX64 = re.compile(r"[0-9a-f]{64}")
EDGE_ETAG = re.compile(r'"/config/ [0-9a-f]{16}"')
EDGE_CONFIG_UPDATE = "EDGE_CONFIG_UPDATE"
EDGE_CONFIG_ROLLBACK = "EDGE_CONFIG_ROLLBACK"
EDGE_CONFIG_EVENTS = (EDGE_CONFIG_UPDATE, EDGE_CONFIG_ROLLBACK)
EVENT_STAGES = ("PREPARED", "DISPATCHED", "REJECTED")
ROUTING_RESULTS = ("COMMITTED", "ROLLED_BACK", "COMMITTED_FAILED")
CONFIG_RESULTS = ("COMMITTED", "ROLLED_BACK")
IDENTITY_FIELDS = ("container_id", "pid", "start_ticks", "started_at", "image_id", "revision",
                   "tree", "restart_count", "oom_killed", "upstream", "runtime_hash",
                   "mounts_hash", "network_hash")
EPOCH_IDENTITY_FIELDS = ("pid", "start_ticks", "started_at", "restart_count", "network_hash")
EPOCH_FIELDS = {"boot_id", "attested_at", *EPOCH_IDENTITY_FIELDS}


class Stop(RuntimeError):
    """A guard failed. The message is a safe, actionable failure code."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(_):
        raise Stop("INVALID_JSON_CONSTANT")

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise Stop("INVALID_JSON") from exc


def validate_release(release):
    require(isinstance(release, dict) and set(release) == {"revision", "tree", "images"},
            "INVALID_RELEASE")
    for field in ("revision", "tree"):
        require(isinstance(release[field], str) and re.fullmatch(r"[0-9a-f]{40}", release[field]),
                "INVALID_RELEASE")
    images = release["images"]
    require(isinstance(images, dict) and set(images) == set(ROLES), "INVALID_RELEASE_IMAGES")
    for image in images.values():
        require(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image),
                "INVALID_RELEASE_IMAGE")


def validate_routes(routes):
    require(isinstance(routes, dict) and set(routes) == set(ROLES)
            and all(value in SLOTS for value in routes.values()), "INVALID_ROUTE_PAIR")


def validate_hash(value):
    require(isinstance(value, str) and HEX64.fullmatch(value), "INVALID_STATE_HASH")


def validate_runtime_epoch(epoch):
    require(type(epoch) is dict and set(epoch) == EPOCH_FIELDS, "INVALID_LEGACY_RUNTIME_EPOCH")
    try:
        boot = epoch["boot_id"]
        started = datetime.fromisoformat(epoch["started_at"])
        attested = datetime.fromisoformat(epoch["attested_at"])
        valid = (str(UUID(boot)) == boot and started.utcoffset() is not None
                 and started.utcoffset().total_seconds() == 0 and attested.utcoffset() is not None
                 and attested.utcoffset().total_seconds() == 0 and started <= attested)
    except (ValueError, TypeError, AttributeError):
        valid = False
    require(valid and all(type(epoch[key]) is int and epoch[key] > 0 for key in ("pid", "start_ticks"))
            and type(epoch["restart_count"]) is int and epoch["restart_count"] >= 0,
            "INVALID_LEGACY_RUNTIME_EPOCH")
    validate_hash(epoch["network_hash"])


def same_runtime_epoch(left, right):
    return all(left[key] == right[key] for key in EPOCH_FIELDS - {"attested_at"})


def verify_intent(intent):
    required = {
        "generation", "previous_generation", "operation", "from_slot", "to_slot",
        "timestamp", "release", "expected_etag", "previous_hash", "next_hash", "intent_hash",
        "baseline_hash", "player_gate_hash",
    }
    require(isinstance(intent, dict) and set(intent) in (required, required | {"previous_record_hash"}),
            "INVALID_INTENT")
    if "previous_record_hash" in intent:
        validate_hash(intent["previous_record_hash"])
    unsigned = {key: value for key, value in intent.items() if key != "intent_hash"}
    require(digest(unsigned) == intent["intent_hash"], "INTENT_HASH_MISMATCH")
    require(type(intent["generation"]) is int and type(intent["previous_generation"]) is int
            and intent["generation"] > intent["previous_generation"] >= 0, "INVALID_GENERATION")
    require(intent["operation"] in ("API", "FRONTEND", "ROLLBACK"), "INVALID_OPERATION")
    if intent["operation"] == "API":
        validate_hash(intent["baseline_hash"])
    else:
        require(intent["baseline_hash"] is None, "UNEXPECTED_BASELINE_HASH")
    if intent["operation"] == "FRONTEND":
        validate_hash(intent["player_gate_hash"])
    else:
        require(intent["player_gate_hash"] is None, "UNEXPECTED_PLAYER_GATE_HASH")
    require(isinstance(intent["expected_etag"], str) and EDGE_ETAG.fullmatch(intent["expected_etag"]),
            "INVALID_ETAG")
    validate_routes(intent["from_slot"])
    validate_routes(intent["to_slot"])
    validate_release(intent["release"])
    validate_hash(intent["previous_hash"])
    validate_hash(intent["next_hash"])
    require(intent["previous_hash"] != intent["next_hash"]
            and intent["from_slot"] != intent["to_slot"], "INVALID_INTENT_NO_CHANGE")
    previous, target = intent["from_slot"], intent["to_slot"]
    if intent["operation"] == "API":
        require(previous["api"] == previous["frontend"] == target["frontend"] != target["api"],
                "INVALID_API_INTENT")
    elif intent["operation"] == "FRONTEND":
        require(previous["frontend"] != previous["api"] == target["api"] == target["frontend"],
                "INVALID_FRONTEND_INTENT")
    else:
        require(previous["api"] != target["api"] == target["frontend"], "INVALID_ROLLBACK_INTENT")


def edge_config_revision(state):
    return state.get("edge_config_revision", 0)


def next_routing_generation(state):
    """Config attempts never consume a routing generation."""
    return 1 + sum("intent" in record for record in state["history"])


def next_edge_config_revision(state):
    return 1 + sum("event" in record and record["result"] == "COMMITTED"
                   and record["event"]["type"] == EDGE_CONFIG_UPDATE for record in state["history"])


def edge_config_rollback_target(state):
    """Find the committed update establishing the active configuration revision."""
    revision = edge_config_revision(state)
    return next((record["event"] for record in reversed(state["history"])
                 if "event" in record and record["result"] == "COMMITTED"
                 and record["event"]["type"] == EDGE_CONFIG_UPDATE
                 and record["event"]["new_revision"] == revision), None)


def pending_event_type(state):
    pending = state["pending"]
    if pending is None:
        return None
    if "event" in pending:
        return pending["event"]["type"]
    return pending["intent"]["operation"]


def _validate_timestamp(value):
    try:
        utc = datetime.fromisoformat(value).utcoffset()
    except (ValueError, TypeError):
        utc = None
    require(utc is not None and utc.total_seconds() == 0, "INVALID_EDGE_CONFIG_TIMESTAMP")


def verify_edge_config_event(event):
    required = {
        "type", "reason", "previous_revision", "new_revision", "routing_generation", "active",
        "previous_hash", "next_hash", "previous_autosave_hash", "next_autosave_hash", "expected_etag",
        "payload_sha256", "timestamp", "previous_record_hash", "edge_boot_id", "event_hash",
    }
    require(type(event) is dict and set(event) == required, "INVALID_EDGE_CONFIG_EVENT")
    require(event["type"] in EDGE_CONFIG_EVENTS and event["reason"] == "TELEMETRY",
            "INVALID_EDGE_CONFIG_EVENT_TYPE")
    require(all(type(event[key]) is int and event[key] >= 0
                for key in ("previous_revision", "new_revision", "routing_generation")),
            "INVALID_EDGE_CONFIG_REVISION")
    validate_routes(event["active"])
    require(event["active"]["api"] == event["active"]["frontend"], "EDGE_CONFIG_ROUTING_NOT_STABLE")
    for key in ("previous_hash", "next_hash", "previous_autosave_hash", "next_autosave_hash",
                "payload_sha256", "event_hash"):
        validate_hash(event[key])
    if event["previous_record_hash"] is not None:
        validate_hash(event["previous_record_hash"])
    require(event["previous_hash"] == event["previous_autosave_hash"]
            and event["next_hash"] == event["next_autosave_hash"] == event["payload_sha256"]
            and event["previous_hash"] != event["next_hash"], "EDGE_CONFIG_EVENT_HASH_MISMATCH")
    require(isinstance(event["expected_etag"], str) and EDGE_ETAG.fullmatch(event["expected_etag"]),
            "INVALID_ETAG")
    boot = event["edge_boot_id"]
    try:
        valid_boot = boot is None or type(boot) is str and str(UUID(boot)) == boot
    except ValueError:
        valid_boot = False
    require(valid_boot, "INVALID_EDGE_CONFIG_BOOT_ID")
    _validate_timestamp(event["timestamp"])
    require(digest({key: value for key, value in event.items() if key != "event_hash"}) == event["event_hash"],
            "EDGE_CONFIG_EVENT_HASH_MISMATCH")


def _validate_config_position(event, *, generation, active, revision, updates, previous_record_hash):
    verify_edge_config_event(event)
    require(event["routing_generation"] == generation and event["active"] == active,
            "EDGE_CONFIG_EVENT_ROUTING_MISMATCH")
    require(event["previous_record_hash"] == previous_record_hash, "HISTORY_RECORD_HASH_MISMATCH")
    require(event["previous_revision"] == revision, "EDGE_CONFIG_EVENT_REVISION_MISMATCH")
    if event["type"] == EDGE_CONFIG_UPDATE:
        require(event["new_revision"] == len(updates) + 1, "EDGE_CONFIG_EVENT_REVISION_MISMATCH")
    else:
        origin = updates.get(revision)
        require(origin is not None and event["new_revision"] == origin["previous_revision"],
                "EDGE_CONFIG_ROLLBACK_TARGET_MISMATCH")
        if event["routing_generation"] == origin["routing_generation"]:
            require(event["previous_hash"] == origin["next_hash"] and event["next_hash"] == origin["previous_hash"],
                    "EDGE_CONFIG_ROLLBACK_HASH_MISMATCH")


def _validate_route_record_link(intent, has_config_history, previous_record_hash):
    require(("previous_record_hash" in intent) == has_config_history
            and (not has_config_history or intent["previous_record_hash"] == previous_record_hash),
            "HISTORY_RECORD_HASH_MISMATCH")


def validate_state(state):
    required = {
        "version", "slots", "routing", "rollback", "pending", "history",
    }
    optional = {"legacy_blue", "edge_config_revision"}
    require(isinstance(state, dict) and required <= set(state) <= required | optional
            and type(state["version"]) is int and state["version"] == 1, "INVALID_STATE")
    routing = state["routing"]
    require(isinstance(routing, dict) and set(routing) == {
        "generation", "active", "config_hash", "api_transition",
    }, "INVALID_COMMITTED_ROUTING")
    validate_routes(routing["active"])
    validate_hash(routing["config_hash"])
    require(type(routing["generation"]) is int and routing["generation"] >= 0,
            "INVALID_COMMITTED_GENERATION")
    slots = state["slots"]
    require(isinstance(slots, dict) and set(slots) == set(SLOTS), "INVALID_SLOTS")
    for slot in slots.values():
        if slot is None:
            continue
        require(isinstance(slot, dict) and set(slot) == {"phase", "release", "ready"}
                and slot["phase"] in PHASES, "INVALID_SLOT")
        validate_release(slot["release"])
        require(isinstance(slot["ready"], dict) and set(slot["ready"]) <= set(ROLES),
                "INVALID_READY")
        for entry in slot["ready"].values():
            require(isinstance(entry, dict) and set(entry) == {
                "identity", "identity_hash", "validated_at",
            },
                    "INVALID_READY_ENTRY")
            validate_hash(entry["identity_hash"])
            require(isinstance(entry["identity"], dict)
                    and set(entry["identity"]) == set(IDENTITY_FIELDS)
                    and digest(entry["identity"]) == entry["identity_hash"], "INVALID_READY_IDENTITY")
    for role, active_slot in routing["active"].items():
        require(slots[active_slot] is not None and role in slots[active_slot]["ready"],
                "ACTIVE_SLOT_MISSING")
    if "legacy_blue" in state:
        legacy = state["legacy_blue"]
        legacy_fields = {
            "manifest_sha256", "postgres_id", "frontend_stable_hash", "runtime_epochs",
        }
        require(type(legacy) is dict and set(legacy) in (legacy_fields, legacy_fields | {"frontend_manifest_sha256"}),
                "INVALID_LEGACY_ADOPTION_STATE")
        for key in ("manifest_sha256", "postgres_id", "frontend_stable_hash"):
            validate_hash(legacy[key])
        if "frontend_manifest_sha256" in legacy:
            validate_hash(legacy["frontend_manifest_sha256"])
            require(slots["BLUE"] is not None and slots["BLUE"]["phase"] != "RETIRED", "LEGACY_FRONTEND_ADOPTION_RETIRED")
        epochs = legacy["runtime_epochs"]
        require(type(epochs) is dict and set(epochs) == set(ROLES)
                and slots["BLUE"] is not None and set(slots["BLUE"]["ready"]) == set(ROLES),
                "INVALID_LEGACY_ADOPTION_STATE")
        for role, epoch in epochs.items():
            validate_runtime_epoch(epoch)
            identity = slots["BLUE"]["ready"][role]["identity"]
            require(all(identity[field] == epoch[field] for field in EPOCH_IDENTITY_FIELDS),
                    "LEGACY_RUNTIME_EPOCH_IDENTITY_MISMATCH")
        require(epochs["api"]["boot_id"] == epochs["frontend"]["boot_id"], "LEGACY_RUNTIME_EPOCH_BOOT_MISMATCH")
    rollback = state["rollback"]
    if rollback is not None:
        validate_routes(rollback)
        require(len(set(rollback.values())) == 1 and rollback["api"] != routing["active"]["api"]
                and slots[rollback["api"]] is not None
                and set(slots[rollback["api"]]["ready"]) == set(ROLES), "INVALID_ROLLBACK_PAIR")
    transition = routing["api_transition"]
    if transition is not None:
        require(isinstance(transition, dict) and set(transition) == {
            "slot", "prior_slot", "generation", "identity_hash", "switched_at", "release",
            "baseline_hash",
        }, "INVALID_API_TRANSITION")
        require(transition["slot"] == routing["active"]["api"]
                and transition["prior_slot"] in SLOTS
                and transition["slot"] != transition["prior_slot"]
                and type(transition["generation"]) is int
                and 0 < transition["generation"] <= routing["generation"],
                "INVALID_API_TRANSITION")
        validate_release(transition["release"])
        validate_hash(transition["identity_hash"])
        validate_hash(transition["baseline_hash"])
        require(rollback is not None and transition["prior_slot"] == rollback["api"]
                and transition["release"] == slots[transition["slot"]]["release"]
                and transition["identity_hash"] == slots[transition["slot"]]["ready"]["api"]["identity_hash"],
                "INVALID_API_TRANSITION")
    else:
        require(rollback is None, "MISSING_API_TRANSITION")

    _validate_history(state)


def _validate_history(state):
    routing = state["routing"]
    history = state["history"]
    require(isinstance(history, list), "INVALID_HISTORY")
    last_attempt = 0
    last_committed = 0
    last_hash = None
    last_routes = {"api": "BLUE", "frontend": "BLUE"}
    previous_record_hash = None
    has_config_history = False
    revision = 0
    updates = {}
    for record in history:
        require(isinstance(record, dict) and set(record) in ({"intent", "result", "recorded_at"},
                                                             {"event", "result", "recorded_at"}),
                "INVALID_HISTORY_ENTRY")
        if "event" in record:
            require(record["result"] in CONFIG_RESULTS, "INVALID_HISTORY_ENTRY")
            event = record["event"]
            _validate_config_position(event, generation=last_committed, active=last_routes,
                                      revision=revision, updates=updates, previous_record_hash=previous_record_hash)
            _validate_timestamp(record["recorded_at"])
            has_config_history = True
        else:
            require(record["result"] in ROUTING_RESULTS, "INVALID_HISTORY_ENTRY")
            event = record["intent"]
            verify_intent(event)
            _validate_route_record_link(event, has_config_history, previous_record_hash)
            require(event["generation"] == last_attempt + 1 and event["previous_generation"] == last_committed
                    and event["from_slot"] == last_routes, "HISTORY_GENERATION_MISMATCH")
            last_attempt = event["generation"]
        if last_hash is None:
            last_hash = event["previous_hash"]
        require(event["previous_hash"] == last_hash, "HISTORY_HASH_MISMATCH")
        if record["result"] != "ROLLED_BACK":
            last_hash = event["next_hash"]
            if "event" in record:
                revision = event["new_revision"]
                if event["type"] == EDGE_CONFIG_UPDATE:
                    updates[revision] = event
            else:
                last_committed = event["generation"]
                last_routes = event["to_slot"]
        previous_record_hash = digest(record)
    require(last_committed == routing["generation"] and last_routes == routing["active"]
            and (last_hash is None or last_hash == routing["config_hash"]), "COMMITTED_HISTORY_MISMATCH")
    require(("edge_config_revision" in state) == has_config_history and type(edge_config_revision(state)) is int
            and edge_config_revision(state) == revision, "COMMITTED_EDGE_CONFIG_REVISION_MISMATCH")

    pending = state["pending"]
    if pending is None:
        return
    require(isinstance(pending, dict) and set(pending) in ({"stage", "intent"}, {"stage", "event"})
            and pending["stage"] in EVENT_STAGES, "INVALID_PENDING")
    if "event" in pending:
        event = pending["event"]
        _validate_config_position(event, generation=last_committed, active=last_routes,
                                  revision=revision, updates=updates, previous_record_hash=previous_record_hash)
        require(event["previous_hash"] == routing["config_hash"], "PENDING_CONFIG_HASH_MISMATCH")
    else:
        intent = pending["intent"]
        verify_intent(intent)
        _validate_route_record_link(intent, has_config_history, previous_record_hash)
        require(intent["generation"] == last_attempt + 1
                and intent["previous_generation"] == routing["generation"]
                and intent["previous_hash"] == routing["config_hash"]
                and intent["from_slot"] == routing["active"], "PENDING_GENERATION_MISMATCH")

def require_private(path, directory=False):
    info = path.lstat()
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    require(expected_kind(info.st_mode), "PRIVATE_PATH_WRONG_TYPE")
    if os.name == "posix":
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "PRIVATE_PATH_PERMISSIONS")


def read_private(path):
    path = Path(path)
    require_private(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


def sync_dir(path):
    if os.name == "posix":
        fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_replace(source, target):
    if os.name == "nt":
        # The Windows test harness uses NTFS. WRITE_THROUGH makes the rename
        # durable without opening a directory handle through Python's POSIX API.
        import ctypes

        replace = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        replace.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong)
        replace.restype = ctypes.c_int
        if not replace(source, str(target), 0x01 | 0x08):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        sync_dir(target.parent)


def write_private_atomic(path, contents, replace=True):
    """Publish durable private bytes; a seal never replaces existing evidence."""
    path = Path(path)
    require_private(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            durable_replace(temporary, path)
        else:
            os.link(temporary, path)
            sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def exclusive_lock(path):
    if path.exists() or path.is_symlink():
        require_private(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise Stop("ROUTECTL_WRITER_LOCKED") from exc
    try:
        yield
    finally:
        os.close(fd)


class StateStore:
    """A single durable envelope holds the committed state and pending intent."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "routectl.lock"

    @contextlib.contextmanager
    def lock(self):
        created = not self.directory.exists()
        self.directory.mkdir(mode=0o700, exist_ok=True)
        require_private(self.directory, directory=True)
        if created:
            sync_dir(self.directory.parent)
        with exclusive_lock(self.lock_path):
            yield

    def load(self, allow_missing=False):
        if allow_missing and not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            envelope = decode(read_private(self.path))
        except FileNotFoundError as exc:
            raise Stop("RELEASE_STATE_MISSING") from exc
        require(isinstance(envelope, dict) and set(envelope) == {"sha256", "state"},
                "INVALID_STATE_ENVELOPE")
        require(digest(envelope["state"]) == envelope["sha256"], "STATE_CHECKSUM_MISMATCH")
        validate_state(envelope["state"])
        return envelope["state"]

    def save(self, state):
        validate_state(state)
        payload = canonical({"sha256": digest(state), "state": state}) + b"\n"
        write_private_atomic(self.path, payload)

    def seal_config(self, config):
        directory = self.directory / "generations"
        directory.mkdir(mode=0o700, exist_ok=True)
        require_private(directory, directory=True)
        sync_dir(self.directory)
        config_hash = digest(config)
        path = directory / (config_hash + ".json")
        if path.exists() or path.is_symlink():
            require(self.load_config(config_hash) == config, "SEALED_CONFIG_CHANGED")
        else:
            write_private_atomic(path, canonical(config), replace=False)
        return config_hash

    def load_config(self, config_hash):
        validate_hash(config_hash)
        directory = self.directory / "generations"
        try:
            require_private(directory, directory=True)
            config = decode(read_private(directory / (config_hash + ".json")))
        except FileNotFoundError:
            raise Stop("SEALED_CONFIG_MISSING") from None
        require(digest(config) == config_hash, "SEALED_CONFIG_CHANGED")
        return config
