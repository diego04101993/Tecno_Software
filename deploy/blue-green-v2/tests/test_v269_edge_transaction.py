"""Check telemetry-only transaction boundaries with deterministic Edge faults."""

from copy import deepcopy
import multiprocessing
import tempfile
from pathlib import Path
import unittest

from edge_boot import BOOTSTRAP, ColdStart, gated_config
from edge_telemetry import telemetry_enabled, telemetry_rollback, telemetry_update
from release_state import StateStore, Stop, canonical, decode, digest, edge_config_revision
from routectl import BLUE_PAIR, Controller, RouteConfig
import test_routectl as routes


BOOT_ONE = b"01234567-0000-4000-8000-000000000001\n"
BOOT_TWO = b"01234567-0000-4000-8000-000000000002\n"


def legacy_config():
    config = routes.edge_config()
    config["logging"] = {"logs": {"default": {
        "encoder": {"format": "filter", "wrap": {"format": "json"}, "fields": {
            key: {"filter": "delete"} for key in ("error", "headers", "request", "resp_headers")
        }},
        "exclude": ["http.log.error"],
    }}}
    servers = config["apps"]["http"]["servers"]
    for server in ("public", "preview-blue", "preview-green"):
        for site in servers[server]["routes"]:
            subroute = site["handle"][0]
            handlers = subroute["routes"][0]["handle"]
            handlers[0]["early"] = True
            handlers.insert(1, {"handler": "log_append", "key": "upstream",
                                "value": "{http.reverse_proxy.upstream.hostport}"})
            if server.startswith("preview-"):
                subroute["routes"] = [
                    {"group": "allowed", "match": [{"method": ["GET"]}],
                     "handle": [{"handler": "subroute", "routes": subroute["routes"]}]},
                    {"group": "allowed", "handle": [{"handler": "static_response", "status_code": 403}]},
                ]
    config["apps"]["tls"] = {"automation": {"policies": [{"issuers": [{"module": "internal"}]}]}}
    servers["redirect"]["routes"] = []
    return gated_config(config)


class BootableWire(routes.Wire):
    def __init__(self):
        super().__init__()
        self.live = legacy_config()
        self.autosave = deepcopy(self.live)
        self.boot = BOOT_ONE
        self.admitted = True

    def boot_token(self):
        return self.boot

    def status_identity(self):
        return (0, 0, 0, self.boot), (0,) if self.admitted else None

    def close_ingress(self):
        self.admitted = False

    def ingress_open(self):
        return self.admitted

    def open_ingress(self, config_hash, *, before_write=None):
        assert not self.admitted and self.effective()[0].hash == self.persisted().hash == config_hash
        self.admitted = True


def config_until_killed(directory, wire_path, identities, stage, reached):
    class PausingStore(StateStore):
        def save(self, state):
            super().save(state)
            if stage == "PREPARED" and (state["pending"] or {}).get("stage") == "PREPARED":
                reached.set()
                multiprocessing.Event().wait(30)

    edge = routes.PersistedWire(wire_path, reached if stage == "APPLIED" else None)
    controller = Controller(PausingStore(directory), edge, lambda role, slot: identities[role, slot],
                            routes.RouteControllerTests.ready_gate, routes.FakePlayerGate())
    controller.config_update()


