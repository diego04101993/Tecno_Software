"""Verify that stale prepared handoffs stop before the historical frontend moves."""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from edge_boot import BOOTSTRAP, ColdStart, gated_config
from gates import FRONTEND_CONFIG
from prepared_handoff import PreparedHandoff
from release_state import StateStore, Stop, digest, now, require, write_private_atomic
from routectl import BLUE_PAIR, CaddyEdge, Controller, RouteConfig, bind_legacy_blue
from test_legacy_blue import envelope, legacy_fixture
from test_legacy_epoch import EpochProbe, EpochWire
from test_routectl import edge_config


class PreparedWire(EpochWire):
    def __init__(self):
        super().__init__()
        self.etag_override = None
        self.locked = False
        self.patches = []
        self.reject_patch_at = None

    @contextmanager
    def lock(self):
        require(not self.locked, "ROUTECTL_WRITER_LOCKED")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def request(self, method, etag=None, config=None):
        if method == "PATCH":
            assert self.locked
            self.patches.append((etag, deepcopy(config)))
            if len(self.patches) == self.reject_patch_at:
                return 412, None, b""
        status, response_etag, body = super().request(method, etag, config)
        if method == "GET" and self.etag_override:
            response_etag = self.etag_override
        return status, response_etag, body

    def require_closed(self):
        require(not self.admitted, "INVALIDATE_HANDOFF_PREPARED_INGRESS")


class PreparedProbe(EpochProbe):
    def __init__(self, fixture, manifest):
        super().__init__(fixture, manifest)
        self.frontend_config_sha256 = "3" * 64
        self.bindings["frontend:BLUE"] = {"frontend_config_sha256": self.frontend_config_sha256}

    def _run(self, *args, timeout=15):
        assert args == ("exec", self.frontend_identity["container_id"], "sha256sum", FRONTEND_CONFIG)
        return f"{self.frontend_config_sha256}  {FRONTEND_CONFIG}\n".encode()


class Topology:
    def __init__(self, probe):
        self.probe = probe
        self.edge_pid = 301
        self.proxy_network_id = "8" * 64
        self.tls_owner = "historical-frontend"
        self.moved = False

    def snapshot(self):
        backend = self.probe.fixture[0]
        frontend = self.probe.frontend_identity
        return {
            "roles": {
                "backend": {"process": {
                    "id": backend["Id"], "image": backend["Image"], "pid": backend["State"]["Pid"],
                    "started_at": backend["State"]["StartedAt"], "restart_count": backend["RestartCount"],
                }},
                "frontend": {"process": {
                    "id": frontend["container_id"], "image": frontend["image_id"], "pid": frontend["pid"],
                    "started_at": frontend["started_at"], "restart_count": frontend["restart_count"],
                }},
                "edge": {"process": {"pid": self.edge_pid}},
                "historical": {"process": {"name": "/" + self.probe.edge_name}},
                "postgres": {"process": {"id": self.probe.legacy_postgres_id()}},
            },
            "proxy_network_id": self.proxy_network_id, "tls_owner": self.tls_owner,
        }

    def verify_moved(self, _before):
        assert self.moved
        return True


class PreparedHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(Path(directory.name) / "release")
        self.wire = PreparedWire()
        self.fixture = legacy_fixture()
        manifest = envelope(self.fixture)
        self.probe = PreparedProbe(self.fixture, manifest)
        self.topology = Topology(self.probe)
        self.controller = Controller(self.store, self.wire, self.probe, lambda *_: True, None)
        config = edge_config()
        config["apps"]["http"]["servers"]["redirect"]["routes"] = []
        self.public_config = gated_config(bind_legacy_blue(config, manifest, manifest["sha256"]))
        release = {
            "revision": self.fixture[-1]["revision"], "tree": self.fixture[-1]["tree"],
            "images": {"api": self.fixture[-1]["image_id"], "frontend": self.probe.frontend_identity["image_id"]},
        }
        self.probe.frontend_identity.update(revision=release["revision"], tree=release["tree"])
        cold = ColdStart(self.controller)
        cold.preview(self.public_config)
        cold.initialize(self.public_config, release, {"api": True, "frontend": True}, digest(self.public_config))
        self.wire.patches.clear()
        self.handoff = PreparedHandoff(self.controller, self.topology)
        self.events = []
        self.prepared = self.handoff.prepare(mark=self.events.append)
        self.approved_hash = self.prepared["sha256"]

    def execute(self, **options):
        def move():
            self.assertTrue(self.wire.locked)
            with self.assertRaisesRegex(Stop, "ROUTECTL_WRITER_LOCKED"):
                with self.store.lock():
                    self.fail("release-state writer lock was released before T0")
            self.events.append("TEST_FRONTEND_STOP")
            self.topology.moved = True

        return self.handoff.execute(self.prepared, expected_sha256=self.approved_hash, move=move,
                                    mark=self.events.append, **options)

    def assert_pre_downtime_stop(self, reason=".*"):
        state = self.store.path.read_bytes()
        private = deepcopy(self.wire.live)
        with self.assertRaisesRegex(Stop, reason):
            self.execute()
        self.assertNotIn("TEST_FRONTEND_STOP", self.events)
        self.assertFalse(self.topology.moved)
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.wire.patches, [])
        self.assertEqual(self.wire.live, private)
        self.assertEqual(self.store.path.read_bytes(), state)

    def test_etag_drift_stops_before_frontend_stop(self):
        self.wire.etag_override = '"/config/ ' + "9" * 16 + '"'
        self.assert_pre_downtime_stop()

    def test_committed_generation_drift_stops_before_frontend_stop(self):
        changed = self.store.load()
        mixed = {"api": "GREEN", "frontend": "BLUE"}
        baseline = RouteConfig(self.public_config)
        history = (("API", baseline, baseline.changed(mixed, 1)),
                   ("ROLLBACK", baseline.changed(mixed, 1), baseline.changed(BLUE_PAIR, 2)))
        for operation, previous, next_route in history:
            self.store.seal_config(next_route.config)
            intent = {
                "generation": next_route.generation, "previous_generation": previous.generation,
                "operation": operation, "from_slot": previous.active, "to_slot": next_route.active,
                "timestamp": now(), "release": changed["slots"]["BLUE"]["release"],
                "expected_etag": self.wire.raw_effective()[1], "previous_hash": previous.hash,
                "next_hash": next_route.hash, "baseline_hash": "8" * 64 if operation == "API" else None,
                "player_gate_hash": None,
            }
            intent["intent_hash"] = digest(intent)
            changed["history"].append({"intent": intent, "result": "COMMITTED", "recorded_at": now()})
        changed["routing"].update(generation=2, config_hash=history[-1][2].hash)
        self.store.save(changed)
        self.assert_pre_downtime_stop()

    def test_backend_identity_drift_stops_before_frontend_stop(self):
        self.probe.fixture[0]["State"]["Pid"] += 1
        self.assert_pre_downtime_stop()

    def test_frontend_blue_identity_drift_stops_before_frontend_stop(self):
        self.probe.frontend_identity["pid"] += 1
        self.assert_pre_downtime_stop()

    def test_frontend_file_drift_with_same_pid_stops_before_frontend_stop(self):
        self.probe.frontend_config_sha256 = "7" * 64
        self.assert_pre_downtime_stop("INVALIDATE_HANDOFF_PREPARED_FRONTEND_CONFIG")

    def test_edge_process_drift_stops_before_frontend_stop(self):
        self.topology.edge_pid += 1
        self.assert_pre_downtime_stop()

    def test_network_contract_drift_stops_before_frontend_stop(self):
        self.topology.proxy_network_id = "7" * 64
        self.assert_pre_downtime_stop()

    def test_tls_owner_drift_stops_before_frontend_stop(self):
        self.topology.tls_owner = "edge"
        self.assert_pre_downtime_stop()

    def test_prepared_configuration_hash_drift_stops_before_frontend_stop(self):
        self.prepared["payload"]["prepared_config_sha256"] = "7" * 64
        self.prepared["sha256"] = digest(self.prepared["payload"])
        self.assert_pre_downtime_stop()

    def test_corrupt_prepared_envelope_stops_before_frontend_stop(self):
        self.prepared["payload"] = []
        self.assert_pre_downtime_stop()

    def test_existing_malformed_admission_marker_is_not_closed(self):
        edge = CaddyEdge(socket_path=self.store.directory / "edge.sock")
        edge.require_closed()
        write_private_atomic(edge.ingress_marker, b"invalid-but-present\n", replace=False)
        self.assertFalse(edge.ingress_open())
        with self.assertRaisesRegex(Stop, "INVALIDATE_HANDOFF_PREPARED_INGRESS"):
            edge.require_closed()

    def test_second_cas_failure_after_move_keeps_ingress_closed(self):
        self.wire.reject_patch_at = 2
        sealed = self.store.path.read_bytes()
        with self.assertRaisesRegex(Stop, "EDGE_ETAG_COLLISION"):
            self.execute()
        self.assertTrue(self.topology.moved)
        self.assertEqual(len(self.wire.patches), 2)
        self.assertEqual(self.wire.live, BOOTSTRAP)
        self.assertFalse(self.wire.admitted)
        self.assertNotIn("T_INGRESS_READY", self.events)
        self.assertEqual(self.store.path.read_bytes(), sealed)

    def test_expired_downtime_budget_blocks_all_cas(self):
        def expired():
            raise Stop("HANDOFF_DOWNTIME_LIMIT_EXCEEDED")

        with self.assertRaisesRegex(Stop, "HANDOFF_DOWNTIME_LIMIT_EXCEEDED"):
            self.execute(ensure_budget=expired)
        self.assertTrue(self.topology.moved)
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.wire.patches, [])

    def test_ingress_budget_is_checked_after_config_and_autosave(self):
        edge = CaddyEdge(socket_path=self.store.directory / "edge.sock")
        config = RouteConfig(self.public_config)
        checked = []

        def persisted():
            checked.append("autosave")
            return config

        def expired():
            self.assertEqual(checked, ["autosave"])
            raise Stop("HANDOFF_DOWNTIME_LIMIT_EXCEEDED")

        edge.effective = lambda: (config, self.wire.raw_effective()[1])
        edge.persisted = persisted
        with self.assertRaisesRegex(Stop, "HANDOFF_DOWNTIME_LIMIT_EXCEEDED"):
            edge.open_ingress(config.hash, before_write=expired)
        edge.require_closed()

    def test_prepared_apply_preserves_tls_retirement_cas_and_state(self):
        sealed = self.store.path.read_bytes()
        self.assertFalse(self.wire.admitted)
        self.assertFalse(self.topology.moved)
        self.assertEqual(self.wire.patches, [])
        first_etag = self.wire.raw_effective()[1]
        self.execute()
        self.assertTrue(self.wire.admitted)
        self.assertEqual(self.wire.patches, [(first_etag, BOOTSTRAP),
                                             ('"/config/ ' + digest(BOOTSTRAP)[:16] + '"', self.public_config)])
        self.assertEqual(self.wire.live, self.public_config)
        self.assertEqual(self.wire.autosave, self.public_config)
        self.assertEqual(self.store.path.read_bytes(), sealed)
        self.assertLess(self.events.index("T_FINAL_REVALIDATION_COMPLETE"), self.events.index("TEST_FRONTEND_STOP"))
        self.assertLess(self.events.index("TEST_FRONTEND_STOP"), self.events.index("T_CONFIG_PATCH_BEGIN"))
        self.assertLess(self.events.index("T_CONFIG_PATCH_COMPLETE"), self.events.index("T_EFFECTIVE_CONFIG_VERIFIED"))


if __name__ == "__main__":
    unittest.main()
