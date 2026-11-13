"""Recover the committed routing generation without admitting public requests."""

import copy
import re
import sys
from pathlib import Path
from time import monotonic, sleep

from release_state import ROLES, Stop, decode, require, validate_hash, validate_release
from routectl import ADMIN_ADDRESS, BLUE_PAIR, RouteConfig


BOOTSTRAP = decode(Path(__file__).with_name("edge-bootstrap.json").read_bytes())
require(BOOTSTRAP == {"admin": {"config": {"persist": False}, "listen": ADMIN_ADDRESS}},
        "UNSAFE_EDGE_BOOTSTRAP")

INGRESS_GUARD = {
    "match": [{"not": [{"file": {"root": "/run/bgv2", "try_files": ["/ingress-ready"]}}]}],
    "handle": [{"handler": "static_response", "status_code": 503}],
    "terminal": True,
}


def validate_ingress_guard(config):
    for name in ("public", "redirect"):
        require(config["apps"]["http"]["servers"][name]["routes"][0] == INGRESS_GUARD,
                "EDGE_INGRESS_GUARD_MISSING")


def gated_config(config):
    result = copy.deepcopy(config)
    for name in ("public", "redirect"):
        result["apps"]["http"]["servers"][name]["routes"].insert(0, copy.deepcopy(INGRESS_GUARD))
    return result


def preview_config(config):
    result = copy.deepcopy(config)
    result["admin"]["config"] = {"persist": False}
    http = result["apps"]["http"]
    for name in ("public", "redirect"):
        del http["servers"][name]
    # The initial legacy handoff starts Edge while the historical ingress
    # still owns the shared certificate store. Private previews must not
    # acquire, renew or write any public certificate in that interval.
    if RouteConfig(config).targets["api", "BLUE"] != "backend-blue:8000":
        result["storage"] = {"module": "file_system", "root": "/config/preview-tls"}
        result["apps"]["pki"] = {"certificate_authorities": {"local": {"install_trust": False}}}
        result["apps"]["tls"] = {"automation": {"policies": [{"issuers": [{"module": "internal"}]}]}}
    return result


