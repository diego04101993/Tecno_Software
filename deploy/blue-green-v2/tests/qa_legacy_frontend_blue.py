"""Exercise legacy frontend admission using a cached, unlabeled Caddy image."""

import hashlib
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from time import monotonic

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, LocalDocker, PROJECT_DIRECTORY, RELEASES, wait_until


OCI_KEYS = ("org.opencontainers.image.revision", "io.tecnocontrol.git.tree")
HISTORICAL_PROJECT = "bgv2-legacy-frontend-qa"
HISTORICAL_CONFIG = b"{\n\tadmin off\n\tauto_https off\n}\n:80 {\n\troot * /srv\n\tfile_server\n}\n"
INDEX = b'<html><link rel="stylesheet" href="/assets/qa.css"><script src="/assets/qa.js"></script></html>\n'
ASSETS = {"qa.css": b"body { color: black; }\n", "qa.js": b"console.log('legacy frontend QA');\n"}


def sha256(content):
    return hashlib.sha256(content).hexdigest()


class LegacyFrontendFixture:
    """Create only owned containers, volumes and networks on the local Engine."""

    def __init__(self, docker, directory):
        from qa_topology import choose_subnet

        self.docker = docker
        self.directory = directory
        self.image = docker.inspect("image", CADDY_IMAGE)
        assert self.image["Id"] == CADDY_IMAGE
        assert all(key not in (self.image["Config"].get("Labels") or {}) for key in OCI_KEYS)
        assert self.image["RepoDigests"], "Cached QA image must have a RepoDigest"
        subnet = choose_subnet(docker)
        self.subnet_prefix = subnet.rsplit(".", 1)[0]
        self.proxy = docker.create_network("legacy-front-proxy", internal=True, subnet=subnet)
        self.ingress = docker.create_network("legacy-front-ingress")
        self.data = docker.create_volume("legacy-front-data")
        self.config = docker.create_volume("legacy-front-config")
        self.fixture_files = {"srv/index.html": INDEX, "etc/caddy/Caddyfile": HISTORICAL_CONFIG,
                              **{f"srv/assets/{name}": body for name, body in ASSETS.items()}}

    def _copy_files(self, container, files):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path, content in files.items():
                entry = tarfile.TarInfo(path)
                entry.size = len(content)
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(content))
        self.docker.run("cp", "-", f"{container}:/", input=archive.getvalue())

    def _copy_content(self, container):
        item = self.docker.inspect("container", container)
        assert item["Id"] == container and item["Image"] == self.image["Id"]
        assert item["State"]["Running"] is False
        assert not any(entry["Destination"] == "/srv" for entry in item["Mounts"])
        self._copy_files(container, self.fixture_files)

    def read(self, *args):
        result = self.docker.invoke(*args)
        if result.returncode:
            raise RuntimeError("Disposable legacy frontend read-only inspection failed")
        return result.stdout

    def start_historical(self):
        from qa_topology import mount_volume

        self.historical = self.docker.create_container(
            "legacy-front-historical", self.image["Id"], args=(
                "--network", self.ingress,
                "--label", "com.docker.compose.project=" + HISTORICAL_PROJECT,
                "--label", "com.docker.compose.service=frontend",
                "--mount", mount_volume(self.data, "/data"),
                "--mount", mount_volume(self.config, "/config"),
                "--publish", "127.0.0.1::80", "--publish", "127.0.0.1::443",
            ),
        )
        self.docker.run("network", "connect", "--ip", self.subnet_prefix + ".2", self.proxy, self.historical)
        self._copy_content(self.historical)
        self.docker.run("start", self.historical)
        wait_until(lambda: self.docker.execute(self.historical, "nc", "-z", "-w", "1", "127.0.0.1", "80").returncode == 0)
        return self.docker.inspect("container", self.historical)

    def start_private(self, image_id):
        from qa_topology import binding

        assert image_id == self.image["Id"], "Creation requires the exact qualified fixture image ID"
        self.private_ip = self.subnet_prefix + ".4"
        self.private = self.docker.create_container(
            "legacy-front-blue", image_id,
            "caddy", "run", "--config", "/etc/caddy/Caddyfile.bgv2", "--adapter", "caddyfile",
            args=("--network", self.proxy, "--ip", self.private_ip, "--network-alias", "frontend-blue",
                  "--label", "io.tecnocontrol.bgv2.slot=BLUE", "--label", "io.tecnocontrol.bgv2.role=frontend",
                  "--mount", binding(DEPLOY_DIRECTORY / "Caddyfile.slot", "/etc/caddy/Caddyfile.bgv2"),
                  "--tmpfs", "/config:ro", "--tmpfs", "/data:ro", "--health-interval", "1s",
                  "--health-timeout", "2s", "--health-retries", "15",
                  "--health-cmd", "nc -z -w 1 127.0.0.1 80"),
        )
        self._copy_content(self.private)
        self.docker.run("start", self.private)
        wait_until(lambda: self.docker.inspect("container", self.private)["State"]["Health"]["Status"] == "healthy")
        return self.docker.inspect("container", self.private)


