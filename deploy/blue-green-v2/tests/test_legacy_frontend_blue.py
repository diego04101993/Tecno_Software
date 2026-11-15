"""Qualification and manifest controls using an explicit non-production policy."""

from copy import deepcopy
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest

from legacy_frontend_blue import (HISTORICAL_COMMAND, LEGACY_FRONTEND_IMAGE_EXACT, PRIVATE_COMMAND, R3_POLICY,
                                  asset_paths, classify_historical, historical_evidence, historical_mounts,
                                  historical_ports, seal_manifest, sha, verify_manifest, verify_private_frontend)
from release_state import Stop, digest


IMAGE = "sha256:" + "1" * 64
HISTORICAL_ID = "2" * 64
PRIVATE_ID = "3" * 64
NETWORK_ID = "4" * 64
INDEX = b"<script src=/assets/app.js></script><link href='/assets/app.css' rel=stylesheet>"
HISTORICAL_CADDYFILE = b"historical Caddyfile fixture\n"
SLOT_CADDYFILE = b"slot Caddyfile fixture\n"
CADDY = "v2.11.4 h1:disposable"
PRIVATE_SOURCE = "/approved/deploy/Caddyfile.slot"
REPO_DIGEST = f"test/legacy@{IMAGE}"
ROLLBACK = f"services:\n  frontend:\n    image: {IMAGE}\n".encode()


class ReadFixture:
    def __init__(self):
        self.caddy = CADDY
        self.index = INDEX
        self.caddyfile = HISTORICAL_CADDYFILE
        self.slot_caddyfile = SLOT_CADDYFILE
        self.assets = {"/srv/assets/app.js": b"test script", "/srv/assets/app.css": b"test stylesheet"}
        self.command = HISTORICAL_COMMAND.copy()
        self.rollback = ROLLBACK
        self.calls = []

    def read(self, *args):
        self.calls.append(args)
        assert args[:2] in (("exec", HISTORICAL_ID), ("exec", PRIVATE_ID))
        if args[2:] == ("cat", "/proc/1/cmdline"):
            return ("\0".join(self.command) + "\0").encode()
        if args[-2] == "legacy-frontend-static":
            data = f"{self.caddy}\n{sha(self.caddyfile)}  /etc/caddy/Caddyfile\n"
            if args[-1] == "private":
                data += f"{sha(self.slot_caddyfile)}  /etc/caddy/Caddyfile.bgv2\n"
            return data.encode() + self.index
        assert args[5] == "legacy-frontend-assets"
        return b"".join(f"{sha(self.assets[path])}  {path}\n".encode() for path in args[6:] if path in self.assets)

    def read_host(self, path):
        assert path == "/approved/rollback.yml"
        return self.rollback


