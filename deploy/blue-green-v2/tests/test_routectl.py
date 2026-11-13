"""Deterministic route-only tests, including process death at durable boundaries."""

import copy
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from release_state import StateStore, Stop, canonical, decode, digest, exclusive_lock, now
from routectl import ADMIN_ADDRESS, BLUE_PAIR, CaddyEdge, Controller, DEFAULT_HOSTS, upstream


class Killed(BaseException):
    pass


def fixtures(slot):
    seed = "a" if slot == "BLUE" else "b"
    release = {"revision": seed * 40, "tree": seed * 39 + "0",
               "images": {"api": "sha256:" + seed * 64,
                          "frontend": "sha256:" + seed * 63 + "0"}}
    identities = {}
    for role in release["images"]:
        identities[role, slot] = {
            "container_id": ("1" if slot == "BLUE" else "2") * 63 + ("1" if role == "api" else "2"),
            "pid": (100 if slot == "BLUE" else 200) + (1 if role == "api" else 2),
            "start_ticks": 12345,
            "started_at": now(),
            "image_id": release["images"][role], "revision": release["revision"], "tree": release["tree"],
            "restart_count": 0, "oom_killed": False, "upstream": upstream(role, slot),
            "runtime_hash": "1" * 64, "mounts_hash": "2" * 64, "network_hash": "3" * 64,
        }
    return release, identities


def edge_config():
    def server(slot, listener, gen):
        return {"listen": [listener], "strict_sni_host": True, "routes": [
            {"match": [{"host": [host]}], "handle": [
                {"handler": "subroute", "routes": [{"handle": [
                    {"handler": "log_append", "key": "route_generation", "value": gen},
                    {"handler": "reverse_proxy", "upstreams": [{"dial": upstream(role, slot)}]},
                ]}]},
            ], "terminal": True}
            for role, host in DEFAULT_HOSTS.items()
        ]}

    return {
        "admin": {"listen": ADMIN_ADDRESS},
        "apps": {"http": {"servers": {
            "public": {**server("BLUE", ":443", "0"), "@id": "bgv2-generation-0"},
            "preview-blue": server("BLUE", "127.0.0.1:8444", "preview"),
            "preview-green": server("GREEN", "127.0.0.1:8443", "preview"),
            "redirect": {"listen": [":80"]},
        }}},
    }


class Wire(CaddyEdge):
    def __init__(self):
        super().__init__()
        self.live = edge_config()
        self.autosave = copy.deepcopy(self.live)
        self.patch_attempts = 0
        self.fault = None
        self.raw_autosave = None

    def request(self, method, etag=None, config=None):
        live_etag = '"/config/ ' + digest(self.live)[:16] + '"'
        if method == "GET":
            return 200, live_etag, canonical(self.live)
        self.patch_attempts += 1
        if self.fault == "collision" or etag != live_etag:
            return 412, None, b""
        if self.fault == "no-response-not-applied":
            raise TimeoutError()
        self.live = copy.deepcopy(config)
        if self.fault == "failed-autosave":
            return 200, None, b""
        self.autosave = copy.deepcopy(config)
        if self.fault == "no-response-applied":
            raise TimeoutError()
        if self.fault == "killed-applied":
            raise Killed()
        if callable(self.fault):
            self.fault()
        return 200, None, b""

    def read_autosave(self):
        return self.raw_autosave if self.raw_autosave is not None else canonical(self.autosave)


class FaultStore(StateStore):
    fault = None

    def save(self, state):
        super().save(state)
        if self.fault and state["pending"] is not None and state["pending"]["stage"] == self.fault:
            raise Killed()


class PersistedWire(Wire):
    def __init__(self, path, interrupted=None):
        super().__init__()
        self.path = Path(path)
        self.interrupted = interrupted
        self.live = decode(self.path.read_bytes())
        self.autosave = copy.deepcopy(self.live)

    def request(self, method, etag=None, config=None):
        response = super().request(method, etag, config)
        if method == "PATCH" and response[0] == 200:
            with self.path.open("wb") as stream:
                stream.write(canonical(self.autosave))
                stream.flush()
                os.fsync(stream.fileno())
            if self.interrupted:
                self.interrupted.set()
                multiprocessing.Event().wait(30)
        return response


