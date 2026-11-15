"""Exercise the scoped frontend exception through Docker and controller gates."""

from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from edge_boot import BOOTSTRAP, ColdStart, gated_config
from gates import DockerProbe, FRONTEND_ARGV, GateError, ReadyGate, sign_receipt
from legacy_frontend_blue import LEGACY_FRONTEND_IMAGE_EXACT, READY_ADOPTION
from release_state import StateStore, Stop, digest, now, validate_state
from routectl import Controller, bind_legacy_blue
from test_legacy_blue import BOOT_ID, envelope, legacy_fixture
from test_legacy_epoch import EpochProbe, EpochWire
from test_routectl import edge_config, fixtures


PIN = "a" * 64
KEY = b"phase3 fixture only" * 3
FRONTEND_CHECKS = {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}


def release_for(api_binding, image_id):
    return {"revision": api_binding["revision"], "tree": api_binding["tree"],
            "images": {"api": api_binding["image_id"], "frontend": image_id}}


class DockerFrontend(DockerProbe):
    def __init__(self, *, embedded=False, explicit=True, slot="BLUE"):
        self.slot = slot
        self.fixture = legacy_fixture()
        self.api_manifest = envelope(self.fixture)
        frontend_id = "7" * 64
        image_id = "sha256:" + "6" * 64
        self.network = {"Name": "fixture-proxy", "Id": "5" * 64, "Driver": "bridge", "Internal": True,
                        "Containers": {frontend_id: {}}}
        self.release = release_for(self.fixture[-1], image_id)
        self.payload = {"release": self.release, "historical": {"container_id": "8" * 64}}
        self.raw_manifest = {"payload": self.payload, "sha256": PIN}
        self.slot_caddyfile = b":80 {\n  file_server\n}\n"
        self.binding = {"name": "fixture-frontend-" + slot.lower(), "image_id": image_id,
                        "revision": self.release["revision"], "tree": self.release["tree"],
                        "upstream": "frontend-" + slot.lower() + ":80", "ip": "172.30.250.4",
                        "network": self.network["Name"], "frontend_config_sha256": hashlib.sha256(self.slot_caddyfile).hexdigest()}
        if embedded:
            self.binding["legacy_frontend_adoption"] = {"manifest": self.raw_manifest, "sha256": PIN}
        self.item = {"Id": frontend_id, "Name": "/" + self.binding["name"], "Image": image_id, "RestartCount": 0,
                     "Path": FRONTEND_ARGV[0], "Args": FRONTEND_ARGV[1:],
                     "Created": "2026-11-12T17:59:00Z",
                     "Config": {"Labels": {"io.tecnocontrol.bgv2.slot": slot, "io.tecnocontrol.bgv2.role": "frontend"}},
                     "State": {"Running": True, "Status": "running", "OOMKilled": False, "Restarting": False,
                               "Dead": False, "Paused": False, "Pid": 842, "StartedAt": "2026-11-12T18:00:00Z",
                               "Health": {"Status": "healthy"}},
                     "HostConfig": {"PortBindings": {}, "PublishAllPorts": False},
                     "Mounts": [{"Type": "bind", "Destination": "/etc/caddy/Caddyfile.bgv2", "RW": False}],
                     "NetworkSettings": {"Ports": {"80/tcp": None}, "Networks": {
                         self.network["Name"]: {"NetworkID": self.network["Id"], "Aliases": ["frontend-" + slot.lower()],
                                                "IPAddress": self.binding["ip"], "EndpointID": "4" * 64}}}}
        self.image = {"Id": image_id, "Config": {"Labels": None}}
        self.historical = deepcopy(self.item)
        self.historical.update(Id=self.payload["historical"]["container_id"], Name="/fixture-historical")
        args = {"legacy_frontend_blue": self.raw_manifest, "legacy_frontend_blue_sha256": PIN} if explicit else {}
        bindings = {"api:BLUE": self.fixture[-1], "frontend:" + slot: self.binding}
        with patch("gates.verify_manifest", return_value=self.payload):
            super().__init__(bindings, edge_name="fixture-historical", legacy_blue=self.api_manifest,
                             legacy_blue_sha256=self.api_manifest["sha256"],
                             legacy_frontend_policy=SimpleNamespace(release=self.release), **args)

    def _json(self, *args):
        if args[:2] == ("context", "inspect"):
            return [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        if args[:2] == ("image", "inspect"):
            return [self.image]
        if args[:2] == ("network", "inspect"):
            return [self.network]
        if args[2:] == (self.historical["Id"],):
            return [self.historical]
        assert args[2:] in ((self.item["Id"],), (self.binding["name"],))
        return [self.item]

    def _run(self, *args, timeout=15):
        assert args[0] == "exec" and args[1] == self.item["Id"]
        return (b"\0".join(arg.encode() for arg in FRONTEND_ARGV) + b"\0\0"
                + b"1 (caddy) " + b" ".join([b"1"] * 20) + b"\nnet:[41]\n" + BOOT_ID.encode()
                + b"\n" + self.slot_caddyfile)

    def _host_boot_id(self, *_args):
        return BOOT_ID

    def proof(self, classification=LEGACY_FRONTEND_IMAGE_EXACT):
        return {"classification": classification, "container_id": self.item["Id"], "image_id": self.image["Id"]}


class LegacyFrontendDockerTests(unittest.TestCase):
    def test_only_private_classifier_can_admit_missing_oci(self):
        probe = DockerFrontend(embedded=True, explicit=False)
        with patch("gates.verify_private_frontend", return_value=probe.proof()) as classify:
            identity = probe.initial_legacy_frontend_identity()
            self.assertEqual(identity["container_id"], probe.item["Id"])
            self.assertEqual(identity["image_id"], probe.image["Id"])
            self.assertEqual(probe.legacy_frontend_blue_sha256, PIN)
            with self.assertRaises(GateError):
                probe("frontend", "BLUE")
            with self.assertRaisesRegex(GateError, "LEGACY_FRONTEND_SCOPE_INVALID"):
                probe._probe("frontend", "GREEN", with_epoch=True, legacy_frontend=True)
            self.assertEqual(classify.call_count, 1)

    def test_unclassified_or_contradictory_oci_blocks_even_exact_slot(self):
        probe = DockerFrontend()
        with patch("gates.verify_private_frontend", return_value=probe.proof("UNKNOWN_OR_DRIFTED")):
            with self.assertRaisesRegex(GateError, "FRONTEND_UNKNOWN_OR_DRIFTED"):
                probe.initial_legacy_frontend_identity()
        for key in ("org.opencontainers.image.revision", "io.tecnocontrol.git.tree"):
            with self.subTest(key=key), patch("gates.verify_private_frontend", return_value=probe.proof()):
                probe.image["Config"]["Labels"] = {key: "e" * 40}
                probe.item["Config"]["Labels"][key] = "e" * 40
                with self.assertRaisesRegex(GateError, "FRONTEND_UNKNOWN_OR_DRIFTED"):
                    probe.initial_legacy_frontend_identity()
                del probe.item["Config"]["Labels"][key]

    def test_native_frontend_still_requires_both_exact_oci_pins(self):
        probe = DockerFrontend()
        revision = "org.opencontainers.image.revision"
        tree = "io.tecnocontrol.git.tree"
        image_labels = probe.image["Config"]["Labels"] = {revision: probe.release["revision"]}
        live_labels = probe.item["Config"]["Labels"]
        live_labels.update(image_labels)
        with patch("gates.verify_private_frontend") as classify:
            with self.assertRaisesRegex(GateError, "WRONG_OCI_LABELS"):
                probe("frontend", "BLUE")
            image_labels[tree] = live_labels[tree] = probe.release["tree"]
            self.assertEqual(probe("frontend", "BLUE")["container_id"], probe.item["Id"])
            classify.assert_not_called()

    def test_original_source_must_still_be_active_for_initial_adoption(self):
        probe = DockerFrontend()
        with patch("gates.classify_historical", return_value=LEGACY_FRONTEND_IMAGE_EXACT) as classify:
            probe.verify_initial_legacy_frontend_source()
            probe.historical["State"]["Running"] = False
            with self.assertRaisesRegex(GateError, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
                probe.verify_initial_legacy_frontend_source()
            self.assertEqual(classify.call_count, 1)

    def test_two_manifest_sources_and_nonlegacy_blue_rejected(self):
        with self.assertRaisesRegex(GateError, "LEGACY_FRONTEND_MANIFEST_SOURCE_INVALID"):
            DockerFrontend(embedded=True)
        with self.assertRaisesRegex(GateError, "LEGACY_FRONTEND_SCOPE_INVALID"):
            DockerProbe({}, legacy_frontend_blue={}, legacy_frontend_blue_sha256=PIN)


class AdoptedFrontend(EpochProbe):
    def __init__(self, fixture, backend_manifest, release):
        super().__init__(fixture, backend_manifest)
        self.legacy_frontend_blue_sha256 = PIN
        self.legacy_frontend_blue = {"release": release}
        self.frontend_identity.update(revision=release["revision"], tree=release["tree"])
        self.source_active = True

    def verify_initial_legacy_frontend_source(self):
        if not self.source_active:
            raise GateError("LEGACY_FRONTEND_HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED")

    def _probe(self, role, slot, legacy_compose=None, *, with_epoch=False, legacy_frontend=False):
        return super()._probe(role, slot, legacy_compose, with_epoch=with_epoch)


class LegacyFrontendLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name) / "state")
        fixture = legacy_fixture()
        manifest = envelope(fixture)
        self.release = release_for(fixture[-1], fixtures("BLUE")[0]["images"]["frontend"])
        self.probe = AdoptedFrontend(fixture, manifest, self.release)
        self.wire = EpochWire()
        config = edge_config()
        config["apps"]["http"]["servers"]["redirect"]["routes"] = []
        self.config = gated_config(bind_legacy_blue(config, manifest, manifest["sha256"]))
        gate = ReadyGate(KEY)
        self.controller = Controller(self.store, self.wire, self.probe,
                                     lambda receipt, context: True if context["role"] == "api" else gate(receipt, context), None)
        self.cold = ColdStart(self.controller)
        self.cold.preview(self.config)

    def receipt(self, *, slot="BLUE", adoption=READY_ADOPTION):
        identity = self.probe.frontend_identity
        stamp = now()
        data = {"role": "frontend", "slot": slot, "release": self.release, "identity_hash": digest(identity),
                "started_at": identity["started_at"], "healthy_at": stamp, "ready_at": stamp,
                "private_route": True, "checks": FRONTEND_CHECKS}
        if adoption is not None:
            data["adoption"] = adoption
        return sign_receipt("READY", data, KEY)

    def initialize(self, receipt=None):
        return self.cold.initialize(self.config, self.release,
                                    {"api": True, "frontend": receipt or self.receipt()}, digest(self.config))

    def test_initial_state_seals_manifest_before_normal_recovery(self):
        state = self.initialize()
        self.assertEqual(state["legacy_blue"]["frontend_manifest_sha256"], PIN)
        self.probe.source_active = False
        self.assertTrue(self.cold.recover()["ingress_ready"])
        self.wire.live = deepcopy(BOOTSTRAP)
        self.probe.reboot()
        self.assertEqual(self.cold.recover()["legacy_blue_reattest"], "LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT")
        self.assertEqual(self.store.load()["legacy_blue"]["frontend_manifest_sha256"], PIN)

    def test_unscoped_ready_and_inactive_historical_source_fail_initial_adoption(self):
        with self.assertRaisesRegex(Stop, "READY_RECEIPT_REJECTED"):
            self.initialize(self.receipt(adoption=None))
        self.probe.source_active = False
        with self.assertRaisesRegex(Stop, "HISTORICAL_SOURCE_UNKNOWN_OR_DRIFTED"):
            self.initialize()
        self.assertIsNone(self.store.load(allow_missing=True))

    def test_wrong_pin_and_retired_state_fail_closed(self):
        state = self.initialize()
        self.cold.recover()
        self.probe.legacy_frontend_blue_sha256 = "b" * 64
        with self.assertRaisesRegex(Stop, "LEGACY_FRONTEND_ADOPTION_STATE_MISMATCH"):
            self.cold.recover()
        self.assertFalse(self.wire.admitted)
        state["slots"]["BLUE"]["phase"] = "RETIRED"
        with self.assertRaisesRegex(Stop, "LEGACY_FRONTEND_ADOPTION_RETIRED"):
            validate_state(state)

    def test_scoped_receipt_cannot_authorize_native_or_green(self):
        identity = self.probe.frontend_identity
        gate = ReadyGate(KEY)
        native = {"role": "frontend", "slot": "BLUE", "release": self.release,
                  "identity_hash": digest(identity), "started_at": identity["started_at"]}
        self.assertFalse(gate(self.receipt(), native))
        self.assertFalse(gate(self.receipt(slot="GREEN"), {**native, "slot": "GREEN", "adoption": READY_ADOPTION}))
        with patch.object(self.controller, "_identity", return_value=identity):
            with self.assertRaisesRegex(Stop, "LEGACY_FRONTEND_REQUIRES_INITIAL_ADOPTION"):
                self.controller._verified_ready("frontend", "BLUE", self.release, self.receipt())

    def test_native_ledger_cannot_activate_supplied_frontend_exception(self):
        from routectl import RouteConfig

        with self.assertRaisesRegex(Stop, "LEGACY_FRONTEND_ADOPTION_STATE_MISMATCH"):
            self.controller._require_legacy_binding({}, RouteConfig(edge_config()))

    def test_existing_legacy_retirement_requires_rebind(self):
        state = self.initialize()
        state["routing"]["active"] = {"api": "GREEN", "frontend": "GREEN"}
        state["slots"]["BLUE"]["phase"] = "STANDBY"
        with (patch.object(self.store, "load", return_value=state), patch.object(self.store, "save") as save,
              patch.object(self.controller, "_current"), patch.object(self.controller, "_verify_targets")):
            with self.assertRaisesRegex(Stop, "LEGACY_BLUE_RETIREMENT_REQUIRES_REBIND"):
                self.controller.retire("BLUE")
            save.assert_not_called()