class TelemetryPolicyTests(unittest.TestCase):
    def test_exact_reversible_delta_retains_full_routing_and_tls_bytes(self):
        old = RouteConfig(legacy_config()).changed(BLUE_PAIR, 2)
        source = canonical(old.config)
        new = RouteConfig(telemetry_update(old.config))
        self.assertTrue(telemetry_enabled(new.config))
        self.assertEqual(canonical(telemetry_rollback(new.config)), source)
        self.assertEqual((old.active, old.generation, old.proxies, old.targets),
                         (new.active, new.generation, new.proxies, new.targets))
        self.assertEqual(old.config["apps"]["tls"], new.config["apps"]["tls"])
        self.assertEqual(canonical(old.config), source)

    def test_wrong_classifier_or_missing_redaction_is_rejected(self):
        old = legacy_config()
        updated = telemetry_update(old)
        public = updated["apps"]["http"]["servers"]["public"]
        api_site = next(row for row in public["routes"] if row.get("match") == [
            {"host": [routes.DEFAULT_HOSTS["api"]]}])
        classifier = api_site["handle"][0]["routes"][0]["handle"][0]
        classifier["mappings"][1]["outputs"] = ["OTHER"]
        self.assertFalse(telemetry_enabled(updated))
        with self.assertRaisesRegex(Stop, "EDGE_TELEMETRY"):
            telemetry_rollback(updated)
        classifier["mappings"][1]["outputs"] = ["PLAYER_RUNTIME_CONFIG"]
        api_site["handle"][0]["routes"][-1]["handle"][0]["early"] = 1
        self.assertFalse(telemetry_enabled(updated))
        del old["logging"]["logs"]["default"]["encoder"]["fields"]["request"]
        with self.assertRaisesRegex(Stop, "EDGE_TELEMETRY_REDACTION_MISMATCH"):
            telemetry_update(old)


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = routes.FaultStore(Path(self.temp.name) / "state")
        self.edge = BootableWire()
        blue, blue_ids = routes.fixtures("BLUE")
        green, green_ids = routes.fixtures("GREEN")
        self.ids = blue_ids | green_ids
        self.controller = Controller(self.store, self.edge, self.probe, routes.RouteControllerTests.ready_gate,
                                     routes.FakePlayerGate())
        self.controller.initialize(blue, self.receipts("BLUE"), baseline_hash=digest(self.edge.live))
        self.controller.prepare("GREEN", green)
        self.controller.start("GREEN")
        for role, receipt in self.receipts("GREEN").items():
            self.controller.register_ready(role, "GREEN", receipt)
        self.controller.switch_api("GREEN", "fixture-baseline")
        self.original = self.controller.rollback()
        self.original_config = deepcopy(self.edge.live)
        self.edge.patch_attempts = 0
        self.assertEqual(self.original["routing"]["generation"], 2)

    def probe(self, role, slot):
        return deepcopy(self.ids[role, slot])

    def receipts(self, slot):
        return {role: {"identity_hash": digest(self.probe(role, slot)), "role": role} for role in BLUE_PAIR}

    def assert_current(self, revision):
        state = self.controller.status()
        self.assertEqual(state["routing"]["active"], BLUE_PAIR)
        self.assertEqual(state["routing"]["generation"], 2)
        self.assertEqual(edge_config_revision(state), revision)
        self.assertEqual(state["slots"], self.original["slots"])
        self.assertEqual(state["history"][:2], self.original["history"])
        self.assertEqual(self.edge.effective()[0].hash, self.edge.persisted().hash)
        self.assertEqual(state["routing"]["config_hash"], self.edge.effective()[0].hash)
        return state

    def test_update_rollback_and_high_watermark_preserve_legacy_prefix(self):
        self.assertNotIn("edge_config_revision", self.original)
        self.controller.config_update()
        changed = self.assert_current(1)
        self.assertEqual(changed["history"][-1]["event"]["type"], "EDGE_CONFIG_UPDATE")
        self.assertTrue(telemetry_enabled(self.edge.live))
        self.controller.config_rollback()
        reverted = self.assert_current(0)
        self.assertEqual(reverted["history"][-1]["event"]["type"], "EDGE_CONFIG_ROLLBACK")
        self.assertEqual(reverted["history"][:-1], changed["history"])
        self.assertEqual(self.edge.live, self.original_config)
        self.controller.config_update()
        self.assert_current(2)
        self.assertEqual(self.edge.patch_attempts, 3)

    def test_crash_before_patch_reconciles_previous_without_dispatch(self):
        self.store.fault = "PREPARED"
        with self.assertRaises(routes.Killed):
            self.controller.config_update()
        self.store.fault = None
        self.assertEqual(self.store.load()["pending"]["stage"], "PREPARED")
        self.assertEqual(self.controller.reconcile()["result"], "ROLLBACK")
        self.assert_current(0)
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_crash_after_patch_reconciles_applied_exactly_once(self):
        self.edge.fault = "killed-applied"
        with self.assertRaises(routes.Killed):
            self.controller.config_update()
        self.assertEqual(self.store.load()["pending"]["stage"], "DISPATCHED")
        self.assertEqual(self.controller.reconcile()["result"], "COMMIT")
        self.assert_current(1)
        self.assertEqual(self.controller.reconcile()["result"], "IN_SYNC")
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_actual_kill_releases_writer_and_recovers_at_each_durable_boundary(self):
        context = multiprocessing.get_context("spawn")
        path = Path(self.temp.name) / "disposable-edge.json"
        path.write_bytes(canonical(self.edge.live))
        for stage, result, revision in (("PREPARED", "ROLLBACK", 0), ("APPLIED", "COMMIT", 1)):
            with self.subTest(stage=stage):
                reached = context.Event()
                process = context.Process(target=config_until_killed,
                                          args=(self.store.directory, path, self.ids, stage, reached))
                process.start()
                try:
                    self.assertTrue(reached.wait(10), "Writer did not reach the durable checkpoint")
                finally:
                    if process.is_alive():
                        process.kill()
                    process.join(10)
                self.assertIsNotNone(process.exitcode)
                edge = routes.PersistedWire(path)
                controller = Controller(StateStore(self.store.directory), edge, self.probe,
                                        routes.RouteControllerTests.ready_gate, routes.FakePlayerGate())
                outcome = controller.reconcile()
                self.assertEqual(outcome["result"], result)
                self.assertEqual(edge_config_revision(outcome["state"]), revision)
                self.assertEqual(outcome["state"]["routing"]["generation"], 2)
                self.assertEqual(outcome["state"]["routing"]["active"], BLUE_PAIR)
                self.assertEqual(edge.patch_attempts, 0)

    def test_lost_response_with_apply_commits_once_after_reconcile(self):
        self.edge.fault = "no-response-applied"
        with self.assertRaisesRegex(Stop, "EDGE_MUTATION_OUTCOME_UNKNOWN"):
            self.controller.config_update()
        self.assertEqual(self.controller.reconcile()["result"], "COMMIT")
        self.assert_current(1)
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_lost_response_without_apply_never_resends_and_new_boot_can_reconcile(self):
        self.edge.fault = "no-response-not-applied"
        with self.assertRaisesRegex(Stop, "EDGE_MUTATION_OUTCOME_UNKNOWN"):
            self.controller.config_update()
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.patch_attempts, 1)
        self.assertEqual(self.edge.live, self.original_config)
        self.edge.fault = None
        self.edge.admitted = False
        self.edge.boot = BOOT_TWO
        self.edge.live = deepcopy(BOOTSTRAP)
        self.assertTrue(ColdStart(self.controller).recover(expected_boot=BOOT_TWO)["ingress_ready"])
        self.assert_current(0)
        self.assertEqual(self.edge.patch_attempts, 2)  # one load of the old committed config
        self.controller.config_update()
        self.assert_current(1)

    def test_applied_recovery_on_reboot_restores_new_autosave(self):
        self.edge.fault = "no-response-applied"
        with self.assertRaisesRegex(Stop, "EDGE_MUTATION_OUTCOME_UNKNOWN"):
            self.controller.config_update()
        self.edge.fault = None
        self.edge.admitted = False
        self.edge.boot = BOOT_TWO
        self.edge.live = deepcopy(BOOTSTRAP)
        self.assertTrue(ColdStart(self.controller).recover(expected_boot=BOOT_TWO)["ingress_ready"])
        self.assert_current(1)

    def test_stale_etag_rejects_and_reconciles_without_blind_retry(self):
        self.edge.fault = "collision"
        with self.assertRaisesRegex(Stop, "EDGE_ETAG_COLLISION"):
            self.controller.config_update()
        self.assertEqual(self.store.load()["pending"]["stage"], "REJECTED")
        self.assertEqual(self.controller.reconcile()["result"], "ROLLBACK")
        self.assert_current(0)
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_stale_autosave_fails_closed_and_does_not_commit(self):
        self.edge.fault = "failed-autosave"
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.config_update()
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(edge_config_revision(self.store.load()), 0)
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_stale_state_with_matching_new_caddy_copies_fails_closed(self):
        original = self.store.path.read_bytes()
        self.controller.config_update()
        self.store.path.write_bytes(original)
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_unexpected_hash_while_dispatched_is_ambiguous(self):
        def out_of_band():
            self.edge.live["logging"]["logs"]["default"]["level"] = "ERROR"
        self.edge.fault = out_of_band
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.config_update()
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_second_writer_fails_before_patch(self):
        with self.store.lock(), self.assertRaisesRegex(Stop, "ROUTECTL_WRITER_LOCKED"):
            self.controller.config_update()
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_out_of_band_coherent_effective_and_autosave_fails_closed(self):
        self.edge.live = telemetry_update(self.edge.live)
        self.edge.autosave = deepcopy(self.edge.live)
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.config_update()
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_corrupt_config_event_fails_with_recomputed_state_envelope(self):
        self.controller.config_update()
        envelope = decode(self.store.path.read_bytes())
        envelope["state"]["history"][-1]["event"]["reason"] = "UNAPPROVED"
        envelope["sha256"] = digest(envelope["state"])
        self.store.path.write_bytes(canonical(envelope))
        with self.assertRaises(Stop):
            StateStore(self.store.directory).load()

    def test_committed_telemetry_recovers_over_known_old_autosave_on_new_boot(self):
        self.controller.config_update()
        self.edge.admitted = False
        self.edge.boot = BOOT_TWO
        self.edge.live = deepcopy(BOOTSTRAP)
        self.edge.autosave = deepcopy(self.original_config)
        self.assertFalse(self.edge.ingress_open())
        self.assertTrue(ColdStart(self.controller).recover(expected_boot=BOOT_TWO)["ingress_ready"])
        self.assert_current(1)
        self.assertTrue(telemetry_enabled(self.edge.live))

    def test_pending_status_identifies_event_without_mutating(self):
        self.store.fault = "PREPARED"
        with self.assertRaises(routes.Killed):
            self.controller.config_update()
        snapshot = self.store.path.read_bytes()
        report = self.controller.status_report()
        self.assertNotEqual(report["STATUS"], "PASS")
        self.assertEqual(report["PENDING_EVENT_TYPE"], "EDGE_CONFIG_UPDATE")
        self.assertEqual(self.store.path.read_bytes(), snapshot)


if __name__ == "__main__":
    unittest.main()
