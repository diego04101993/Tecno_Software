"""Validate mixed journals without changing legacy routing history bytes."""

from copy import deepcopy
import unittest

from release_state import (
    EDGE_CONFIG_ROLLBACK, EDGE_CONFIG_UPDATE, Stop, canonical, digest, edge_config_revision,
    edge_config_rollback_target, next_edge_config_revision, next_routing_generation, now,
    pending_event_type, validate_state,
)
import test_routectl


def hashed(record, field):
    record[field] = digest({name: value for name, value in record.items() if name != field})
    return record


def config_event(state, target_hash, operation=EDGE_CONFIG_UPDATE, revision=None):
    route = state["routing"]
    if revision is None:
        revision = next_edge_config_revision(state)
    last = state["history"][-1] if state["history"] else None
    old_hash = route["config_hash"]
    return hashed({
        "type": operation,
        "reason": "TELEMETRY",
        "previous_revision": edge_config_revision(state),
        "new_revision": revision,
        "routing_generation": route["generation"],
        "active": route["active"].copy(),
        "previous_hash": old_hash,
        "next_hash": target_hash,
        "previous_autosave_hash": old_hash,
        "next_autosave_hash": target_hash,
        "expected_etag": '"/config/ 0000000000000000"',
        "payload_sha256": target_hash,
        "timestamp": now(),
        "previous_record_hash": digest(last) if last else None,
        "edge_boot_id": "b5d67c19-b673-493b-94c5-38f6e091fd78",
    }, "event_hash")


def settle(state, event, result="COMMITTED"):
    state["history"].append({"event": event, "result": result, "recorded_at": now()})
    if result == "COMMITTED":
        state["routing"]["config_hash"] = event["next_hash"]
        state["edge_config_revision"] = event["new_revision"]
    else:
        state["edge_config_revision"] = edge_config_revision(state)
    state["pending"] = None


