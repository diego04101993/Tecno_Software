"""Deterministic legacy host boot policy and killed-writer recovery tests."""

from copy import deepcopy
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from edge_boot import BOOTSTRAP, ColdStart, gated_config, preview_config
from gates import DockerProbe, GateError, runtime_epoch
from release_state import StateStore, Stop, canonical, digest, write_private_atomic
from routectl import BLUE_PAIR, Controller, bind_legacy_blue
from test_legacy_blue import BOOT_ID, POSTGRES_ID, FixtureProbe, envelope, legacy_fixture, payload
from test_routectl import Wire, edge_config, fixtures


NEW_BOOT = "c724b063-7e74-4656-be0c-b239a7de2c72"


class EpochProbe(FixtureProbe):
    def __init__(self, fixture, manifest):
        super().__init__(fixture, manifest, manifest["sha256"])
        self.boot_id = BOOT_ID
        self.postgres_healthy = True
        self.namespace = "net:[42]"
        self.frontend_identity = fixtures("BLUE")[1]["frontend", "BLUE"]
        self.frontend_identity["started_at"] = "2026-11-12T18:00:00.000000000Z"
        self.frontend_stable = {"immutable_fixture": "frontend-blue"}

    def _probe(self, role, slot, legacy_compose=None, *, with_epoch=False):
        if role == "frontend" and slot == "BLUE":
            identity = deepcopy(self.frontend_identity)
            if with_epoch:
                return {"identity": identity, "stable_identity": deepcopy(self.frontend_stable),
                        "runtime_epoch": runtime_epoch(identity, self.boot_id)}
            return identity
        self.assert_scope(role, slot, legacy_compose)
        return payload(self.fixture, boot_id=self.boot_id, namespace=self.namespace)

    def legacy_engine_boot_id(self):
        return self.boot_id

    def legacy_postgres_id(self):
        if not self.postgres_healthy:
            raise GateError("LEGACY_POSTGRES_NOT_EXACT_HEALTHY")
        return POSTGRES_ID

    def require_legacy_reboot_health_ready(self, ready, postgres_id):
        assert postgres_id == POSTGRES_ID

    def verify_legacy_runtime_running(self, ready, epoch):
        assert epoch["boot_id"] == self.boot_id
        identities = {"api": self.legacy_api_snapshot()["identity"], "frontend": self.frontend_identity}
        if any(ready[role]["identity"]["pid"] != identity["pid"] for role, identity in identities.items()):
            raise GateError("STOP_REATTEST_REQUIRED")

    def reboot(self, *, new_boot=True):
        if new_boot:
            self.boot_id = NEW_BOOT
        item = self.fixture[0]
        item["State"].update(Pid=1735, StartedAt="2026-11-12T18:02:00Z")
        item["RestartCount"] = 1
        item["NetworkSettings"]["Networks"][self.fixture[-1]["network"]]["EndpointID"] = "8" * 64
        self.namespace = "net:[84]"
        self.frontend_identity.update(pid=1813, start_ticks=24691, started_at="2026-11-12T18:02:01Z",
                                      restart_count=1, network_hash="6" * 64)


class EpochWire(Wire):
    def __init__(self):
        super().__init__()
        self.live = deepcopy(BOOTSTRAP)
        self.autosave = None
        self.admitted = False

    def request(self, method, etag=None, config=None):
        previous = self.autosave
        response = super().request(method, etag, config)
        if method == "PATCH" and self.live.get("admin", {}).get("config", {}).get("persist") is False:
            self.autosave = previous
        return response

    def read_autosave(self):
        if self.autosave is None:
            raise FileNotFoundError
        return super().read_autosave()

    def close_ingress(self):
        self.admitted = False

    def open_ingress(self, config_hash):
        assert digest(self.live) == digest(self.autosave) == config_hash
        self.admitted = True

    def ingress_open(self):
        return self.admitted


class InterruptedEpochStore(StateStore):
    def __init__(self, root, reached, checkpoint):
        super().__init__(root)
        self.reached = reached
        self.checkpoint = checkpoint

    def save(self, state):
        if self.checkpoint == "before":
            self.pause()
        super().save(state)
        if self.checkpoint == "after":
            self.pause()

    def pause(self):
        self.reached.set()
        multiprocessing.Event().wait(30)


def recover_until_killed(directory, wire, probe, reached, checkpoint):
    store = InterruptedEpochStore(directory, reached, checkpoint)
    ColdStart(Controller(store, wire, probe, None, None)).recover()


class LegacyEpochTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = StateStore(Path(temp.name) / "release")
        self.fixture = legacy_fixture()
        self.manifest = envelope(self.fixture)
        self.probe = EpochProbe(self.fixture, self.manifest)
        self.wire = EpochWire()
        config = edge_config()
        config["apps"]["http"]["servers"]["redirect"]["routes"] = []
        self.config = gated_config(bind_legacy_blue(config, self.manifest, self.manifest["sha256"]))
        self.release = {"revision": self.fixture[-1]["revision"], "tree": self.fixture[-1]["tree"],
                        "images": {"api": self.fixture[-1]["image_id"], "frontend": self.probe.frontend_identity["image_id"]}}
        self.probe.frontend_identity.update(revision=self.release["revision"], tree=self.release["tree"])
        self.cold = ColdStart(Controller(self.store, self.wire, self.probe, lambda *_: True, None))
        self.cold.preview(self.config)
        self.original = self.cold.initialize(self.config, self.release, {"api": True, "frontend": True}, digest(self.config))
        self.cold.recover()
        self.assertTrue(self.wire.admitted)

    def reset_edge(self):
        self.wire.live = deepcopy(BOOTSTRAP)
        self.wire.close_ingress()

    def test_same_boot_and_process_retains_epoch(self):
        self.reset_edge()
        self.assertEqual(self.cold.recover(), {"generation": 0, "ingress_ready": True, "result": "COMMITTED"})
        self.assertEqual(self.store.load(), self.original)

    def test_legacy_preview_retires_scratch_tls_before_committed_config_load(self):
        self.wire.live = preview_config(self.config)
        self.wire.close_ingress()
        configurations = []
        request = self.wire.request

        def track(method, etag=None, config=None):
            if method == "PATCH":
                self.assertFalse(self.wire.admitted)
                configurations.append(config)
            return request(method, etag, config)

        with patch.object(self.wire, "request", side_effect=track):
            self.cold.recover()
        self.assertEqual(configurations, [BOOTSTRAP, self.config])
        self.assertTrue(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_rejected_private_tls_retirement_does_not_admit_ingress(self):
        staged = preview_config(self.config)
        self.wire.live = staged
        self.wire.fault = "collision"
        with self.assertRaisesRegex(Stop, "EDGE_ETAG_COLLISION"):
            self.cold.recover()
        self.assertEqual(self.wire.live, staged)
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_new_boot_rechecks_stable_identity_and_commits_epochs_atomically(self):
        self.reset_edge()
        self.probe.reboot()
        receipt = self.cold.recover()
        self.assertEqual(receipt["legacy_blue_reattest"], "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT")
        state = self.store.load()
        self.assertTrue(self.wire.admitted)
        self.assertEqual(state["routing"], self.original["routing"])
        self.assertEqual(state["legacy_blue"]["manifest_sha256"], self.manifest["sha256"])
        self.assertEqual(state["legacy_blue"]["frontend_stable_hash"], self.original["legacy_blue"]["frontend_stable_hash"])
        for role, epoch in state["legacy_blue"]["runtime_epochs"].items():
            self.assertEqual(epoch["boot_id"], NEW_BOOT)
            self.assertEqual(epoch["restart_count"], 1)
            self.assertEqual(state["slots"]["BLUE"]["ready"][role]["identity"]["pid"], epoch["pid"])
        self.assertNotIn("legacy_blue_reattest", self.cold.recover())

    def test_changed_boot_never_accepts_stable_identity_mutation(self):
        self.reset_edge()
        self.probe.reboot()
        edits = {
            "image": lambda item: item.update(Image="sha256:" + "9" * 64),
            "config": lambda item: item["Config"]["Env"].append("UNEXPECTED=true"),
            "mount": lambda item: item["Mounts"][0].update(Source="/different"),
            "container": lambda item: item.update(Id="9" * 64),
            "created": lambda item: item.update(Created="2026-11-12T17:58:00Z"),
            "network": lambda item: item["NetworkSettings"]["Networks"][self.fixture[-1]["network"]].update(Gateway="172.30.250.1"),
        }
        original = deepcopy(self.fixture[0])
        for field, edit in edits.items():
            with self.subTest(field=field), self.assertRaisesRegex(Stop, "LEGACY_BLUE_UNKNOWN_OR_DRIFTED"):
                self.fixture[0].clear()
                self.fixture[0].update(deepcopy(original))
                edit(self.fixture[0])
                self.cold.recover()
            self.assertFalse(self.wire.admitted)
            self.assertEqual(self.store.load(), self.original)

    def test_same_boot_docker_restart_closes_ingress_and_never_reattests(self):
        self.probe.reboot(new_boot=False)
        with self.assertRaisesRegex(Stop, "STOP_REATTEST_REQUIRED"):
            self.cold.monitor_legacy_blue()
        self.assertFalse(self.wire.admitted)
        self.reset_edge()
        with self.assertRaisesRegex(Stop, "STOP_REATTEST_REQUIRED"):
            self.cold.recover()
        self.assertEqual(self.store.load(), self.original)

    def test_monitor_reports_changed_process_before_health_finishes_starting(self):
        ready = self.original["slots"]["BLUE"]["ready"]
        inspected = []
        for role in ("api", "frontend"):
            identity = ready[role]["identity"]
            state = {"Pid": identity["pid"], "StartedAt": identity["started_at"], "Running": True,
                     "Status": "running", "OOMKilled": False, "Paused": False, "Restarting": False,
                     "Dead": False, "Health": {"Status": "healthy"}}
            inspected.append({"Id": identity["container_id"], "Image": identity["image_id"],
                              "RestartCount": identity["restart_count"], "State": state})
        self.probe._json = lambda *args: inspected
        monitor = DockerProbe.verify_legacy_runtime_running
        epoch = self.original["legacy_blue"]["runtime_epochs"]["api"]
        monitor(self.probe, ready, epoch)
        inspected[0]["State"].update(Pid=9873, Health={"Status": "starting"})
        with self.assertRaisesRegex(Stop, "STOP_REATTEST_REQUIRED"):
            monitor(self.probe, ready, epoch)

    def test_observed_docker_inspect_outage_closes_same_boot_and_preserves_epoch(self):
        def unavailable(*_):
            raise GateError("SLOT_READ_ONLY_INSPECTION_FAILED")

        self.probe.verify_legacy_runtime_running = unavailable
        with self.assertRaisesRegex(Stop, "STOP_REATTEST_REQUIRED"):
            self.cold.monitor_legacy_blue()
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_postgres_and_blue_frontend_are_rechecked_before_epoch_write(self):
        self.reset_edge()
        self.probe.reboot()
        self.probe.postgres_healthy = False
        with self.assertRaisesRegex(Stop, "LEGACY_POSTGRES_NOT_EXACT_HEALTHY"):
            self.cold.recover()
        self.probe.postgres_healthy = True
        self.probe.frontend_stable["immutable_fixture"] = "replacement"
        with self.assertRaisesRegex(Stop, "LEGACY_BLUE_FRONTEND_UNKNOWN_OR_DRIFTED"):
            self.cold.recover()
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_unknown_effective_config_prevents_epoch_write(self):
        self.reset_edge()
        self.probe.reboot()
        self.wire.live["logging"] = {"logs": {}}
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.cold.recover()
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_process_change_after_reboot_probe_prevents_epoch_write(self):
        self.reset_edge()
        self.probe.reboot()
        recheck = self.probe.verify_legacy_runtime_running

        def change_process(ready, epoch):
            self.probe.frontend_identity["pid"] += 1
            recheck(ready, epoch)

        self.probe.verify_legacy_runtime_running = change_process
        with self.assertRaisesRegex(Stop, "STOP_REATTEST_REQUIRED"):
            self.cold.recover()
        self.assertFalse(self.wire.admitted)
        self.assertEqual(self.store.load(), self.original)

    def test_epoch_corruption_is_closed_even_with_matching_envelope_checksum(self):
        corrupt = deepcopy(self.original)
        corrupt["legacy_blue"]["runtime_epochs"]["api"]["pid"] += 1
        write_private_atomic(self.store.path, canonical({"sha256": digest(corrupt), "state": corrupt}))
        with self.assertRaisesRegex(Stop, "LEGACY_RUNTIME_EPOCH_IDENTITY_MISMATCH"):
            self.cold.recover()
        self.assertFalse(self.wire.admitted)

    def test_killed_writer_before_and_after_epoch_commit_never_admits_ingress(self):
        self.reset_edge()
        self.probe.reboot()
        context = multiprocessing.get_context("spawn")
        for checkpoint, expected in (("before", BOOT_ID), ("after", NEW_BOOT)):
            with self.subTest(checkpoint=checkpoint):
                reached = context.Event()
                child = context.Process(target=recover_until_killed, args=(self.store.directory, self.wire, self.probe,
                                                                         reached, checkpoint))
                child.start()
                try:
                    self.assertTrue(reached.wait(10), "Reconciler missed epoch checkpoint")
                finally:
                    child.kill()
                    child.join(10)
                self.assertFalse(self.wire.admitted)
                self.assertEqual(self.store.load()["legacy_blue"]["runtime_epochs"]["api"]["boot_id"], expected)
        self.assertTrue(self.cold.recover()["ingress_ready"])
        self.assertEqual(self.store.load()["routing"]["active"], BLUE_PAIR)


if __name__ == "__main__":
    unittest.main()