class ColdStart:
    def __init__(self, controller):
        self.controller = controller
        self.edge = controller.edge
        self.store = controller.store

    def _config(self, config):
        result = RouteConfig(config, self.edge.hosts)
        validate_ingress_guard(result.config)
        return result

    def _baseline(self, config):
        baseline = self._config(config)
        require(baseline.generation == 0 and baseline.active == BLUE_PAIR, "BASELINE_CONFIG_MISMATCH")
        return baseline

    def _load(self, config, etag):
        status = self.edge.patch(config, etag)
        require(status != 412, "EDGE_ETAG_COLLISION")
        require(200 <= status < 300, "EDGE_BOOT_LOAD_REJECTED")
        effective, new_etag = self.edge.raw_effective()
        require(effective == config, "EDGE_BOOT_EFFECTIVE_MISMATCH")
        return new_etag

    def preview(self, config):
        baseline = self._baseline(config)
        with self.controller._locked():
            require(self.store.load(allow_missing=True) is None, "ALREADY_INITIALIZED")
            self.edge.close_ingress()
            effective, etag = self.edge.raw_effective()
            staged = preview_config(baseline.config)
            require(effective in (BOOTSTRAP, staged), "STOP_AMBIGUOUS")
            if effective != staged:
                self._load(staged, etag)
            return {"ingress_ready": False, "preview": "BLUE", "baseline_hash": baseline.hash}

    def initialize(self, config, release, receipts_by_role, baseline_hash):
        baseline = self._baseline(config)
        validate_release(release)
        validate_hash(baseline_hash)
        require(baseline.hash == baseline_hash, "BASELINE_CONFIG_MISMATCH")
        require(isinstance(receipts_by_role, dict) and set(receipts_by_role) == set(ROLES),
                "READY_RECEIPTS_REQUIRED")
        with self.controller._locked():
            require(self.store.load(allow_missing=True) is None, "ALREADY_INITIALIZED")
            self.edge.close_ingress()
            effective, _ = self.edge.raw_effective()
            require(effective == preview_config(config), "EDGE_PRIVATE_PREVIEW_REQUIRED")
            state = self.controller._initial_state(release, receipts_by_role, baseline.hash)
            self.controller._verify_route_bindings(state, baseline)
            self.controller._verify_targets(state, BLUE_PAIR, route_config=baseline)
            self.store.seal_config(baseline.config)
            self.store.save(state)
            return state

    def _read_autosave(self):
        try:
            raw = self.edge.read_autosave()
        except FileNotFoundError:
            return None
        try:
            saved = decode(raw)
        except Stop as exc:
            require(str(exc) in ("INVALID_JSON", "DUPLICATE_JSON_KEY", "INVALID_JSON_CONSTANT"),
                    "STOP_AMBIGUOUS")
            return None
        try:
            return self._config(saved)
        except (Stop, KeyError, IndexError, TypeError):
            # Parseable but unknown config is evidence, not a missing file.
            raise Stop("STOP_AMBIGUOUS") from None

    def _committed(self, state):
        routing = state["routing"]
        config = self._config(self.store.load_config(routing["config_hash"]))
        require(config.generation == routing["generation"] and config.active == routing["active"],
                "STOP_AMBIGUOUS")
        self.controller._require_legacy_binding(state, config)
        self.controller._verify_route_bindings(state, config)
        return config

    def _reconcile_journal(self, state, committed, saved):
        pending = state["pending"]
        if pending is None:
            if saved is None or saved.hash == committed.hash:
                return "COMMITTED"
            ancestors = {item["intent"]["previous_hash"] for item in state["history"]}
            require(saved.generation < committed.generation and saved.hash in ancestors
                    and self.store.load_config(saved.hash) == saved.config, "STOP_AMBIGUOUS")
            return "RESTORED_COMMITTED"
        intent = pending["intent"]
        if pending["stage"] in ("PREPARED", "REJECTED"):
            require(saved is None or saved.hash == committed.hash, "STOP_AMBIGUOUS")
            self.controller._record(state, "ROLLED_BACK")
            return "ROLLBACK"
        require(pending["stage"] == "DISPATCHED" and saved is not None and saved.hash == intent["next_hash"]
                and saved.generation == intent["generation"] and saved.active == intent["to_slot"]
                and self.store.load_config(saved.hash) == saved.config, "STOP_AMBIGUOUS")
        try:
            self.controller._verify_promotion(state, intent["operation"], saved.active)
            self.controller._verify_route_bindings(state, saved)
        except Stop:
            self.controller._record(state, "COMMITTED_FAILED")
            raise Stop("SWITCH_IDENTITY_CHECK_FAILED") from None
        self.controller._record(state, "COMMITTED")
        return "COMMIT"

    def recover(self, expected_boot=None):
        with self.controller._locked():
            if expected_boot is not None:
                require(self.edge.boot_token() == expected_boot, "EDGE_BOOT_CHANGED")
            self.edge.close_ingress()
            state = self.store.load()
            committed = self._committed(state)
            saved = self._read_autosave()
            legacy = state.get("legacy_blue")
            if legacy is not None and state["pending"] is not None:
                require(self.controller.probe.legacy_engine_boot_id() == legacy["runtime_epochs"]["api"]["boot_id"],
                        "LEGACY_BLUE_REATTEST_SCOPE_NOT_SAFE")
            outcome = self._reconcile_journal(state, committed, saved)
            desired = self._committed(state)
            effective, etag = self.edge.raw_effective()
            staged = preview_config(committed.config)
            expected = (BOOTSTRAP, staged, committed.config, desired.config)
            require(effective in expected, "STOP_AMBIGUOUS")
            reattestation = self.controller.reattest_legacy_blue_after_host_reboot(state)
            self.controller._verify_targets(state, desired.active, route_config=desired)
            if legacy is not None and effective == staged:
                # Retire the scratch CA and its in-memory certs before loading
                # the same SNI names from historical storage. Admission stays closed.
                etag = self._load(BOOTSTRAP, etag)
                effective = BOOTSTRAP
            if effective != desired.config:
                self._load(desired.config, etag)
            require(self.edge.persisted().hash == desired.hash, "EDGE_BOOT_AUTOSAVE_MISMATCH")
            require(self.store.load() == state, "EDGE_BOOT_STATE_CHANGED")
            self.edge.open_ingress(desired.hash)
            result = {"ingress_ready": True, "generation": desired.generation, "result": outcome}
            if reattestation is not None:
                result["legacy_blue_reattest"] = reattestation
            return result

    def close(self):
        with self.controller._locked():
            self.edge.close_ingress()
        return {"ingress_ready": False}

    def monitor_legacy_blue(self):
        """Close an admitted legacy pair if Docker restarts it on the same boot."""
        with self.controller._locked():
            if not self.edge.ingress_open():
                return
            try:
                state = self.store.load()
                if state["routing"]["active"] != BLUE_PAIR:
                    return
                legacy = state["legacy_blue"]
                self.controller.probe.verify_legacy_runtime_running(state["slots"]["BLUE"]["ready"],
                                                                    legacy["runtime_epochs"]["api"])
            except Stop as exc:
                self.edge.close_ingress()
                if str(exc) == "SLOT_READ_ONLY_INSPECTION_FAILED":
                    raise Stop("STOP_REATTEST_REQUIRED") from None
                raise
            except (OSError, KeyError):
                self.edge.close_ingress()
                raise

    def supervise(self):
        """Run on the host across Edge process boots, never admitting an uncertain generation."""
        seen = None
        while True:
            try:
                boot = self.edge.boot_token()
                require(re.fullmatch(rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n", boot),
                        "INVALID_EDGE_BOOT_ID")
            except (OSError, Stop):
                sleep(1)
                continue
            if boot != seen:
                seen = boot
                try:
                    self._supervise_boot(boot)
                except Stop as exc:
                    print(f"INGRESS_READY=NO {exc}", file=sys.stderr, flush=True)
                except OSError:
                    print("INGRESS_READY=NO EDGE_BOOT_IO_FAILED", file=sys.stderr, flush=True)
            if getattr(self.controller.probe, "legacy_blue_endpoint", None) is not None:
                try:
                    self.monitor_legacy_blue()
                except Stop as exc:
                    if str(exc) != "ROUTECTL_WRITER_LOCKED":
                        print(f"INGRESS_READY=NO {exc}", file=sys.stderr, flush=True)
                except (OSError, KeyError):
                    print("INGRESS_READY=NO LEGACY_BLUE_MONITOR_FAILED", file=sys.stderr, flush=True)
            sleep(1)

    def _supervise_boot(self, boot):
        admin_deadline = monotonic() + 30
        recovery_deadline = monotonic() + 120
        while monotonic() < recovery_deadline and self.edge.boot_token() == boot:
            try:
                self.edge.raw_effective()
            except Stop as exc:
                require(str(exc) == "EDGE_READ_FAILED", str(exc))
                require(monotonic() < admin_deadline, "EDGE_ADMIN_START_TIMEOUT")
                sleep(0.5)
                continue
            # A service restarted after verified admission does not interrupt
            # normal runtime routing. A new Edge boot removes this marker.
            if self.edge.ingress_open():
                return
            try:
                result = self.recover(expected_boot=boot)
            except Stop as exc:
                if str(exc) not in ("ROUTECTL_WRITER_LOCKED", "LEGACY_BLUE_REBOOT_SERVICES_STARTING"):
                    raise
                sleep(0.5)
                continue
            reattestation = result.get("legacy_blue_reattest")
            event = f" {reattestation}" if reattestation is not None else ""
            print(f"INGRESS_READY=YES GENERATION={result['generation']}{event}", flush=True)
            return
        raise Stop("EDGE_BOOT_CHANGED" if self.edge.boot_token() != boot else "EDGE_CLOSED_RECOVERY_TIMEOUT")