def verify(fixture):
    from legacy_frontend_blue import (LEGACY_FRONTEND_IMAGE_EXACT, R3_POLICY, classify_historical, historical_evidence,
                                      historical_mounts, historical_ports, seal_manifest, verify_manifest,
                                      verify_private_frontend)
    from release_state import Stop, canonical, digest

    docker = fixture.docker
    historical = fixture.start_historical()
    image = fixture.image
    network = docker.inspect("network", fixture.proxy)
    slot_file = DEPLOY_DIRECTORY / "Caddyfile.slot"
    release = RELEASES["BLUE"]
    private_ip = fixture.subnet_prefix + ".4"
    rollback_source = fixture.directory / "rollback.json"
    rollback_reference = image["RepoDigests"][0]
    rollback_source.write_text(json.dumps({"services": {"frontend": {"image": image["Id"]}}}), encoding="utf-8")
    network_contract = tuple(sorted((name, endpoint["NetworkID"], endpoint["IPAddress"])
                                    for name, endpoint in historical["NetworkSettings"]["Networks"].items()))
    policy = replace(
        R3_POLICY, revision=release["revision"], tree=release["tree"], backend_image_id=release["images"]["api"],
        image_id=image["Id"], repo_digests=tuple(sorted(image["RepoDigests"])), image_config_sha256=digest(image["Config"]),
        historical_container_id=historical["Id"], historical_config_sha256=digest(historical["Config"]),
        historical_caddyfile_sha256=sha256(HISTORICAL_CONFIG), index_sha256=sha256(INDEX),
        caddy_version=docker.read(historical["Id"], "caddy", "version"),
        historical_mounts_sha256=digest(historical_mounts(historical)),
        historical_ports_sha256=digest(historical_ports(historical)),
        historical_networks=network_contract, historical_name=historical["Name"].lstrip("/"),
        historical_project=HISTORICAL_PROJECT,
        private_network_name=fixture.proxy, private_network_id=network["Id"],
        slot_caddyfile_sha256=sha256(slot_file.read_bytes()), rollback_source=str(rollback_source),
        rollback_source_sha256=sha256(rollback_source.read_bytes()), rollback_reference=rollback_reference,
    )
    evidence = historical_evidence(historical, image, fixture.read, policy=policy)
    private_contract = {"network_name": fixture.proxy, "network_id": network["Id"], "ipv4": private_ip,
                        "slot_caddyfile_sha256": policy.slot_caddyfile_sha256, "slot_caddyfile_source": str(slot_file)}
    manifest = seal_manifest(evidence, private_contract, policy=policy)
    pin = manifest["sha256"]
    payload = verify_manifest(manifest, pin, policy=policy)
    classification = classify_historical(historical, image, fixture.read, manifest, pin, policy=policy)
    assert classification == LEGACY_FRONTEND_IMAGE_EXACT
    private = fixture.start_private(payload["release"]["images"]["frontend"])
    slot = {"name": private["Name"].lstrip("/"), "image_id": image["Id"], "revision": policy.revision,
            "tree": policy.tree, "network": fixture.proxy, "ip": private_ip, "upstream": "frontend-blue:80",
            "frontend_config_sha256": policy.slot_caddyfile_sha256}
    for path, expected in {"/": INDEX, **{f"/assets/{name}": body for name, body in ASSETS.items()}}.items():
        result = docker.execute(private["Id"], "curl", "--noproxy", "*", "--silent", "--show-error", "--fail",
                                "http://127.0.0.1" + path)
        assert result.returncode == 0 and result.stdout == expected
    started = monotonic()
    proof = verify_private_frontend(private, image, network, fixture.read, payload, slot, policy=policy)
    elapsed = monotonic() - started
    assert proof["classification"] == LEGACY_FRONTEND_IMAGE_EXACT
    probe_seconds = verify_runtime_admission(fixture, policy, manifest, slot)
    # A post-seal mutation in an owned QA container must not qualify.
    fixture._copy_files(private["Id"], {"srv/assets/qa.js": b"changed QA asset\n"})
    try:
        verify_private_frontend(private, image, network, fixture.read, payload, slot, policy=policy)
    except Stop:
        pass
    else:
        raise AssertionError("Changed referenced asset incorrectly qualified")
    manifest_file = canonical(manifest) + b"\n"
    return {"result": "PASS", "scope": "DISPOSABLE_CACHED_UNLABELED_CADDY_NOT_R3",
            "classification": proof["classification"], "fixture_image_id": image["Id"],
            "exact_image_creation": True, "referenced_assets": len(ASSETS), "private_content_http": 200,
            "asset_drift_rejected": True, "private_verification_seconds": round(elapsed, 6),
            "docker_probe_controller_admission": "PASS_REAL_FRONTEND_WITH_SYNTHETIC_API_AND_EDGE_WIRE",
            "native_missing_oci_rejected": True,
            "docker_probe_seconds": round(probe_seconds, 6),
            "manifest_payload_sha256": pin, "manifest_file_sha256": sha256(manifest_file)}, manifest_file