def switch_until_killed(directory, wire_path, identities, checkpoint, interrupted):
    class PausingStore(StateStore):
        def save(self, state):
            super().save(state)
            if checkpoint == "PREPARED" and (state["pending"] or {}).get("stage") == "PREPARED":
                interrupted.set()
                multiprocessing.Event().wait(30)

    edge = PersistedWire(wire_path, interrupted if checkpoint == "APPLIED" else None)
    controller = Controller(PausingStore(directory), edge, lambda role, slot: identities[role, slot],
                            RouteControllerTests.ready_gate, FakePlayerGate())
    controller.switch_api("GREEN", "fixture-baseline")


def take_lock(directory, ready, release):
    with StateStore(directory).lock():
        ready.set()
        release.wait(15)


def take_edge_lock(socket_path, ready, release):
    with CaddyEdge(socket_path=socket_path).lock():
        ready.set()
        release.wait(15)


class FakePlayerGate:
    @staticmethod
    def capture_baseline(receipt, context):
        return digest({"fixture": receipt, "context": context}) if receipt == "fixture-baseline" else None

    def __call__(self, receipt, context):
        return receipt == context


class RouteControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FaultStore(Path(self.temp.name) / "state")
        self.edge = Wire()
        self.blue, blue_identities = fixtures("BLUE")
        self.green, green_identities = fixtures("GREEN")
        self.identities = blue_identities | green_identities
        self.controller = Controller(self.store, self.edge, self.probe, self.ready_gate, FakePlayerGate())
        self.controller.initialize(self.blue, self.receipts("BLUE"), baseline_hash=digest(self.edge.live))
        self.controller.prepare("GREEN", self.green)
        self.controller.start("GREEN")
        for role, receipt in self.receipts("GREEN").items():
            self.controller.register_ready(role, "GREEN", receipt)

    def probe(self, role, slot):
        entry = self.identities[role, slot]
        if entry is None:
            raise Stop("DEAD_PROCESS")
        return copy.deepcopy(entry)

    def receipts(self, slot):
        return {role: {"identity_hash": digest(self.probe(role, slot)), "role": role}
                for role in ("api", "frontend")}

    @staticmethod
    def ready_gate(receipt, context):
        return receipt == {key: context[key] for key in ("identity_hash", "role")}

    def assert_routes(self, api, frontend):
        state = self.controller.status()
        pair = {"api": api, "frontend": frontend}
        self.assertEqual(state["routing"]["active"], pair)
        self.assertEqual(self.edge.effective()[0].active, pair)
        self.assertEqual(self.edge.persisted().active, pair)

    def test_switch_and_full_rollback_preserve_exact_processes(self):
        before = copy.deepcopy(self.identities)
        first = self.controller.switch_api("GREEN", "fixture-baseline")
        self.assert_routes("GREEN", "BLUE")
        self.controller.switch_frontend("GREEN", first["routing"]["api_transition"])
        self.assert_routes("GREEN", "GREEN")
        self.controller.rollback()
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(before, self.identities)
        self.assertEqual(self.edge.patch_attempts, 3)

    def test_two_successive_releases_can_reuse_retired_slot(self):
        first = self.controller.switch_api("GREEN", "fixture-baseline")
        self.controller.switch_frontend("GREEN", first["routing"]["api_transition"])
        self.controller.retire("BLUE")
        self.controller.prepare("BLUE", self.blue)
        self.controller.start("BLUE")
        for role, receipt in self.receipts("BLUE").items():
            self.controller.register_ready(role, "BLUE", receipt)
        self.controller.switch_api("BLUE", "fixture-baseline")
        self.assert_routes("BLUE", "GREEN")

    def test_player_missing_rolls_back_without_frontend_promotion(self):
        self.controller.switch_api("GREEN", "fixture-baseline")
        with self.assertRaisesRegex(Stop, "PLAYER_GATE_FAILED_BACKEND_ROLLED_BACK"):
            self.controller.switch_frontend("GREEN", {"invalid": True})
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(self.edge.patch_attempts, 2)

    def test_missing_baseline_never_dispatches(self):
        with self.assertRaisesRegex(Stop, "PLAYER_BASELINE_RECEIPT_REJECTED"):
            self.controller.switch_api("GREEN", None)
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_dead_after_ready_never_dispatches(self):
        self.identities["api", "GREEN"] = None
        with self.assertRaisesRegex(Stop, "DEAD_PROCESS"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(self.controller.status()["slots"]["GREEN"]["phase"], "FAILED")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_failed_qa_can_invalidate_only_the_candidate(self):
        with self.assertRaisesRegex(Stop, "SLOT_PROTECTED"):
            self.controller.invalidate("BLUE")
        self.controller.invalidate("GREEN")
        self.assertEqual(self.controller.status()["slots"]["GREEN"]["phase"], "FAILED")
        with self.assertRaisesRegex(Stop, "SLOT_NOT_READY"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.controller.prepare("GREEN", self.green)
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_unexpected_digest_never_dispatches(self):
        self.identities["api", "GREEN"]["image_id"] = self.blue["images"]["api"]
        with self.assertRaisesRegex(Stop, "SLOT_RELEASE_IDENTITY_MISMATCH"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_restart_after_ready_never_dispatches(self):
        self.identities["api", "GREEN"]["restart_count"] = 1
        with self.assertRaisesRegex(Stop, "SLOT_PROCESS_IDENTITY_INVALID"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_pid_mutation_never_dispatches(self):
        self.identities["api", "GREEN"]["pid"] += 1
        with self.assertRaisesRegex(Stop, "READY_IDENTITY_CHANGED"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_death_after_patch_records_failed_state_and_preserves_rollback(self):
        self.edge.fault = lambda: self.identities.__setitem__(("api", "GREEN"), None)
        with self.assertRaisesRegex(Stop, "SWITCH_IDENTITY_CHECK_FAILED"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.controller.status()["slots"]["GREEN"]["phase"], "FAILED")
        self.edge.fault = None
        self.controller.rollback()
        self.assert_routes("BLUE", "BLUE")

    def test_transient_standby_inspection_failure_requires_exact_revalidation_for_rollback(self):
        first = self.controller.switch_api("GREEN", "fixture-baseline")
        exact_blue = self.identities["api", "BLUE"]
        self.edge.fault = lambda: self.identities.__setitem__(("api", "BLUE"), None)
        with self.assertRaisesRegex(Stop, "SWITCH_IDENTITY_CHECK_FAILED"):
            self.controller.switch_frontend("GREEN", first["routing"]["api_transition"])
        self.edge.fault = None
        with self.assertRaisesRegex(Stop, "DEAD_PROCESS"):
            self.controller.rollback()
        self.identities["api", "BLUE"] = exact_blue
        self.controller.rollback()
        self.assert_routes("BLUE", "BLUE")

    def test_kill_before_dispatch_reconciles_old_old(self):
        self.store.fault = "PREPARED"
        with self.assertRaises(Killed):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.store.fault = None
        self.assertEqual(self.controller.reconcile()["result"], "ROLLBACK")
        self.assert_routes("BLUE", "BLUE")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_kill_after_dispatch_reconciles_exact_applied(self):
        self.edge.fault = "killed-applied"
        with self.assertRaises(Killed):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.controller.reconcile()["result"], "COMMIT")
        self.assert_routes("GREEN", "BLUE")
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_killed_writer_releases_lock_and_recovers_at_each_durable_boundary(self):
        context = multiprocessing.get_context("spawn")
        wire_path = Path(self.temp.name) / "disposable-edge.json"
        wire_path.write_bytes(canonical(self.edge.live))
        for checkpoint, outcome, target in (("PREPARED", "ROLLBACK", BLUE_PAIR),
                                            ("APPLIED", "COMMIT", {"api": "GREEN", "frontend": "BLUE"})):
            with self.subTest(checkpoint=checkpoint):
                interrupted = context.Event()
                child = context.Process(target=switch_until_killed,
                                        args=(self.store.directory, wire_path, self.identities,
                                              checkpoint, interrupted))
                child.start()
                try:
                    self.assertTrue(interrupted.wait(10), "Writer did not reach durable checkpoint")
                finally:
                    child.kill()
                    child.join(10)
                self.assertIsNotNone(child.exitcode)
                edge = PersistedWire(wire_path)
                recovered = Controller(StateStore(self.store.directory), edge, self.probe,
                                       self.ready_gate, FakePlayerGate()).reconcile()
                self.assertEqual(recovered["result"], outcome)
                self.assertEqual(recovered["state"]["routing"]["active"], target)
                self.assertEqual(edge.patch_attempts, 0)

    def test_timeout_with_apply_reconciles_no_second_patch(self):
        self.edge.fault = "no-response-applied"
        with self.assertRaisesRegex(Stop, "EDGE_MUTATION_OUTCOME_UNKNOWN"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.controller.reconcile()["result"], "COMMIT")
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_timeout_without_apply_remains_ambiguous(self):
        self.edge.fault = "no-response-not-applied"
        with self.assertRaisesRegex(Stop, "EDGE_MUTATION_OUTCOME_UNKNOWN"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.effective()[0].active, BLUE_PAIR)
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_412_never_retries(self):
        self.edge.fault = "collision"
        with self.assertRaisesRegex(Stop, "EDGE_ETAG_COLLISION"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.controller.reconcile()["result"], "ROLLBACK")
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_edge_restart_recovers_committed_pair(self):
        self.controller.switch_api("GREEN", "fixture-baseline")
        self.edge.live = copy.deepcopy(self.edge.autosave)
        self.assertEqual(self.controller.reconcile()["result"], "IN_SYNC")
        self.assert_routes("GREEN", "BLUE")

    def test_2xx_without_autosave_fails_closed(self):
        self.edge.fault = "failed-autosave"
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_replacing_state_with_stale_copy_fails_closed(self):
        old = self.store.path.read_bytes()
        self.controller.switch_api("GREEN", "fixture-baseline")
        self.store.path.write_bytes(old)
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.reconcile()
        self.assertEqual(self.edge.patch_attempts, 1)

    def test_corrupt_state_fails_closed(self):
        self.store.path.write_bytes(b'{"sha256":"corrupt","state":{}}')
        with self.assertRaisesRegex(Stop, "STATE_CHECKSUM_MISMATCH"):
            self.controller.reconcile()

    def test_unknown_mutation_fails_even_if_autosave_matches(self):
        self.edge.live["logging"] = {"different": "config"}
        self.edge.autosave = copy.deepcopy(self.edge.live)
        with self.assertRaisesRegex(Stop, "STOP_AMBIGUOUS"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_truncated_autosave_fails_closed(self):
        self.edge.raw_autosave = b'{"apps":{'
        with self.assertRaisesRegex(Stop, "INVALID_JSON"):
            self.controller.switch_api("GREEN", "fixture-baseline")
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_concurrent_writer_in_separate_process_is_rejected(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=take_lock, args=(self.store.directory, ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "lock holder did not start")
            with self.assertRaisesRegex(Stop, "ROUTECTL_WRITER_LOCKED"):
                self.controller.switch_api("GREEN", "fixture-baseline")
        finally:
            release.set()
            process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.edge.patch_attempts, 0)

    def test_two_state_dirs_cannot_write_same_edge(self):
        socket_path = Path(self.temp.name) / "admin.sock"
        self.edge.socket_path = socket_path
        alternate = Controller(StateStore(Path(self.temp.name) / "alternate"), self.edge,
                               self.probe, self.ready_gate, FakePlayerGate())
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=take_edge_lock, args=(socket_path, ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "edge lock holder did not start")
            with self.assertRaisesRegex(Stop, "ROUTECTL_WRITER_LOCKED"):
                alternate.initialize(self.blue, self.receipts("BLUE"), baseline_hash=digest(self.edge.live))
        finally:
            release.set()
            process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.edge.patch_attempts, 0)


if __name__ == "__main__":
    unittest.main()
