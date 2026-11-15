"""Prepare the initial legacy handoff without opening ingress."""

from copy import deepcopy
from hashlib import sha256
import re

from edge_boot import BOOTSTRAP, ColdStart, preview_config
from release_state import ROLES, decode, digest, now, read_private, require, validate_hash
from routectl import BLUE_PAIR


def _ignore_event(_name):
    pass


def _autosave_bytes(edge):
    try:
        return edge.read_autosave()
    except FileNotFoundError:
        return None


def _bytes_sha(raw):
    return sha256(raw).hexdigest() if raw is not None else None


class PreparedHandoff:
    """Use the existing state, Edge writer lock, and CAS for initial admission."""

    def __init__(self, controller, topology):
        self.controller = controller
        self.topology = topology
        self.cold = ColdStart(controller)
        self.edge = controller.edge
        self.store = controller.store

    def prepare(self, *, mark=_ignore_event):
        mark("T_PREPARE_BEGIN")
        with self.controller._locked():
            state = self.store.load()
            require(state.get("legacy_blue") is not None
                    and state["routing"]["generation"] == 0 and state["routing"]["active"] == BLUE_PAIR
                    and state["pending"] is None and state["slots"]["BLUE"]["phase"] == "ACTIVE",
                    "HANDOFF_INITIAL_BLUE_REQUIRED")
            raw_state = read_private(self.store.path)
            # Bind the exact on-disk envelope to the validated state used below.
            require(decode(raw_state) == {"sha256": digest(state), "state": state}, "HANDOFF_STATE_CHANGED")
            committed = self.cold._committed(state)
            self.edge.require_closed()
            snapshot = self.topology.snapshot()
            effective, etag = self.edge.raw_effective()
            require(effective == preview_config(committed.config), "HANDOFF_PRIVATE_PREVIEW_REQUIRED")
            autosave = _autosave_bytes(self.edge)
            saved = self.cold._parse_autosave(autosave)
            require(saved is None or saved.hash == committed.hash, "HANDOFF_AUTOSAVE_DRIFT")
            if self.controller.probe.legacy_frontend_blue_sha256 is not None:
                self.controller.probe.verify_initial_legacy_frontend_source()
            self.controller._verify_targets(state, BLUE_PAIR, route_config=committed)
            self.controller.probe.verify_handoff_sources(state, snapshot)
            require(self.topology.snapshot() == snapshot, "HANDOFF_RUNTIME_DRIFT_DURING_PREPARE")
            require(read_private(self.store.path) == raw_state and self.edge.raw_effective() == (effective, etag)
                    and _autosave_bytes(self.edge) == autosave, "HANDOFF_INPUT_DRIFT_DURING_PREPARE")
            self.edge.require_closed()
            payload = {
                "status": "HANDOFF_PREPARED",
                "prepared_at": now(),
                "prepared_generation": committed.generation,
                "prepared_config_sha256": committed.hash,
                "prepared_expected_etag": etag,
                "prepared_state_sha256": _bytes_sha(raw_state),
                "prepared_blue_identities": {
                    role: state["slots"]["BLUE"]["ready"][role]["identity"] for role in ROLES
                },
                "private_config": effective,
                "autosave_sha256": _bytes_sha(autosave),
                "config_payload": committed.config,
                "bootstrap_payload": deepcopy(BOOTSTRAP),
                "bootstrap_sha256": digest(BOOTSTRAP),
                # The existing rollback restores this historical ingress/peer,
                # preserving the same endpoint intention and TLS ownership.
                "rollback_payload": snapshot,
                "rollback_sha256": digest(snapshot),
            }
            receipt = {"sha256": digest(payload), "payload": payload}
        mark("T_PREPARE_COMPLETE")
        return receipt

    def _revalidate(self, prepared, expected_sha256):
        validate_hash(expected_sha256)
        require(type(prepared) is dict and set(prepared) == {"sha256", "payload"}
                and prepared["sha256"] == expected_sha256 and digest(prepared["payload"]) == expected_sha256,
                "INVALIDATE_HANDOFF_PREPARED_CHECKSUM")
        plan = prepared["payload"]
        require(type(plan) is dict and plan.get("status") == "HANDOFF_PREPARED"
                and type(plan.get("prepared_generation")) is int and plan["prepared_generation"] == 0
                and digest(plan.get("config_payload")) == plan.get("prepared_config_sha256")
                and plan.get("bootstrap_payload") == BOOTSTRAP
                and plan.get("bootstrap_sha256") == digest(BOOTSTRAP)
                and digest(plan.get("rollback_payload")) == plan.get("rollback_sha256")
                and isinstance(plan.get("prepared_expected_etag"), str)
                and re.fullmatch(r'"/config/ [0-9a-f]{16}"', plan["prepared_expected_etag"]),
                "INVALIDATE_HANDOFF_PREPARED_PAYLOAD")
        state_bytes = read_private(self.store.path)
        require(_bytes_sha(state_bytes) == plan.get("prepared_state_sha256"),
                "INVALIDATE_HANDOFF_PREPARED_STATE")
        envelope = decode(state_bytes)
        state = envelope["state"]
        require(state["pending"] is None and state["routing"]["generation"] == plan["prepared_generation"]
                and state["routing"]["active"] == BLUE_PAIR
                and state["routing"]["config_hash"] == plan["prepared_config_sha256"]
                and state["slots"]["BLUE"]["phase"] == "ACTIVE"
                and {role: state["slots"]["BLUE"]["ready"][role]["identity"] for role in ROLES}
                == plan["prepared_blue_identities"], "INVALIDATE_HANDOFF_PREPARED_STATE")
        require(self.store.load_config(plan["prepared_config_sha256"]) == plan["config_payload"],
                "INVALIDATE_HANDOFF_PREPARED_SEAL")
        require(_bytes_sha(_autosave_bytes(self.edge)) == plan["autosave_sha256"],
                "INVALIDATE_HANDOFF_PREPARED_AUTOSAVE")
        self.edge.require_closed()
        expected_topology = plan["rollback_payload"]
        self.controller.probe.verify_handoff_sources(state, expected_topology)
        require(self.topology.snapshot() == expected_topology,
                "INVALIDATE_HANDOFF_PREPARED_TOPOLOGY")
        effective, etag = self.edge.raw_effective()
        require(etag == plan["prepared_expected_etag"] and effective == plan["private_config"],
                "INVALIDATE_HANDOFF_PREPARED_EDGE_CONFIG")
        require(read_private(self.store.path) == state_bytes, "INVALIDATE_HANDOFF_PREPARED_STATE")
        self.edge.require_closed()
        return plan, state_bytes

    def execute(self, prepared, *, expected_sha256, move, mark=_ignore_event, ensure_budget=None):
        mark("T_FINAL_REVALIDATION_BEGIN")
        # Keep both existing writer locks until ingress has been verified and
        # opened. The transfer callback runs only after every stale-state check.
        with self.controller._locked():
            plan, state_bytes = self._revalidate(prepared, expected_sha256)
            mark("T_FINAL_REVALIDATION_COMPLETE")
            move()
            self.topology.verify_moved(plan["rollback_payload"])
            self.edge.require_closed()
            require(read_private(self.store.path) == state_bytes, "HANDOFF_COMMITTED_STATE_CHANGED")
            if ensure_budget is not None:
                ensure_budget()
            mark("T_CONFIG_PATCH_BEGIN")
            # Preserve both CAS steps: retire the private preview's scratch CA,
            # then load the precomputed public payload using the returned ETag.
            etag = self.cold._load(plan["bootstrap_payload"], plan["prepared_expected_etag"])
            if ensure_budget is not None:
                ensure_budget()
            self.cold._load(plan["config_payload"], etag, on_patch=lambda: mark("T_CONFIG_PATCH_COMPLETE"))
            mark("T_EFFECTIVE_CONFIG_VERIFIED")
            require(read_private(self.store.path) == state_bytes, "HANDOFF_COMMITTED_STATE_CHANGED")
            if ensure_budget is None:
                self.edge.open_ingress(plan["prepared_config_sha256"])
            else:
                self.edge.open_ingress(plan["prepared_config_sha256"], before_write=ensure_budget)
            mark("T_INGRESS_READY")
            return {"ingress_ready": True, "generation": plan["prepared_generation"], "result": "COMMITTED"}