def verify_runtime_admission(fixture, policy, manifest, frontend_binding):
    """Exercise real frontend probing through the controller and isolated API evidence."""
    from edge_boot import ColdStart, gated_config
    from gates import DockerProbe, GateError, ReadyGate, sign_receipt
    from legacy_frontend_blue import READY_ADOPTION
    from release_state import StateStore, digest, now
    from routectl import BLUE_PAIR, Controller, bind_legacy_blue
    from test_legacy_blue import envelope, legacy_fixture, payload
    from test_legacy_epoch import EpochWire
    from test_routectl import edge_config

    historical = fixture.docker.inspect("container", fixture.historical)
    legacy_api = legacy_fixture()
    api_item, api_image, _, _, api_binding = legacy_api
    api_item["Image"] = api_image["Id"] = api_binding["image_id"] = policy.backend_image_id
    api_binding.update(revision=policy.revision, tree=policy.tree)
    approved_api = envelope(legacy_api)

    class FrontendRuntimeProbe(DockerProbe):
        def _host_boot_id(self, container, observed=None):
            # The fixture API identity is synthetic. The real Docker frontend
            # shares this QA-only kernel epoch for Controller admission.
            return approved_api["payload"]["runtime_epoch"]["boot_id"]

        def legacy_engine_boot_id(self):
            return self._host_boot_id(None)

        def _probe(self, role, slot, legacy_compose=None, **options):
            if role == "api":
                assert slot == "BLUE" and legacy_compose == approved_api["payload"]["compose"] and not options
                return payload(legacy_api)
            return super()._probe(role, slot, legacy_compose, **options)

        def _postgres(self):
            return api_binding["postgres_id"]

    context = "desktop-linux" if sys.platform == "win32" else "default"
    probe = FrontendRuntimeProbe(
        {"api:BLUE": api_binding, "frontend:BLUE": frontend_binding},
        context=context, edge_name=historical["Name"].lstrip("/"),
        legacy_blue=approved_api, legacy_blue_sha256=approved_api["sha256"], legacy_frontend_policy=policy,
        legacy_frontend_blue=manifest, legacy_frontend_blue_sha256=manifest["sha256"],
    )
    try:
        probe("frontend", "BLUE")
    except GateError as exc:
        assert str(exc) == "WRONG_OCI_LABELS"
    else:
        raise AssertionError("Native frontend probe accepted unlabeled fixture")
    started = monotonic()
    identities = {"api": probe("api", "BLUE"), "frontend": probe.initial_legacy_frontend_identity()}
    probe_seconds = monotonic() - started
    probe.frontend_listener("BLUE", identities["frontend"], initial_legacy=True)

    api_checks = {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                  "channels_elapsed_ns": 1, "runtime_serial_ok": 36, "runtime_concurrent_ok": 16,
                  "runtime_clients": 4, "runtime_transport_timeout_s": 10, "phase3_infra_capacity": True,
                  "schema_unchanged": True, "media_unchanged": True, "pool": "ISOLATED_NOT_APPLICABLE",
                  "contracts": {"windows_legacy": True, "android_normal": True}}
    front_checks = {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}
    key = bytes(range(48))
    release = manifest["payload"]["release"]
    receipts = {}
    for role, checks, adoption in (("api", api_checks, "LEGACY_BLUE"), ("frontend", front_checks, READY_ADOPTION)):
        identity = identities[role]
        receipts[role] = sign_receipt("READY", {
            "role": role, "slot": "BLUE", "release": release, "identity_hash": digest(identity),
            "started_at": identity["started_at"], "healthy_at": now(), "ready_at": now(),
            "adoption": adoption, "private_route": True, "checks": checks,
        }, key)
    config = edge_config()
    config["apps"]["http"]["servers"]["redirect"]["routes"] = []
    config = gated_config(bind_legacy_blue(config, approved_api, approved_api["sha256"]))
    store = StateStore(fixture.directory / "state")
    wire = EpochWire()
    cold = ColdStart(Controller(store, wire, probe, ReadyGate(key), None))
    cold.preview(config)
    state = cold.initialize(config, release, receipts, digest(config))
    assert state["legacy_blue"]["frontend_manifest_sha256"] == manifest["sha256"]
    assert cold.recover()["generation"] == 0 and wire.ingress_open()
    assert cold.controller.status()["routing"]["active"] == BLUE_PAIR
    return probe_seconds


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires assertions enabled")
    docker = LocalDocker()
    try:
        with tempfile.TemporaryDirectory(prefix="bgv23-legacy-frontend-", dir=PROJECT_DIRECTORY / ".tmp") as root:
            result, manifest = verify(LegacyFrontendFixture(docker, Path(root)))
    finally:
        docker.cleanup()
    evidence = PROJECT_DIRECTORY / ".tmp" / f"bgv23-qa-legacy-frontend-{docker.nonce}"
    evidence.mkdir(mode=0o700)
    with (evidence / "legacy-frontend-blue-manifest.json").open("xb") as stream:
        stream.write(manifest)
    report = json.dumps({**result, "owned_cleanup": "PASS"}, sort_keys=True) + "\n"
    with (evidence / "qa-result.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(report)
    print(report, end="", flush=True)
    print(f"QA_EVIDENCE={evidence}", flush=True)


if __name__ == "__main__":
    main()
