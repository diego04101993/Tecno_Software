"""Route-only controller for Blue-Green V2. It cannot start or stop a slot."""

import argparse
import contextlib
import copy
import http.client
import json
import os
import re
import socket
import stat
from pathlib import Path

from gates import legacy_blue_endpoint, utc, verify_legacy_blue_manifest

from release_state import (
    IDENTITY_FIELDS, ROLES, SLOTS, StateStore, Stop, canonical, decode, digest, exclusive_lock, now, read_private,
    require, require_private, same_runtime_epoch, sync_dir, validate_hash, validate_release, write_private_atomic,
)

DEFAULT_HOSTS = {
    "api": "api.tecnocontrolcloud.com",
    "frontend": "app.tecnocontrolcloud.com",
}
BLUE_PAIR = {"api": "BLUE", "frontend": "BLUE"}
ADMIN_ADDRESS = "unix//run/bgv2/admin.sock|0600"
MAX_CONFIG_BYTES = 8 * 1024 * 1024


def upstream(role, slot):
    service, port = {"api": ("backend", 8000), "frontend": ("frontend", 80)}[role]
    return f"{service}-{slot.lower()}:{port}"


def walk_handlers(route):
    handlers = route.get("handle", [])
    require(isinstance(handlers, list), "INVALID_EDGE_HANDLERS")
    for handler in handlers:
        require(isinstance(handler, dict), "INVALID_EDGE_HANDLER")
        yield handler
        for child in handler.get("routes", []):
            yield from walk_handlers(child)


class RouteConfig:
    """The adapted Edge config is fixed except for public routing and its generation."""

    def __init__(self, config, hosts=None):
        self.config = config
        hosts = DEFAULT_HOSTS if hosts is None else hosts
        require(isinstance(hosts, dict) and set(hosts) == set(ROLES)
                and len(set(hosts.values())) == len(ROLES), "INVALID_ROUTE_HOSTS")
        try:
            admin = config["admin"]
            require(admin["listen"] == ADMIN_ADDRESS and admin.get("disabled", False) is False
                    and "remote" not in admin and "config" not in admin, "EDGE_ADMIN_NOT_PRIVATE")
            servers = config["apps"]["http"]["servers"]
            require(set(servers) == {"public", "preview-blue", "preview-green", "redirect"},
                    "EDGE_SERVER_SET_CHANGED")
            public = servers["public"]
            generation = re.fullmatch(r"bgv2-generation-(0|[1-9][0-9]*)", public["@id"])
            require(generation is not None, "INVALID_EDGE_GENERATION")
            self.generation = int(generation[1])
            require(public["listen"] == [":443"], "PUBLIC_LISTENER_CHANGED")
            require(servers["redirect"]["listen"] == [":80"], "REDIRECT_LISTENER_CHANGED")
            preview_ports = {"BLUE": "8444", "GREEN": "8443"}
            for server_name in ("public", "preview-blue", "preview-green"):
                server = servers[server_name]
                routes = server["routes"]
                if server_name == "public" and len(routes) == len(hosts) + 1:
                    from edge_boot import validate_ingress_guard

                    validate_ingress_guard(config)
                    routes = routes[1:]
                require(server.get("strict_sni_host") is True
                        and len(routes) == len(hosts)
                        and all(route.get("terminal") is True for route in routes),
                        "EDGE_HOST_OR_SNI_POLICY_CHANGED")
            self.active = {}
            self.proxies = {}
            self.generation_logs = {}
            self.targets = {(role, slot): upstream(role, slot) for role in ROLES for slot in SLOTS}
            blue_api, _ = self._role_handlers(servers["preview-blue"], hosts["api"])
            blue_dial = self._dial(blue_api)
            if blue_dial != upstream("api", "BLUE"):
                self.targets["api", "BLUE"] = legacy_blue_endpoint(blue_dial)
            for role, host in hosts.items():
                proxy, generation_log = self._role_handlers(public, host)
                require(generation_log["value"] == str(self.generation), "EDGE_GENERATION_LOG_CHANGED")
                self.proxies[role] = proxy
                self.generation_logs[role] = generation_log
                choices = {self.target_for(role, slot): slot for slot in SLOTS}
                dial = self._dial(proxy)
                require(dial in choices, "UNKNOWN_PUBLIC_UPSTREAM")
                self.active[role] = choices[dial]
                for slot in SLOTS:
                    preview = servers[f"preview-{slot.lower()}"]
                    require(preview["listen"] == [f"127.0.0.1:{preview_ports[slot]}"],
                            "PREVIEW_LISTENER_NOT_PRIVATE")
                    preview_proxy, preview_generation = self._role_handlers(preview, host)
                    require(self._dial(preview_proxy) == self.target_for(role, slot),
                            "UNKNOWN_PREVIEW_UPSTREAM")
                    require(preview_generation["value"] == "preview",
                            "PREVIEW_GENERATION_LOG_CHANGED")
                    public_shape = {key: value for key, value in proxy.items() if key != "upstreams"}
                    preview_shape = {key: value for key, value in preview_proxy.items() if key != "upstreams"}
                    require(public_shape == preview_shape, "PREVIEW_PROXY_POLICY_MISMATCH")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise Stop("INVALID_EDGE_ROUTE_CONFIG") from exc
        self.public = public
        self.hosts = hosts
        self.hash = digest(config)

    def target_for(self, role, slot):
        return self.targets[role, slot]

    @staticmethod
    def _role_handlers(server, host):
        routes = [route for route in server["routes"] if route.get("match") == [{"host": [host]}]]
        require(len(routes) == 1, "AMBIGUOUS_EDGE_HOST_ROUTE")
        handlers = list(walk_handlers(routes[0]))
        proxies = [handler for handler in handlers if handler.get("handler") == "reverse_proxy"]
        generation_logs = [handler for handler in handlers if handler.get("handler") == "log_append"
                           and handler.get("key") == "route_generation"]
        require(len(proxies) == 1 and len(generation_logs) == 1,
                "AMBIGUOUS_EDGE_ROUTE_HANDLERS")
        return proxies[0], generation_logs[0]

    @staticmethod
    def _dial(proxy):
        targets = proxy["upstreams"]
        require(isinstance(targets, list) and len(targets) == 1
                and isinstance(targets[0], dict) and set(targets[0]) == {"dial"},
                "AMBIGUOUS_EDGE_UPSTREAM")
        return targets[0]["dial"]

    def changed(self, routes, generation):
        result = RouteConfig(copy.deepcopy(self.config), self.hosts)
        result.public["@id"] = f"bgv2-generation-{generation}"
        for role, slot in routes.items():
            result.proxies[role]["upstreams"][0]["dial"] = result.target_for(role, slot)
            result.generation_logs[role]["value"] = str(generation)
        return RouteConfig(result.config, self.hosts)