def fixtures():
    image = {"Id": IMAGE, "Os": "linux", "Architecture": "amd64", "RepoDigests": [REPO_DIGEST],
             "Config": {"Entrypoint": None, "Cmd": HISTORICAL_COMMAND.copy(), "Labels": None,
                        "Env": ["SECRET_KEY=should_not_be_persisted"]},
             "RootFS": {"Type": "layers", "Layers": ["sha256:" + "5" * 64]}}
    historical = {"Id": HISTORICAL_ID, "Image": IMAGE, "Name": "/fixture-legacy-frontend", "Path": "caddy",
                  "Args": HISTORICAL_COMMAND[1:], "Created": "2026-11-09T00:00:00Z", "RestartCount": 0,
                  "Config": {"Image": IMAGE, "Entrypoint": None, "Cmd": HISTORICAL_COMMAND.copy(),
                             "Labels": {"com.docker.compose.project": "fixture", "com.docker.compose.service": "frontend"}},
                  "HostConfig": {"NetworkMode": "edge"},
                  "Mounts": [{"Type": "volume", "Name": "historical-tls", "Source": "/docker/tls", "Destination": "/data", "RW": True}],
                  "NetworkSettings": {"Networks": {"edge": {"NetworkID": "6" * 64, "IPAddress": "172.19.0.2"},
                                                   "proxy": {"NetworkID": NETWORK_ID, "IPAddress": "172.30.250.2"}},
                                      "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}]}},
                  "State": {"Running": True, "Status": "running", "Pid": 3202, "OOMKilled": False,
                            "Dead": False, "Paused": False, "Restarting": False}}
    policy = replace(R3_POLICY, revision="a" * 40, tree="b" * 40, image_id=IMAGE, repo_digests=(REPO_DIGEST,),
                     image_config_sha256=digest(image["Config"]), historical_container_id=HISTORICAL_ID,
                     historical_config_sha256=digest(historical["Config"]), historical_caddyfile_sha256=sha(HISTORICAL_CADDYFILE),
                     index_sha256=sha(INDEX), caddy_version=CADDY, historical_mounts_sha256=digest(historical_mounts(historical)),
                     historical_ports_sha256=digest(historical_ports(historical)), historical_name="fixture-legacy-frontend",
                     historical_networks=(("edge", "6" * 64, "172.19.0.2"), ("proxy", NETWORK_ID, "172.30.250.2")),
                     historical_project="fixture", private_network_name="proxy", private_network_id=NETWORK_ID,
                     slot_caddyfile_sha256=sha(SLOT_CADDYFILE), rollback_source="/approved/rollback.yml",
                     rollback_source_sha256=sha(ROLLBACK), rollback_reference=REPO_DIGEST)
    return historical, image, policy


class LegacyFrontendManifestTests(unittest.TestCase):
    def setUp(self):
        self.historical, self.image, self.policy = fixtures()
        self.source = ReadFixture()
        self.contract = {"network_name": "proxy", "network_id": NETWORK_ID, "ipv4": "172.30.250.4",
                         "slot_caddyfile_sha256": sha(SLOT_CADDYFILE), "slot_caddyfile_source": PRIVATE_SOURCE}
        self.evidence = historical_evidence(self.historical, self.image, self.source.read, policy=self.policy,
                                            read_host=self.source.read_host)
        self.manifest = seal_manifest(self.evidence, self.contract, policy=self.policy)
        self.pin = self.manifest["sha256"]
        self.payload = verify_manifest(self.manifest, self.pin, policy=self.policy)
        self.binding = {"name": "fixture-frontend-blue", "image_id": IMAGE, "revision": self.policy.revision,
                        "tree": self.policy.tree, "network": "proxy", "ip": "172.30.250.4", "upstream": "frontend-blue:80",
                        "frontend_config_sha256": sha(SLOT_CADDYFILE)}
        self.private = {"Id": PRIVATE_ID, "Name": "/fixture-frontend-blue", "Image": IMAGE,
                        "Path": "caddy", "Args": PRIVATE_COMMAND[1:],
                        "Config": {"Image": IMAGE, "Entrypoint": None, "Cmd": PRIVATE_COMMAND.copy(),
                                   "Labels": {"io.tecnocontrol.bgv2.slot": "BLUE", "io.tecnocontrol.bgv2.role": "frontend"}},
                        "HostConfig": {"Tmpfs": {"/data": "ro", "/config": "ro"}, "PublishAllPorts": False},
                        "Mounts": [{"Source": PRIVATE_SOURCE, "Destination": "/etc/caddy/Caddyfile.bgv2", "Type": "bind", "RW": False}],
                        "NetworkSettings": {"Ports": {"80/tcp": None}, "Networks": {"proxy": {
                            "NetworkID": NETWORK_ID, "IPAddress": "172.30.250.4", "Aliases": ["frontend-blue"]}}}}
        self.network = {"Name": "proxy", "Id": NETWORK_ID, "Driver": "bridge", "Internal": True}

    def classify(self):
        return classify_historical(self.historical, self.image, self.source.read, self.manifest, self.pin,
                                   policy=self.policy, read_host=self.source.read_host)

    def check_private(self):
        return verify_private_frontend(self.private, self.image, self.network, self.source.read, self.payload,
                                       self.binding, policy=self.policy)

    def test_exact_unlabeled_history_and_exact_private_image_are_classified(self):
        self.assertEqual(self.classify(), LEGACY_FRONTEND_IMAGE_EXACT)
        self.assertEqual(self.check_private(), {"classification": LEGACY_FRONTEND_IMAGE_EXACT,
                                                "container_id": PRIVATE_ID, "image_id": IMAGE})
        self.assertEqual(self.payload["release"]["images"]["frontend"], IMAGE)
        self.assertEqual(self.payload["image"]["repo_digests"], [REPO_DIGEST])
        self.assertEqual(set(self.payload["content"]["asset_sha256"]), {"/assets/app.js", "/assets/app.css"})
        self.assertEqual(self.payload["private_contract"]["port"], 80)
        self.assertNotIn("SECRET_KEY", str(self.payload))

    def test_image_mutations_fail_closed(self):
        edits = {
            "image ID": lambda image: image.update(Id="sha256:" + "e" * 64),
            "RepoDigest": lambda image: image.update(RepoDigests=[f"other/repo@{IMAGE}"]),
            "image config": lambda image: image["Config"].update(User="other"),
            "entrypoint": lambda image: image["Config"].update(Entrypoint=["other"]),
            "command": lambda image: image["Config"].update(Cmd=["other"]),
            "architecture": lambda image: image.update(Architecture="arm64"),
            "rootfs layer": lambda image: image["RootFS"].update(Layers=["sha256:" + "e" * 64]),
        }
        original = deepcopy(self.image)
        for name, change in edits.items():
            with self.subTest(name=name):
                self.image = deepcopy(original)
                change(self.image)
                with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                    self.classify()
                with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                    self.check_private()

    def test_content_mutations_fail_closed(self):
        for field, replacement in (("caddy", "v2.11.5"), ("caddyfile", b"changed caddyfile"),
                                   ("index", INDEX + b"changed"), ("assets", {**self.source.assets, "/srv/assets/app.js": b"changed"}),
                                   ("assets", {"/srv/assets/app.css": b"test stylesheet"})):
            with self.subTest(field=field, replacement=replacement):
                self.source = ReadFixture()
                setattr(self.source, field, replacement)
                with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                    self.classify()
                with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                    self.check_private()
        self.source = ReadFixture()
        self.source.slot_caddyfile = b"unexpected slot config"
        with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
            self.check_private()

    def test_historical_mount_network_config_and_rollback_contract_are_exact(self):
        edits = {
            "mount": lambda old: old["Mounts"][0].update(Source="/wrong"),
            "network": lambda old: old["NetworkSettings"]["Networks"]["proxy"].update(Gateway="172.30.250.1"),
            "config": lambda old: old["Config"].update(WorkingDir="/wrong"),
            "retired": lambda old: old["State"].update(Running=False, Status="exited"),
        }
        original = deepcopy(self.historical)
        for name, change in edits.items():
            with self.subTest(name=name), self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                self.historical = deepcopy(original)
                change(self.historical)
                self.classify()
        self.historical = original
        self.source.rollback += b"# unexpectedly changed"
        with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
            self.classify()

    def test_private_port_tls_mount_network_and_wrong_slot_are_rejected(self):
        edits = {
            "mount": lambda item: item["Mounts"][0].update(RW=True),
            "public TLS": lambda item: item["Mounts"].append({"Destination": "/data", "Type": "volume", "RW": True}),
            "published": lambda item: item["NetworkSettings"]["Ports"].update({"80/tcp": [{"HostPort": "80"}]}),
            "network": lambda item: item["NetworkSettings"]["Networks"]["proxy"].update(IPAddress="172.30.250.7"),
            "second network": lambda item: item["NetworkSettings"]["Networks"].update(other={}),
            "GREEN": lambda item: item["Config"]["Labels"].update({"io.tecnocontrol.bgv2.slot": "GREEN"}),
            "historical container": lambda item: item.update(Id=HISTORICAL_ID),
        }
        original = deepcopy(self.private)
        for name, change in edits.items():
            with self.subTest(name=name), self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                self.private = deepcopy(original)
                change(self.private)
                self.check_private()

    def test_oci_revision_or_tree_present_with_wrong_value_blocks(self):
        for field in ("org.opencontainers.image.revision", "io.tecnocontrol.git.tree"):
            for value in ("f" * 40, None):
                with self.subTest(field=field, value=value):
                    self.private["Config"]["Labels"][field] = value
                    with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                        self.check_private()
                    del self.private["Config"]["Labels"][field]
                    self.image["Config"]["Labels"] = {field: value}
                    with self.assertRaisesRegex(Stop, "UNKNOWN_OR_DRIFTED"):
                        self.classify()
                    self.image["Config"]["Labels"] = None

    def test_manifest_corruption_wrong_checksum_and_non_r3_scope_fail(self):
        with self.assertRaisesRegex(Stop, "CHECKSUM"):
            verify_manifest(self.manifest, "e" * 64, policy=self.policy)
        changed = deepcopy(self.manifest)
        changed["payload"]["content"]["asset_sha256"].pop("/assets/app.js")
        with self.assertRaisesRegex(Stop, "CHECKSUM"):
            verify_manifest(changed, self.pin, policy=self.policy)
        changed["payload"]["scope"]["role"] = "GREEN"
        changed["sha256"] = digest(changed["payload"])
        with self.assertRaisesRegex(Stop, "SCOPE"):
            verify_manifest(changed, changed["sha256"], policy=self.policy)
        with self.assertRaisesRegex(Stop, "SCOPE"):
            verify_manifest(self.manifest, self.pin)

    def test_all_static_paths_are_parsed_and_noncanonical_paths_stop(self):
        self.assertEqual(asset_paths(INDEX + b'<img srcset="/assets/a.png 1x, /assets/b.png 2x">'),
                         ["/assets/a.png", "/assets/app.css", "/assets/app.js", "/assets/b.png"])
        for path in (b"../assets/app.js", b"/assets/../x.js", b"//example.org/assets/x.js", b"/assets/x.js?other"):
            with self.subTest(path=path), self.assertRaisesRegex(Stop, "ASSET_PATH_INVALID"):
                asset_paths(INDEX + b"<script src=" + path + b"></script>")