class EdgeConfigLedgerTests(unittest.TestCase):
    def setUp(self):
        fixture = test_routectl.RouteControllerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        fixture.controller.switch_api("GREEN", "fixture-baseline")
        fixture.controller.rollback()
        self.state = self.store.load()
        self.assertEqual(self.state["routing"]["generation"], 2)
        self.assertEqual(self.state["routing"]["active"], {"api": "BLUE", "frontend": "BLUE"})

    def update(self):
        event = config_event(self.state, "c" * 64)
        settle(self.state, event)
        validate_state(self.state)
        return event

    def route_attempt(self):
        intent = deepcopy(self.state["history"][0]["intent"])
        intent.update(generation=next_routing_generation(self.state),
                      previous_generation=self.state["routing"]["generation"],
                      previous_hash=self.state["routing"]["config_hash"], next_hash="d" * 64,
                      previous_record_hash=digest(self.state["history"][-1]))
        return hashed(intent, "intent_hash")

    def test_valid_legacy_state_adopts_zero_without_rewriting_disk(self):
        before = self.store.path.read_bytes()
        legacy = self.store.load()
        self.assertNotIn("edge_config_revision", legacy)
        self.assertEqual(edge_config_revision(legacy), 0)
        self.assertEqual(next_edge_config_revision(legacy), 1)
        self.assertEqual(next_routing_generation(legacy), 3)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_pending_first_update_keeps_revision_implicit_and_routes_unchanged(self):
        state = self.state
        event = config_event(state, "c" * 64)
        pending = {"stage": "PREPARED", "event": event}
        state["pending"] = pending
        self.store.save(state)
        state = self.store.load()
        self.assertEqual(state["pending"], pending)
        self.assertEqual(pending_event_type(state), EDGE_CONFIG_UPDATE)
        self.assertNotIn("edge_config_revision", state)
        self.assertEqual(state["routing"]["generation"], 2)

    def test_first_config_update_appends_without_rewriting_routing_history(self):
        state = self.state
        before = deepcopy(state)
        event = self.update()
        self.store.save(state)
        self.assertEqual(canonical(self.store.load()["history"][:-1]), canonical(before["history"]))
        self.assertEqual(state["slots"], before["slots"])
        self.assertEqual(state["rollback"], before["rollback"])
        self.assertEqual(state["routing"], {**before["routing"], "config_hash": event["next_hash"]})
        self.assertEqual(edge_config_revision(state), 1)
        self.assertEqual(next_routing_generation(state), 3)
        self.assertIsNone(pending_event_type(state))

    def test_revision_rollback_restores_old_hash_without_reusing_issued_revision(self):
        state = self.state
        old_hash = state["routing"]["config_hash"]
        origin = self.update()
        self.assertEqual(edge_config_rollback_target(state), origin)
        revert = config_event(state, old_hash, EDGE_CONFIG_ROLLBACK, revision=0)
        settle(state, revert)
        validate_state(state)
        self.assertEqual(state["edge_config_revision"], 0)
        self.assertEqual(state["routing"]["generation"], 2)
        self.assertIsNone(edge_config_rollback_target(state))
        self.assertEqual(next_edge_config_revision(state), 2)
        settle(state, config_event(state, "c" * 64))
        validate_state(state)
        self.assertEqual(state["edge_config_revision"], 2)

    def test_unapplied_attempt_keeps_legacy_config_gen_and_revision_available(self):
        state = self.state
        event = config_event(state, "c" * 64)
        settle(state, event, "ROLLED_BACK")
        validate_state(state)
        self.assertEqual(state["routing"]["config_hash"], event["previous_hash"])
        self.assertEqual(edge_config_revision(state), 0)
        self.assertEqual(next_edge_config_revision(state), 1)
        self.assertEqual(next_routing_generation(state), 3)

    def test_next_route_intent_extends_the_config_chain(self):
        self.update()
        state = self.state
        intent = self.route_attempt()
        state["pending"] = {"stage": "DISPATCHED", "intent": intent}
        validate_state(state)
        self.assertEqual(pending_event_type(state), "API")
        state["history"].append({"intent": intent, "result": "ROLLED_BACK", "recorded_at": now()})
        state["pending"] = None
        validate_state(state)
        self.assertEqual(next_routing_generation(state), 4)
        self.assertEqual(edge_config_revision(state), 1)

    def test_mutated_unapplied_config_record_invalidates_link_from_following_route(self):
        self.update()
        state = self.state
        intent = self.route_attempt()
        state["history"].append({"intent": intent, "result": "ROLLED_BACK", "recorded_at": now()})
        state["history"][-2]["recorded_at"] = "2026-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(Stop, "HISTORY_RECORD_HASH_MISMATCH"):
            validate_state(state)

    def test_route_intent_cannot_drop_or_forge_mixed_history_link(self):
        self.update()
        for value in (None, "9" * 64):
            with self.subTest(value=value):
                state = deepcopy(self.state)
                intent = self.route_attempt()
                if value is None:
                    del intent["previous_record_hash"]
                else:
                    intent["previous_record_hash"] = value
                state["pending"] = {"stage": "PREPARED", "intent": hashed(intent, "intent_hash")}
                with self.assertRaisesRegex(Stop, "HISTORY_RECORD_HASH_MISMATCH"):
                    validate_state(state)

    def test_config_event_corruption_is_rejected_even_if_envelope_is_rehashed(self):
        for key, value, reason in (
            ("event_hash", "0" * 64, "EDGE_CONFIG_EVENT_HASH_MISMATCH"),
            ("previous_record_hash", "9" * 64, "HISTORY_RECORD_HASH_MISMATCH"),
            ("previous_autosave_hash", "f" * 64, "EDGE_CONFIG_EVENT_HASH_MISMATCH"),
            ("payload_sha256", "f" * 64, "EDGE_CONFIG_EVENT_HASH_MISMATCH"),
            ("reason", "ROUTING", "INVALID_EDGE_CONFIG_EVENT_TYPE"),
            ("edge_boot_id", "invalid", "INVALID_EDGE_CONFIG_BOOT_ID"),
            ("timestamp", "2026-11-16T23:50:00", "INVALID_EDGE_CONFIG_TIMESTAMP"),
            ("routing_generation", 3, "EDGE_CONFIG_EVENT_ROUTING_MISMATCH"),
            ("active", {"api": "GREEN", "frontend": "BLUE"}, "EDGE_CONFIG_ROUTING_NOT_STABLE"),
            ("previous_revision", True, "INVALID_EDGE_CONFIG_REVISION"),
        ):
            with self.subTest(key=key):
                state = deepcopy(self.state)
                event = config_event(state, "c" * 64)
                event[key] = value
                if key != "event_hash":
                    hashed(event, "event_hash")
                settle(state, event)
                with self.assertRaisesRegex(Stop, reason):
                    validate_state(state)

    def test_config_rollback_cannot_claim_an_unrelated_hash(self):
        self.update()
        wrong = config_event(self.state, "b" * 64, EDGE_CONFIG_ROLLBACK, revision=0)
        self.state["pending"] = {"stage": "DISPATCHED", "event": wrong}
        with self.assertRaisesRegex(Stop, "EDGE_CONFIG_ROLLBACK_HASH_MISMATCH"):
            validate_state(self.state)

    def test_legacy_hash_or_explicit_revision_without_events_is_rejected(self):
        stale = deepcopy(self.state)
        stale["routing"]["config_hash"] = "c" * 64
        with self.assertRaisesRegex(Stop, "COMMITTED_HISTORY_MISMATCH"):
            validate_state(stale)
        self.state["edge_config_revision"] = 0
        with self.assertRaisesRegex(Stop, "COMMITTED_EDGE_CONFIG_REVISION_MISMATCH"):
            validate_state(self.state)

    def test_config_update_cannot_mutate_committed_revision_without_an_event(self):
        self.update()
        self.state["edge_config_revision"] = 2
        with self.assertRaisesRegex(Stop, "COMMITTED_EDGE_CONFIG_REVISION_MISMATCH"):
            validate_state(self.state)


if __name__ == "__main__":
    unittest.main()