def bind_legacy_blue(config, manifest, expected_sha256):
    """Bind only the two generation-zero BLUE API dials before sealing."""
    target = verify_legacy_blue_manifest(manifest, expected_sha256)["stable_identity"]["upstream"]
    result = copy.deepcopy(config)
    try:
        servers = result["apps"]["http"]["servers"]
        require(servers["public"].get("@id", "bgv2-generation-0") == "bgv2-generation-0",
                "LEGACY_BLUE_BINDING_REQUIRES_INITIAL_GENERATION")
        for server in ("public", "preview-blue"):
            proxy, _ = RouteConfig._role_handlers(servers[server], DEFAULT_HOSTS["api"])
            require(RouteConfig._dial(proxy) == upstream("api", "BLUE"), "LEGACY_BLUE_INITIAL_DIAL_INVALID")
            proxy["upstreams"][0]["dial"] = target
    except (KeyError, TypeError, AttributeError, ValueError):
        raise Stop("INVALID_EDGE_ROUTE_CONFIG") from None
    return result


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout)
            client.connect(os.fspath(self.path))
        except BaseException:
            client.close()
            raise
        self.sock = client


class CaddyEdge:
    """Read the effective configuration and independently verify its autosave."""

    def __init__(self, socket_path=None, autosave_path=None, hosts=None, timeout=5):
        self.socket_path = Path(socket_path) if socket_path else None
        self.autosave_path = Path(autosave_path) if autosave_path else None
        self.hosts = DEFAULT_HOSTS if hosts is None else hosts
        self.timeout = timeout

    def request(self, method, etag=None, config=None):
        require(self.socket_path is not None and os.name == "posix", "EDGE_UNIX_SOCKET_REQUIRED")
        require_private(self.socket_path.parent, directory=True)
        info = self.socket_path.lstat()
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "EDGE_UNIX_SOCKET_PERMISSIONS")
        connection = UnixHTTPConnection(self.socket_path, self.timeout)
        headers = {"If-Match": etag, "Content-Type": "application/json"} if config is not None else {}
        try:
            connection.request(method, "/config/", body=canonical(config) if config is not None else None,
                               headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_CONFIG_BYTES + 1)
            require(len(body) <= MAX_CONFIG_BYTES, "EDGE_CONFIG_TOO_LARGE")
            return response.status, response.getheader("ETag"), body
        finally:
            connection.close()

    def lock(self):
        if self.socket_path is None:
            return contextlib.nullcontext()
        require_private(self.socket_path.parent, directory=True)
        # Different --state-dir values cannot acquire two writers for one Edge.
        return exclusive_lock(self.socket_path.with_name(self.socket_path.name + ".writer.lock"))

    def read_autosave(self):
        require(self.autosave_path is not None and os.name == "posix", "EDGE_AUTOSAVE_REQUIRED")
        require_private(self.autosave_path.parent, directory=True)
        require_private(self.autosave_path)
        fd = os.open(self.autosave_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                    and stat.S_IMODE(before.st_mode) == 0o600, "EDGE_AUTOSAVE_PERMISSIONS")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
            require(len(raw) <= MAX_CONFIG_BYTES, "EDGE_CONFIG_TOO_LARGE")
            os.fsync(stream.fileno())
            after = self.autosave_path.lstat()
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "EDGE_AUTOSAVE_CHANGED")
        sync_dir(self.autosave_path.parent)
        return raw

    def effective(self):
        config, etag = self.raw_effective()
        return RouteConfig(config, self.hosts), etag

    def raw_effective(self):
        try:
            status, etag, raw = self.request("GET")
        except (OSError, http.client.HTTPException) as exc:
            raise Stop("EDGE_READ_FAILED") from exc
        require(status == 200, "EDGE_READ_REJECTED")
        require(isinstance(etag, str) and re.fullmatch(r'"/config/ [0-9a-f]{16}"', etag),
                "EDGE_ETAG_REQUIRED")
        return decode(raw), etag

    def persisted(self):
        try:
            return RouteConfig(decode(self.read_autosave()), self.hosts)
        except OSError as exc:
            raise Stop("EDGE_AUTOSAVE_READ_FAILED") from exc

    def patch(self, config, etag):
        try:
            status, _, _ = self.request("PATCH", etag, config)
        except (OSError, http.client.HTTPException) as exc:
            raise Stop("EDGE_MUTATION_OUTCOME_UNKNOWN") from exc
        return status

    @property
    def ingress_marker(self):
        require(self.socket_path is not None, "EDGE_UNIX_SOCKET_REQUIRED")
        return self.socket_path.with_name("ingress-ready")

    def close_ingress(self):
        marker = self.ingress_marker
        require_private(marker.parent, directory=True)
        marker.unlink(missing_ok=True)
        sync_dir(marker.parent)

    def open_ingress(self, config_hash):
        from edge_boot import validate_ingress_guard

        effective, _ = self.effective()
        validate_ingress_guard(effective.config)
        require(effective.hash == self.persisted().hash == config_hash, "STOP_AMBIGUOUS")
        write_private_atomic(self.ingress_marker, (config_hash + "\n").encode(), replace=False)

    def ingress_open(self):
        marker = self.ingress_marker
        if not marker.exists() and not marker.is_symlink():
            return False
        try:
            return re.fullmatch(rb"[0-9a-f]{64}\n", read_private(marker)) is not None
        except OSError:
            return False

    def boot_token(self):
        return read_private(self.ingress_marker.with_name("boot-id"))


