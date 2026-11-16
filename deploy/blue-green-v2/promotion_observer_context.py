"""Bind a Player observation to a verified, committed API transition."""

from dataclasses import dataclass
from pathlib import Path

from release_state import SLOTS, Stop, decode, digest, read_private, require


def committed_routing_context(state, effective):
    """Project the exact routing proof already validated by the controller."""
    route = state["routing"]
    transition = route["api_transition"]
    require(state["pending"] is None and transition is not None,
            "OBSERVER_REQUIRES_API_ONLY_SWITCH")
    slot, prior = transition["slot"], transition["prior_slot"]
    generation = route["generation"]
    require(slot in SLOTS and prior in SLOTS and slot != prior and type(generation) is int and generation > 0
            and generation == transition["generation"] == effective.generation
            and route["active"] == effective.active == {"api": slot, "frontend": prior}
            and state["rollback"] == {"api": prior, "frontend": prior}
            and route["config_hash"] == effective.hash, "OBSERVER_REQUIRES_API_ONLY_SWITCH")
    latest = state["history"][-1]
    intent = latest["intent"]
    require(latest["result"] == "COMMITTED" and intent["operation"] == "API"
            and intent["generation"] == generation and intent["to_slot"] == route["active"],
            "OBSERVER_API_TRANSITION_NOT_COMMITTED")
    entry = state["slots"][slot]
    ready = entry["ready"]["api"]
    identity = ready["identity"]
    upstream = effective.target_for("api", slot)
    prior_upstream = effective.target_for("api", prior)
    require(entry["phase"] == "ACTIVE" and entry["release"] == transition["release"]
            and ready["identity_hash"] == transition["identity_hash"] == digest(identity)
            and identity["upstream"] == upstream and upstream != prior_upstream
            and state["slots"][prior]["ready"]["api"]["identity"]["upstream"] == prior_upstream,
            "OBSERVER_COMMITTED_READY_BINDING_MISMATCH")
    return {"generation": generation, "slot": slot, "upstream": upstream, "prior_slot": prior,
            "prior_upstream": prior_upstream, "backend_container": identity["container_id"],
            "release": entry["release"], "identity_hash": ready["identity_hash"],
            "config_hash": route["config_hash"], "state_sha256": digest(state)}


def file_epoch(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class FrozenPromotion:
    controller: object
    bindings_path: Path
    state: dict
    context: dict
    state_epoch: tuple
    bindings_bytes: bytes
    edge_identity: tuple
    etag: str

    @classmethod
    def capture(cls, controller, bindings_path, expected_state_hash):
        state_epoch = file_epoch(controller.store.path)
        bindings_bytes = read_private(bindings_path)
        require(decode(bindings_bytes) == controller.probe.bindings, "OBSERVER_COMMITTED_READY_BINDING_MISMATCH")
        report = controller.status_report()
        require(report["STATUS"] == "PASS", "OBSERVER_ROUTING_STATUS_NOT_PASS")
        state = report["state"]
        require(report["STATE_SHA256"] == expected_state_hash == digest(state),
                "OBSERVER_ROUTING_CONTEXT_CHANGED")
        effective, etag = controller.edge.effective()
        context = committed_routing_context(state, effective)
        edge_identity = controller.edge.status_identity()
        require(edge_identity[1] is not None and controller.edge.persisted(synchronize=False).hash == context["config_hash"],
                "OBSERVER_ROUTING_STATUS_NOT_PASS")
        frozen = cls(controller, Path(bindings_path), state, context, state_epoch, bindings_bytes, edge_identity, etag)
        frozen.check_state()
        return frozen

    def check_state(self):
        try:
            before = file_epoch(self.controller.store.path)
            valid = (before == self.state_epoch and self.controller.store.load() == self.state
                     and read_private(self.bindings_path) == self.bindings_bytes
                     and file_epoch(self.controller.store.path) == before)
        except (OSError, Stop, ValueError):
            valid = False
        require(valid, "OBSERVER_ROUTING_CONTEXT_CHANGED")

    def recheck(self):
        self.check_state()
        report = self.controller.status_report()
        require(report["STATUS"] == "PASS" and report["state"] == self.state,
                "OBSERVER_ROUTING_CONTEXT_CHANGED")
        effective, etag = self.controller.edge.effective()
        require(etag == self.etag and self.controller.edge.status_identity() == self.edge_identity
                and committed_routing_context(self.state, effective) == self.context
                and self.controller.edge.persisted(synchronize=False).hash == self.context["config_hash"],
                "OBSERVER_ROUTING_CONTEXT_CHANGED")
        self.check_state()