class Controller:
    """Desired release state changes only through proven route transitions."""

    def __init__(self, store, edge, probe, ready_gate, player_gate):
        self.store = store
        self.edge = edge
        self.probe = probe
        self.ready_gate = ready_gate
        self.player_gate = player_gate

    @contextlib.contextmanager
    def _locked(self):
        with self.edge.lock(), self.store.lock():
            yield

    def _current(self, state):
        require(state["pending"] is None, "RECONCILE_PENDING_INTENT")
        effective, etag = self.edge.effective()
        persisted = self.edge.persisted()
        require(effective.hash == persisted.hash == state["routing"]["config_hash"]
                and effective.generation == state["routing"]["generation"]
                and effective.active == state["routing"]["active"], "STOP_AMBIGUOUS")
        if self.edge.socket_path is not None:
            require(self.edge.ingress_open(), "INGRESS_NOT_READY")
        self._require_legacy_binding(state, effective)
        self._verify_route_bindings(state, effective)
        return effective, etag

    def _validate_identity(self, role, slot, release, identity, *, legacy_epoch=False):
        target = upstream(role, slot)
        if (role, slot) == ("api", "BLUE"):
            target = getattr(self.probe, "legacy_blue_endpoint", None) or target
        require(isinstance(identity, dict) and set(identity) == set(IDENTITY_FIELDS)
                and identity["image_id"] == release["images"][role]
                and identity["revision"] == release["revision"]
                and identity["tree"] == release["tree"]
                and identity["upstream"] == target, "SLOT_RELEASE_IDENTITY_MISMATCH")
        require(type(identity["pid"]) is int and identity["pid"] > 0
                and type(identity["restart_count"]) is int
                and identity["restart_count"] >= 0 and (legacy_epoch or identity["restart_count"] == 0)
                and identity["oom_killed"] is False
                and isinstance(identity["started_at"], str) and identity["started_at"]
                and isinstance(identity["start_ticks"], int) and identity["start_ticks"] > 0,
                "SLOT_PROCESS_IDENTITY_INVALID")
        for field in ("container_id", "runtime_hash", "mounts_hash", "network_hash"):
            validate_hash(identity[field])
        return identity

    def _identity(self, role, slot, release):
        return self._validate_identity(role, slot, release, self.probe(role, slot))

    def _require_legacy_binding(self, state, route_config):
        legacy = state.get("legacy_blue")
        if route_config.target_for("api", "BLUE") != upstream("api", "BLUE"):
            require(legacy is not None and legacy["manifest_sha256"] == getattr(self.probe, "legacy_blue_sha256", None)
                    and legacy["postgres_id"] == self.probe.legacy_blue["postgres_id"], "LEGACY_BLUE_ADOPTION_STATE_MISMATCH")
        else:
            require(legacy is None, "UNEXPECTED_LEGACY_BLUE_ADOPTION_STATE")

    def _legacy_snapshots(self, release):
        snapshots = {"api": self.probe.legacy_api_snapshot(), "frontend": self.probe.legacy_frontend_snapshot()}
        for role, snapshot in snapshots.items():
            self._validate_identity(role, "BLUE", release, snapshot["identity"], legacy_epoch=True)
        require(snapshots["api"]["runtime_epoch"]["boot_id"] == snapshots["frontend"]["runtime_epoch"]["boot_id"],
                "LEGACY_RUNTIME_EPOCH_BOOT_MISMATCH")
        return snapshots

    def _verified_ready(self, role, slot, release, receipt, *, initial_adoption=False, identity=None):
        if identity is None:
            identity = self._identity(role, slot, release)
        context = {"role": role, "slot": slot, "release": release,
                   "identity_hash": digest(identity), "started_at": identity["started_at"]}
        if (role, slot) == ("api", "BLUE") and getattr(self.probe, "legacy_blue_endpoint", None):
            require(initial_adoption, "LEGACY_BLUE_REQUIRES_INITIAL_ADOPTION")
            context["adoption"] = "LEGACY_BLUE"
        require(self.ready_gate(receipt, context) is True, "READY_RECEIPT_REJECTED")
        return {"identity": identity, "identity_hash": context["identity_hash"], "validated_at": now()}

    def _assert_ready(self, state, role, slot, allow_failed=False):
        entry = state["slots"][slot]
        phases = ("READY", "ACTIVE", "STANDBY", "FAILED") if allow_failed else ("READY", "ACTIVE", "STANDBY")
        require(entry is not None and entry["phase"] in phases
                and role in entry["ready"], "SLOT_NOT_READY")
        legacy = state.get("legacy_blue") if slot == "BLUE" else None
        if legacy is not None:
            epoch = legacy["runtime_epochs"][role]
            if role == "api":
                identity = self.probe.classify_legacy_blue(approved_epoch=epoch)[1]
            else:
                snapshot = self.probe.legacy_frontend_snapshot()
                require(digest(snapshot["stable_identity"]) == legacy["frontend_stable_hash"],
                        "LEGACY_BLUE_FRONTEND_UNKNOWN_OR_DRIFTED")
                require(same_runtime_epoch(snapshot["runtime_epoch"], epoch),
                        "STOP_REATTEST_REQUIRED" if snapshot["runtime_epoch"]["boot_id"] == epoch["boot_id"]
                        else "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT_REQUIRED")
                identity = snapshot["identity"]
            require(digest(self._validate_identity(role, slot, entry["release"], identity, legacy_epoch=True))
                    == entry["ready"][role]["identity_hash"], "LEGACY_BLUE_DRIFT")
            return identity
        try:
            identity = self._identity(role, slot, entry["release"])
            require(digest(identity) == entry["ready"][role]["identity_hash"], "READY_IDENTITY_CHANGED")
            return identity
        except Stop:
            entry["phase"] = "FAILED"
            self.store.save(state)
            raise

    @staticmethod
    def _verify_route_bindings(state, route_config):
        for slot, record in state["slots"].items():
            if record is not None:
                for role, ready in record["ready"].items():
                    require(ready["identity"]["upstream"] == route_config.target_for(role, slot),
                            "ROUTE_SLOT_ENDPOINT_MISMATCH")

    def _verify_targets(self, state, routes, allow_failed=False, route_config=None):
        if route_config is not None:
            self._verify_route_bindings(state, route_config)
        for role, slot in routes.items():
            self._assert_ready(state, role, slot, allow_failed=allow_failed)

    def _verify_promotion(self, state, operation, routes):
        # A previous failed inspection can reflect a temporary probe error.
        # Emergency rollback still requires an exact live revalidation.
        self._verify_targets(state, routes, allow_failed=(operation == "ROLLBACK"))
        if operation == "API":
            slot = routes["api"]
            self._assert_ready(state, "frontend", slot)
        if operation != "ROLLBACK":
            previous = state["routing"]["active"]
            self._verify_targets(state, previous)
            rollback = state["rollback"]
            if rollback is not None:
                for role, slot in rollback.items():
                    if previous[role] != slot:
                        self._assert_ready(state, role, slot)

    def initialize(self, release, receipts_by_role, baseline_hash=None):
        validate_release(release)
        validate_hash(baseline_hash)
        require(isinstance(receipts_by_role, dict) and set(receipts_by_role) == set(ROLES),
                "READY_RECEIPTS_REQUIRED")
        with self._locked():
            require(self.store.load(allow_missing=True) is None, "ALREADY_INITIALIZED")
            effective, _ = self.edge.effective()
            require(effective.generation == 0 and effective.active == BLUE_PAIR
                    and effective.hash == baseline_hash == self.edge.persisted().hash,
                    "BASELINE_CONFIG_MISMATCH")
            state = self._initial_state(release, receipts_by_role, effective.hash)
            self._verify_targets(state, BLUE_PAIR)
            self._current(state)
            self.store.seal_config(effective.config)
            self.store.save(state)
            return state

    def _initial_state(self, release, receipts_by_role, config_hash):
        legacy = getattr(self.probe, "legacy_blue_endpoint", None) is not None
        snapshots = self._legacy_snapshots(release) if legacy else {}
        if legacy:
            require(same_runtime_epoch(snapshots["api"]["runtime_epoch"], self.probe.legacy_blue["runtime_epoch"])
                    and snapshots["frontend"]["runtime_epoch"]["restart_count"] == 0,
                    "LEGACY_BLUE_DRIFT")
        ready = {role: self._verified_ready(role, "BLUE", release, receipts_by_role[role], initial_adoption=True,
                                            identity=snapshots.get(role, {}).get("identity")) for role in ROLES}
        state = {
            "version": 1,
            "slots": {"BLUE": {"phase": "ACTIVE", "release": release, "ready": ready}, "GREEN": None},
            "routing": {"generation": 0, "active": BLUE_PAIR.copy(), "config_hash": config_hash,
                        "api_transition": None},
            "rollback": None,
            "pending": None,
            "history": [],
        }
        if legacy:
            state["legacy_blue"] = {
                "manifest_sha256": self.probe.legacy_blue_sha256, "postgres_id": self.probe.legacy_postgres_id(),
                "frontend_stable_hash": digest(snapshots["frontend"]["stable_identity"]),
                "runtime_epochs": {role: snapshots[role]["runtime_epoch"] for role in ROLES},
            }
        return state

    def reattest_legacy_blue_after_host_reboot(self, state):
        legacy = state.get("legacy_blue")
        if legacy is None:
            return None
        before = legacy["runtime_epochs"]
        if self.probe.legacy_engine_boot_id() == before["api"]["boot_id"]:
            return None
        require(state["routing"]["active"] == BLUE_PAIR and state["pending"] is None
                and state["rollback"] is None and state["slots"]["BLUE"]["phase"] == "ACTIVE",
                "LEGACY_BLUE_REATTEST_SCOPE_NOT_SAFE")
        self.probe.require_legacy_reboot_health_ready(state["slots"]["BLUE"]["ready"], legacy["postgres_id"])
        release = state["slots"]["BLUE"]["release"]
        snapshots = self._legacy_snapshots(release)
        require(digest(snapshots["frontend"]["stable_identity"]) == legacy["frontend_stable_hash"],
                "LEGACY_BLUE_FRONTEND_UNKNOWN_OR_DRIFTED")
        epochs = {role: snapshots[role]["runtime_epoch"] for role in ROLES}
        new_boot = epochs["api"]["boot_id"]
        require(new_boot != before["api"]["boot_id"] and self.probe.legacy_engine_boot_id() == new_boot,
                "LEGACY_RUNTIME_EPOCH_BOOT_MISMATCH")
        require(all(utc(epochs[role]["started_at"]) > utc(before[role]["started_at"]) for role in ROLES),
                "LEGACY_RUNTIME_EPOCH_DID_NOT_RESTART")
        require(self.probe.legacy_postgres_id() == legacy["postgres_id"], "LEGACY_POSTGRES_NOT_EXACT_HEALTHY")
        for role in ROLES:
            identity = snapshots[role]["identity"]
            state["slots"]["BLUE"]["ready"][role] = {"identity": identity, "identity_hash": digest(identity),
                                                      "validated_at": epochs[role]["attested_at"]}
        self.probe.verify_legacy_runtime_running(state["slots"]["BLUE"]["ready"], epochs["api"])
        legacy["runtime_epochs"] = epochs
        self.store.save(state)
        return "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT"

    def status(self):
        with self._locked():
            state = self.store.load()
            self._current(state)
            return state

    def prepare(self, slot, release):
        require(slot in SLOTS, "UNKNOWN_SLOT")
        validate_release(release)
        with self._locked():
            state = self.store.load()
            self._current(state)
            require(slot not in state["routing"]["active"].values()
                    and (state["rollback"] is None or slot not in state["rollback"].values()),
                    "SLOT_PROTECTED")
            entry = state["slots"][slot]
            require(entry is None or entry["phase"] in ("RETIRED", "FAILED"),
                    "SLOT_ALREADY_PREPARED")
            state["slots"][slot] = {"phase": "PREPARED", "release": release, "ready": {}}
            self.store.save(state)
            return state

    def invalidate(self, slot):
        """Record failed QA for a candidate without changing any live route."""
        require(slot in SLOTS, "UNKNOWN_SLOT")
        with self._locked():
            state = self.store.load()
            self._current(state)
            require(slot not in state["routing"]["active"].values()
                    and (state["rollback"] is None or slot not in state["rollback"].values()),
                    "SLOT_PROTECTED")
            entry = state["slots"][slot]
            require(entry is not None and entry["phase"] in ("PREPARED", "STARTING", "READY"),
                    "SLOT_NOT_A_CANDIDATE")
            entry["phase"] = "FAILED"
            self.store.save(state)
            return state

    def start(self, slot):
        require(slot in SLOTS, "UNKNOWN_SLOT")
        with self._locked():
            state = self.store.load()
            self._current(state)
            entry = state["slots"][slot]
            require(entry is not None and entry["phase"] == "PREPARED", "SLOT_NOT_PREPARED")
            entry["phase"] = "STARTING"
            self.store.save(state)
            return state

    def register_ready(self, role, slot, receipt):
        require(slot in SLOTS and role in ROLES, "UNKNOWN_SLOT_OR_ROLE")
        with self._locked():
            state = self.store.load()
            self._current(state)
            entry = state["slots"][slot]
            require(entry is not None and entry["phase"] == "STARTING", "SLOT_NOT_STARTING")
            ready = entry["ready"]
            require(role not in ready, "ROLE_ALREADY_VALIDATED")
            ready[role] = self._verified_ready(role, slot, entry["release"], receipt)
            if set(ready) == set(ROLES):
                entry["phase"] = "READY"
                self._verify_targets(state, {name: slot for name in ROLES})
            self.store.save(state)
            return state

    def switch_api(self, slot, baseline_receipt):
        require(slot in SLOTS, "UNKNOWN_SLOT")
        with self._locked():
            state = self.store.load()
            effective, etag = self._current(state)
            active = state["routing"]["active"]
            require(active["api"] == active["frontend"] != slot and state["rollback"] is None,
                    "API_SWITCH_NOT_ALLOWED")
            entry = state["slots"][slot]
            require(entry is not None and entry["phase"] == "READY", "SLOT_NOT_READY")
            context = {
                "slot": slot, "prior_slot": active["api"], "previous_generation": effective.generation,
                "identity_hash": entry["ready"]["api"]["identity_hash"], "release": entry["release"],
            }
            baseline_hash = self.player_gate.capture_baseline(baseline_receipt, context)
            require(baseline_hash is not None, "PLAYER_BASELINE_RECEIPT_REJECTED")
            return self._switch(state, effective, etag, "API", {**active, "api": slot},
                                baseline_hash=baseline_hash)

    def switch_frontend(self, slot, receipt):
        require(slot in SLOTS, "UNKNOWN_SLOT")
        with self._locked():
            state = self.store.load()
            effective, etag = self._current(state)
            active = state["routing"]["active"]
            transition = state["routing"]["api_transition"]
            require(active["api"] == slot != active["frontend"] and transition is not None,
                    "FRONTEND_SWITCH_NOT_ALLOWED")
            if self.player_gate(receipt, transition) is not True:
                self._rollback(state, effective, etag)
                raise Stop("PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK")
            return self._switch(state, effective, etag, "FRONTEND", {**active, "frontend": slot},
                                player_gate_hash=digest(receipt))

    def rollback(self):
        with self._locked():
            state = self.store.load()
            effective, etag = self._current(state)
            return self._rollback(state, effective, etag)

    def _rollback(self, state, effective, etag):
        require(state["rollback"] is not None, "NO_ROLLBACK_TARGET")
        return self._switch(state, effective, etag, "ROLLBACK", state["rollback"])

    def retire(self, slot):
        require(slot in SLOTS, "UNKNOWN_SLOT")
        with self._locked():
            state = self.store.load()
            self._current(state)
            active = state["routing"]["active"]
            require(active["api"] == active["frontend"] != slot, "SLOT_PROTECTED")
            entry = state["slots"][slot]
            require(entry is not None and entry["phase"] in ("STANDBY", "FAILED"),
                    "SLOT_NOT_RETIRABLE")
            self._verify_targets(state, active)
            require(slot != "BLUE" or "legacy_blue" not in state, "LEGACY_BLUE_RETIREMENT_REQUIRES_REBIND")
            entry["phase"] = "RETIRED"
            entry["ready"] = {}
            state["rollback"] = None
            state["routing"]["api_transition"] = None
            self.store.save(state)
            return state

    def _switch(self, state, effective, etag, operation, target, baseline_hash=None, player_gate_hash=None):
        self._verify_promotion(state, operation, target)
        generation = len(state["history"]) + 1
        updated = effective.changed(target, generation)
        self.store.seal_config(updated.config)
        release = state["slots"][target["api"]]["release"]
        intent = {
            "generation": generation, "previous_generation": effective.generation, "operation": operation,
            "from_slot": effective.active.copy(), "to_slot": target.copy(), "timestamp": now(),
            "release": copy.deepcopy(release),
            "expected_etag": etag, "previous_hash": effective.hash, "next_hash": updated.hash,
            "baseline_hash": baseline_hash, "player_gate_hash": player_gate_hash,
        }
        intent["intent_hash"] = digest(intent)
        state["pending"] = {"stage": "PREPARED", "intent": intent}
        self.store.save(state)
        self._verify_promotion(state, operation, target)
        state["pending"]["stage"] = "DISPATCHED"
        self.store.save(state)
        status = self.edge.patch(updated.config, etag)
        if status == 412:
            state["pending"]["stage"] = "REJECTED"
            self.store.save(state)
            raise Stop("EDGE_ETAG_COLLISION")
        require(200 <= status < 300, "EDGE_MUTATION_REJECTED_RECONCILE")
        return self._reconcile(state, must_commit=True)["state"]

    def _record(self, state, result):
        intent = state["pending"]["intent"]
        stamp = now()
        if result in ("COMMITTED", "COMMITTED_FAILED"):
            routing = state["routing"]
            routing["generation"] = intent["generation"]
            routing["active"] = intent["to_slot"]
            routing["config_hash"] = intent["next_hash"]
            previous = intent["from_slot"]
            target = intent["to_slot"]
            if intent["operation"] == "API":
                state["rollback"] = previous
                candidate = state["slots"][target["api"]]
                routing["api_transition"] = {
                    "slot": target["api"], "prior_slot": previous["api"],
                    "generation": intent["generation"], "switched_at": stamp,
                    "release": intent["release"], "identity_hash": candidate["ready"]["api"]["identity_hash"],
                    "baseline_hash": intent["baseline_hash"],
                }
                candidate["phase"] = "FAILED" if result == "COMMITTED_FAILED" else "ACTIVE"
            if intent["operation"] == "FRONTEND":
                state["slots"][target["frontend"]]["phase"] = (
                    "FAILED" if result == "COMMITTED_FAILED" else "ACTIVE"
                )
                prior = state["slots"][previous["frontend"]]
                if prior["phase"] != "FAILED":
                    prior["phase"] = "STANDBY"
            if intent["operation"] == "ROLLBACK":
                state["rollback"] = None
                routing["api_transition"] = None
                state["slots"][target["api"]]["phase"] = (
                    "FAILED" if result == "COMMITTED_FAILED" else "ACTIVE"
                )
                state["slots"][previous["api"]]["phase"] = "FAILED"
        state["history"].append({"intent": intent, "result": result, "recorded_at": stamp})
        state["pending"] = None
        self.store.save(state)

    def reconcile(self):
        with self._locked():
            return self._reconcile(self.store.load())

    def _reconcile(self, state, must_commit=False):
        if state["pending"] is None:
            self._current(state)
            return {"result": "IN_SYNC", "state": state}
        pending = state["pending"]
        intent = pending["intent"]
        effective, _ = self.edge.effective()
        persisted = self.edge.persisted()
        require(effective.hash == persisted.hash, "STOP_AMBIGUOUS")
        if (pending["stage"] == "DISPATCHED" and effective.hash == intent["next_hash"]
                and effective.generation == intent["generation"] and effective.active == intent["to_slot"]):
            try:
                self._verify_promotion(state, intent["operation"], intent["to_slot"])
            except Stop:
                # Persist the proven effective routes, without calling another mutation.
                self._record(state, "COMMITTED_FAILED")
                raise Stop("SWITCH_IDENTITY_CHECK_FAILED")
            self._record(state, "COMMITTED")
            return {"result": "COMMIT", "state": state}
        if (not must_commit and pending["stage"] in ("PREPARED", "REJECTED")
                and effective.hash == intent["previous_hash"]
                and effective.generation == intent["previous_generation"]
                and effective.active == intent["from_slot"]):
            self._record(state, "ROLLED_BACK")
            return {"result": "ROLLBACK", "state": state}
        # Old/old after a dispatched transport failure is still ambiguous. The
        # disconnected client cannot prove the server will never apply its PATCH.
        raise Stop("STOP_AMBIGUOUS")


def _json(path):
    return decode(read_private(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--autosave", required=True)
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--edge-name", required=True)
    parser.add_argument("--legacy-blue-manifest")
    parser.add_argument("--legacy-blue-sha256")
    subparsers = parser.add_subparsers(dest="action", required=True)
    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("release")
    initialize.add_argument("receipts")
    initialize.add_argument("baseline_hash")
    initialize.add_argument("--sealed-config", required=True)
    subparsers.add_parser("preview").add_argument("sealed_config")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("slot", choices=SLOTS)
    prepare.add_argument("release")
    for action in ("start", "invalidate", "retire"):
        subparsers.add_parser(action).add_argument("slot", choices=SLOTS)
    api = subparsers.add_parser("switch-api")
    api.add_argument("slot", choices=SLOTS)
    api.add_argument("baseline_receipt")
    ready = subparsers.add_parser("ready")
    ready.add_argument("role", choices=ROLES)
    ready.add_argument("slot", choices=SLOTS)
    ready.add_argument("receipt")
    frontend = subparsers.add_parser("switch-frontend")
    frontend.add_argument("slot", choices=SLOTS)
    frontend.add_argument("receipt")
    for action in ("status", "reconcile", "rollback", "recover-ingress", "supervise-ingress", "close-ingress"):
        subparsers.add_parser(action)
    args = parser.parse_args()
    try:
        from edge_boot import ColdStart
        from gates import DockerProbe, PlayerGate, ReadyGate

        key = read_private(args.key)
        require(bool(args.legacy_blue_manifest) == bool(args.legacy_blue_sha256), "LEGACY_MANIFEST_PIN_REQUIRED")
        controller = Controller(
            StateStore(args.state_dir), CaddyEdge(args.socket, args.autosave),
            DockerProbe(_json(args.bindings), edge_name=args.edge_name,
                        legacy_blue=_json(args.legacy_blue_manifest) if args.legacy_blue_manifest else None,
                        legacy_blue_sha256=args.legacy_blue_sha256), ReadyGate(key), PlayerGate(key),
        )
        action = args.action
        if action == "initialize":
            result = ColdStart(controller).initialize(_json(args.sealed_config), _json(args.release),
                                                      _json(args.receipts), args.baseline_hash)
        elif action == "preview":
            result = ColdStart(controller).preview(_json(args.sealed_config))
        elif action == "recover-ingress":
            result = ColdStart(controller).recover()
        elif action == "close-ingress":
            result = ColdStart(controller).close()
        elif action == "supervise-ingress":
            ColdStart(controller).supervise()
            return 0
        elif action == "prepare":
            result = controller.prepare(args.slot, _json(args.release))
        elif action == "ready":
            result = controller.register_ready(args.role, args.slot, _json(args.receipt))
        elif action == "switch-frontend":
            result = controller.switch_frontend(args.slot, _json(args.receipt))
        elif action == "switch-api":
            result = controller.switch_api(args.slot, _json(args.baseline_receipt))
        elif hasattr(args, "slot"):
            result = getattr(controller, action.replace("-", "_"))(args.slot)
        else:
            result = getattr(controller, action)()
        print(json.dumps(result, indent=2, sort_keys=True))
    except (Stop, OSError, ValueError) as exc:
        # Never expose exception bodies from Docker, the admin socket or input files.
        print(str(exc) if isinstance(exc, Stop) else "ROUTECTL_LOCAL_IO_OR_INPUT_FAILED", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
