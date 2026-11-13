"""Opt-in, disposable R3-shaped Edge handoff and reversal.

The fixture uses cached sealed stand-ins when the exact R3 image IDs are
unavailable. It never loads, builds or pulls images, or uses production data.
"""

import hashlib
import json
import math
import os
import re
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, PROJECT_DIRECTORY, RELEASES, DatabaseFixture, LocalDocker, wait_until
from qa_http import API_HOST, APP_HOST, QA_CADDY_CA, QA_CLIENT_CA, QAHTTP
from qa_topology import QAEdge, binding, mount_volume, wait_edge_bootstrap

from edge_config import bootstrap_config
from gates import BACKEND_ARGV, FRONTEND_ARGV, FRONTEND_CONFIG, DockerProbe, PlayerGate, ReadyGate, sign_receipt
from release_state import StateStore, canonical, digest, now
from routectl import Controller, RouteConfig, bind_legacy_blue


R3_COMMIT = "0a7d5854bee17a64f6dba868dc44f2c0bbceed3d"
R3_IMAGES = {
    "api": "sha256:951cd0b55ce3b461c361cb9b9d83c29d08e32f938cff4eefbbfb842f58ab0832",
    "frontend": "sha256:b2a1ae4a8515e9d1db4cb14744bba356311317c819a3a16b37c791d0257e905a",
}
R3_CADDY_SHA = "de1843bb8670c5d1c2fe9f67dbd987f2c8ce908e37863e51b54d7c8c38df862a"
PROXY_IP = "172.30.250.2"
BACKEND_IP = "172.30.250.3"
FRONTEND_IP = "172.30.250.4"
PREVIEW_IP = "172.30.250.5"
PREVIEW_CA = "/config/preview-tls/pki/authorities/local/root.crt"
PUBLIC_IP = "172.31.199.10"
MEDIA_PATH = "/app/storage/uploads/bgv2-handoff.txt"
MEDIA_CONTENT = b"disposable shared upload\n"
DISPOSABLE_PROJECT = "bgv2-r3-rehearsal"
TRIALS = 10
HANDOFF_LIMIT_SECONDS = 30


def historical_caddyfile():
    raw = subprocess.run(["git", "show", R3_COMMIT + ":frontend/Caddyfile.production"], cwd=PROJECT_DIRECTORY,
                         capture_output=True, check=True).stdout
    assert hashlib.sha256(raw).hexdigest() == R3_CADDY_SHA
    for site in (b"{$APP_DOMAIN} {", b"{$API_DOMAIN} {"):
        assert raw.count(site) == 1
        raw = raw.replace(site, site + b"\n    tls internal")
    return raw


def networks(docker, container):
    state = docker.inspect("container", container)["NetworkSettings"]["Networks"]
    return {name: (entry["IPAddress"], tuple(sorted(entry.get("Aliases") or ())))
            for name, entry in state.items()}


def connect(docker, network, container, ip):
    docker.run("network", "connect", "--ip", ip, network, container)


def release_endpoint(docker, network, container, ip):
    if network in networks(docker, container):
        docker.run("network", "disconnect", network, container)
    assert network not in networks(docker, container)
    endpoints = docker.inspect("network", network).get("Containers") or {}
    assert not any(item["IPv4Address"].split("/")[0] == ip for item in endpoints.values())


def wait_tcp(docker, container, port):
    wait_until(lambda: docker.execute(container, "nc", "-z", "-w", "1", "127.0.0.1", str(port)).returncode == 0)


def published(docker, container):
    values = docker.inspect("container", container)["NetworkSettings"]["Ports"]
    assert {key for key, value in values.items() if value} == {"80/tcp", "443/tcp"}
    assert all(values[key][0]["HostIp"] == "127.0.0.1" and len(values[key]) == 1
               for key in ("80/tcp", "443/tcp"))
    return {key: values[key][0]["HostPort"] for key in ("80/tcp", "443/tcp")}


def disposable_host_ports():
    # Choose high loopback ports and pin them in both container configs. An
    # ephemeral Docker publish can receive a different port after stop/start.
    with socket.socket() as http, socket.socket() as https:
        http.bind(("127.0.0.1", 0))
        https.bind(("127.0.0.1", 0))
        return http.getsockname()[1], https.getsockname()[1]


def assert_tls_mount(docker, volume, container):
    mounts = docker.inspect("container", container)["Mounts"]
    assert any(mount.get("Name") == volume and mount["Destination"] == "/data" and mount["RW"]
               for mount in mounts)


def cert_hashes(docker, container):
    hashes = docker.read(container, "sh", "-ec", "find /data/caddy -name '*.crt' -type f -exec sha256sum '{}' ';' | sort")
    assert all(f"{host}.crt" in hashes for host in (API_HOST, APP_HOST))
    return hashes


def cpu_memory(docker, containers):
    rows = {}
    for role, container in containers.items():
        raw = docker.read(container, "sh", "-ec", "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak "
                          "/sys/fs/cgroup/memory.max /sys/fs/cgroup/cpuset.cpus.effective; "
                          "awk '$1 == \"usage_usec\" {print $2}' /sys/fs/cgroup/cpu.stat")
        memory, peak, limit, cores, cpu = raw.splitlines()
        rows[role] = {"current_bytes": int(memory), "peak_bytes": int(peak), "limit_bytes": int(limit),
                      "cpuset": cores, "cpu_usage_usec": int(cpu)}
    return rows


def trusted_headers(docker, http, backend, client_ip, slot=None):
    marker = uuid4().hex
    spoof = "203.0.113.9"
    http.request(API_HOST, "/api/player/time?qa=" + marker, headers={
        "X-Forwarded-For": spoof, "X-Real-IP": spoof,
        "Forwarded": f"for={spoof};host=invalid.test;proto=http",
        "X-Forwarded-Host": "invalid.test", "X-Forwarded-Proto": "http",
    }, slot=slot).require(200)
    tagged = [line for line in docker.run("logs", backend).splitlines() if marker in line]
    assert len(tagged) == 1 and re.search(r"\b" + re.escape(client_ip) + r":\d+\s+-\s+", tagged[0])
    assert spoof not in tagged[0]


def service_smoke(docker, http, ingress):
    assert http.request(API_HOST, "/").require(200).json()["name"] == "R3-shaped disposable backend"
    index = http.request(APP_HOST, "/").require(200).body
    assert b"/assets/" in index
    assert http.request(API_HOST, "/media/bgv2-handoff.txt").require(200).body == MEDIA_CONTENT
    for host in (APP_HOST, API_HOST):
        headers = docker.read(http.client, "curl", "--noproxy", "*", "--silent", "--show-error", "--include",
                              "--max-time", "3", "--connect-to", f"{host}:80:{ingress}:80", f"http://{host}/?qa=1")
        assert headers.startswith("HTTP/1.1 308") and f"https://{host}/?qa=1" in headers
    return hashlib.sha256(index).hexdigest()


def service_probe(docker, client, address):
    return docker.execute(client, "curl", "--noproxy", "*", "--silent", "--fail", "--max-time", "1",
                          "--cacert", QA_CLIENT_CA, "--connect-to", f"{API_HOST}:443:{address}:443",
                          f"https://{API_HOST}/").returncode == 0


def first_http(http, edge):
    response = http.request(API_HOST, "/", timeout=2).require(200)
    assert response.json()["name"] == "R3-shaped disposable backend"
    assert ("x-request-id" in response.headers) is edge


def no_host_port_owner(ports):
    for port in ports.values():
        with socket.socket() as sock:
            sock.settimeout(0.2)
            assert sock.connect_ex(("127.0.0.1", int(port))) != 0


class Timeline:
    def __init__(self):
        self.monotonic_ns = {}
        self.utc = {}

    def mark(self, name):
        assert name not in self.monotonic_ns
        self.monotonic_ns[name] = time.monotonic_ns()
        self.utc[name] = now()

    def receipt(self):
        if not self.monotonic_ns:
            return {}
        start = next(iter(self.monotonic_ns.values()))
        return {name: {"utc": self.utc[name], "offset_seconds": round((at - start) / 1e9, 6)}
                for name, at in self.monotonic_ns.items()}

    def elapsed(self, first, last):
        return (self.monotonic_ns[last] - self.monotonic_ns[first]) / 1e9


class HandoffWire(QAEdge):
    def __init__(self, docker, relay, edge):
        super().__init__(docker, relay, edge)
        self.timeline = None

    def open_ingress(self, config_hash):
        if self.timeline is not None:
            self.timeline.mark("T_BLUE_BLUE_RECONCILED")
        super().open_ingress(config_hash)
        if self.timeline is not None:
            self.timeline.mark("T_INGRESS_READY")


class Handoff:
    def __init__(self, docker, directory):
        self.docker = docker
        self.directory = directory
        self.images = RELEASES["BLUE"]["images"]
        self.proxy = docker.create_network("proxy", internal=True, subnet="172.30.250.0/24")
        self.db_net = docker.create_network("app", internal=True)
        self.ingress = docker.create_network("ingress", subnet="172.31.199.0/24")
        self.uploads = docker.create_volume("uploads")
        self.tls = docker.create_volume("tls")
        self.old_config = docker.create_volume("r3-config")
        self.edge_config = docker.create_volume("edge-config")
        self.edge_admin = docker.create_volume("admin")
        self.db = DatabaseFixture(docker, self.db_net, self.uploads)

    def setup(self):
        docker = self.docker
        self.db.start()
        env = {"APP_ENV": "production", "APP_NAME": "R3-shaped disposable backend", "AUTO_SEED": "false",
               "DEBUG": "false", "WEB_CONCURRENCY": "1", "SECRET_KEY": self.db.jwt_secret,
               "DATABASE_URL": self.db.db_url, "CORS_ORIGINS": f"https://{APP_HOST}",
               "MEDIA_ROOT": "/app/storage/uploads", "FORWARDED_ALLOW_IPS": PROXY_IP}
        self.api = docker.create_container("legacy-api", self.images["api"], *BACKEND_ARGV, args=(
            "--network", self.proxy, "--ip", BACKEND_IP, "--network-alias", "backend",
            "--label", "com.docker.compose.project=" + DISPOSABLE_PROJECT,
            "--label", "com.docker.compose.service=backend", "--mount", mount_volume(self.uploads, "/app/storage/uploads"),
            "--health-interval", "1s", "--health-timeout", "2s", "--health-retries", "20", "--health-cmd",
            'python -c "import socket; socket.create_connection((\'127.0.0.1\',8000),1).close()"'),
            env=env, memory="768m")
        docker.run("network", "connect", self.db_net, self.api)
        docker.run("start", self.api)
        wait_until(lambda: docker.inspect("container", self.api)["State"]["Health"]["Status"] == "healthy")
        docker.read(self.api, "sh", "-ec", 'cat > "$1"', "media", MEDIA_PATH, input=MEDIA_CONTENT)

        slot_file = DEPLOY_DIRECTORY / "Caddyfile.slot"
        self.frontend = docker.create_container("frontend-blue", self.images["frontend"], *FRONTEND_ARGV, args=(
            "--network", self.proxy, "--ip", FRONTEND_IP, "--network-alias", "frontend-blue",
            "--label", "io.tecnocontrol.bgv2.slot=BLUE", "--label", "io.tecnocontrol.bgv2.role=frontend",
            "--mount", binding(slot_file, FRONTEND_CONFIG), "--tmpfs", "/config:ro", "--tmpfs", "/data:ro",
            "--health-interval", "1s", "--health-timeout", "2s", "--health-retries", "15",
            "--health-cmd", "nc -z -w 1 127.0.0.1 80"))
        docker.run("start", self.frontend)
        wait_until(lambda: docker.inspect("container", self.frontend)["State"]["Health"]["Status"] == "healthy")

        historical = self.directory / "Caddyfile.r3-qa"
        historical.write_bytes(historical_caddyfile())
        http_port, https_port = disposable_host_ports()
        self.old = docker.create_container("r3-frontend", self.images["frontend"],
            "caddy", "run", "--config", "/etc/caddy/Caddyfile.r3-qa", "--adapter", "caddyfile", args=(
                "--network", self.ingress, "--ip", PUBLIC_IP,
                "--publish", f"127.0.0.1:{http_port}:80", "--publish", f"127.0.0.1:{https_port}:443",
                "--mount", mount_volume(self.tls, "/data"), "--mount", mount_volume(self.old_config, "/config"),
                "--mount", binding(historical, "/etc/caddy/Caddyfile.r3-qa")),
            env={"APP_DOMAIN": APP_HOST, "API_DOMAIN": API_HOST}, memory="384m")
        connect(docker, self.proxy, self.old, PROXY_IP)
        docker.run("start", self.old)
        wait_tcp(docker, self.old, 80)
        wait_tcp(docker, self.old, 443)
        wait_until(lambda: docker.execute(self.old, "test", "-s", QA_CADDY_CA).returncode == 0)
        self.client = docker.create_container("client", CADDY_IMAGE, "sleep", "1800", args=("--network", self.ingress))
        docker.run("start", self.client)
        ca = (docker.read(self.old, "cat", QA_CADDY_CA) + "\n").encode()
        docker.read(self.client, "sh", "-ec", 'umask 077; cat > "$1"', "ca", QA_CLIENT_CA, input=ca)
        self.http = QAHTTP(docker, self.old, self.client, PUBLIC_IP)
        self.published_http = QAHTTP(docker, self.old, self.client, "host.docker.internal", public_port=https_port)

    def binding(self, role):
        item = self.docker.inspect("container", self.api if role == "api" else self.frontend)
        release = RELEASES["BLUE"]
        common = {"name": item["Name"].lstrip("/"), "network": self.proxy,
                  "image_id": self.images[role], "revision": release["revision"], "tree": release["tree"]}
        if role == "api":
            return {**common, "ip": BACKEND_IP, "upstream": BACKEND_IP + ":8000", "proxy_ip": PROXY_IP,
                    "uploads_volume": self.uploads, "postgres_id": self.db.pg,
                    "generated_mac_networks": sorted((self.db_net, self.proxy))}
        return {**common, "ip": FRONTEND_IP, "upstream": "frontend-blue:80",
                "generated_mac_networks": [self.proxy],
                "frontend_config_sha256": hashlib.sha256((DEPLOY_DIRECTORY / "Caddyfile.slot").read_bytes()).hexdigest()}

    def prepare_edge(self, ports, manifest):
        docker = self.docker
        self.edge = docker.create_container("edge", CADDY_IMAGE, "sh", "/opt/bgv2/edge-entrypoint.sh", args=(
                "--network", self.proxy, "--ip", PREVIEW_IP, "--publish", f"127.0.0.1:{ports['80/tcp']}:80",
                "--publish", f"127.0.0.1:{ports['443/tcp']}:443", "--mount", binding(DEPLOY_DIRECTORY, "/opt/bgv2"),
                "--mount", mount_volume(self.tls, "/data"), "--mount", mount_volume(self.edge_config, "/config"),
                "--mount", mount_volume(self.edge_admin, "/run/bgv2")), memory="384m")
        self.relay = docker.create_container("relay", self.images["api"], "sleep", "1800", args=(
            "--network", "none", "--mount", binding(DEPLOY_DIRECTORY, "/tooling"),
            "--mount", mount_volume(self.edge_config, "/config"), "--mount", mount_volume(self.edge_admin, "/run/bgv2")),
            env={"PYTHONPATH": "/tooling", "PYTHONDONTWRITEBYTECODE": "1"})
        docker.run("start", self.relay)
        docker.read(self.relay, "sh", "-ec", "mkdir -p /config/caddy; chmod 700 /config /config/caddy /run/bgv2")
        # Adapt using an owned client without adding a second live TLS writer.
        raw = (DEPLOY_DIRECTORY / "Caddyfile.edge").read_bytes()
        docker.read(self.client, "sh", "-ec", "cat > /tmp/bgv2.Caddyfile", input=raw)
        config = json.loads(docker.read(self.client, "caddy", "adapt", "--config", "/tmp/bgv2.Caddyfile",
                                       "--adapter", "caddyfile"))
        self.wire = HandoffWire(docker, self.relay, self.edge)
        self.edge_http = QAHTTP(docker, self.edge, self.client, PUBLIC_IP, preview_ca=PREVIEW_CA)
        return bootstrap_config(canonical(bind_legacy_blue(config, manifest, manifest["sha256"]))).config

    def probe(self, owner, manifest=None):
        context = "desktop-linux" if sys.platform == "win32" else "default"
        name = self.docker.inspect("container", owner)["Name"].lstrip("/")
        settings = {"edge_name": name}
        if manifest is not None:
            settings.update(legacy_blue=manifest, legacy_blue_sha256=manifest["sha256"])
        return DockerProbe({f"{role}:BLUE": self.binding(role) for role in ("api", "frontend")}, context, **settings)


def readonly_ready(fixture, probe, http, qa_token, key):
    """Use the actual candidate paths. Capacity/contracts inputs belong to this QA fixture."""
    from qa_product import ProductQA

    before = {role: probe(role, "BLUE") for role in ("api", "frontend")}
    checks = ProductQA(fixture, fixture.db, http)
    checks.admin = {"Authorization": "Bearer " + qa_token}
    probe.frontend_listener("BLUE", before["frontend"])
    healthy = now()
    api = {**checks.api("BLUE", expected_name="R3-shaped disposable backend"),
           "schema_unchanged": True, "media_unchanged": True,
           "phase3_infra_capacity": True, "pool": "ISOLATED_NOT_APPLICABLE",
           "contracts": {"windows_legacy": True, "android_normal": True}}
    front = checks.frontend("BLUE", route="BLUE")
    output = {}
    for role, values in (("api", api), ("frontend", front)):
        assert probe(role, "BLUE") == before[role]
        output[role] = sign_receipt("READY", {"role": role, "slot": "BLUE", "release": RELEASES["BLUE"],
            "identity_hash": digest(before[role]), "started_at": before[role]["started_at"], "healthy_at": healthy,
            "ready_at": now(), "private_route": True, "checks": values,
            **({"adoption": "LEGACY_BLUE"} if role == "api" else {})}, key)
    return output, checks.results


def prepare(fixture):
    from edge_boot import ColdStart
    from qa_integration import contract_evidence

    docker = fixture.docker
    fixture.setup()
    fixture.client_ip = networks(docker, fixture.client)[fixture.ingress][0]
    fixture.backend_identity = docker.identity(fixture.api)
    fixture.backend_networks = networks(docker, fixture.api)
    fixture.frontend_identity = docker.identity(fixture.frontend)
    fixture.pg_identity = docker.identity(fixture.db.pg)
    fixture.schema = fixture.db.signature()
    fixture.index = fixture.db.index_proof()
    fixture.old_identity = docker.identity(fixture.old)[0]
    fixture.old_networks = networks(docker, fixture.old)
    fixture.html = service_smoke(docker, fixture.http, PUBLIC_IP)
    first_http(fixture.published_http, edge=False)
    trusted_headers(docker, fixture.http, fixture.api, fixture.client_ip)
    fixture.ports = published(docker, fixture.old)
    assert_tls_mount(docker, fixture.tls, fixture.old)
    fixture.tls_fingerprint = cert_hashes(docker, fixture.old)
    fixture.historical_cfg = docker.read(fixture.old, "sha256sum", "/etc/caddy/Caddyfile.r3-qa")
    fixture.idle = cpu_memory(docker, {"legacy_api": fixture.api, "legacy_frontend": fixture.old,
                                       "private_frontend": fixture.frontend, "postgres": fixture.db.pg})
    assert docker.read(fixture.old, "curl", "--noproxy", "*", "--silent", "--fail",
                       f"http://{FRONTEND_IP}/").find("/assets/") >= 0
    fixture.token = fixture.http.request(API_HOST, "/api/auth/login", method="POST", body={
        "email": "operator@bgv2.example.com", "password": fixture.db.app_password}).require(200).json()["access_token"]
    fixture.manifest = fixture.probe(fixture.old).capture_legacy_blue(expected_project=DISPOSABLE_PROJECT,
                                                                    expected_service="backend")
    fixture.baseline = fixture.prepare_edge(fixture.ports, fixture.manifest)
    contract_evidence()
    fixture.key = secrets.token_bytes(48)
    fixture.pre_probe = fixture.probe(fixture.old, fixture.manifest)
    fixture.post_probe = fixture.probe(fixture.edge, fixture.manifest)
    fixture.store = StateStore(fixture.directory / "release-state")
    fixture.pre_controller = Controller(fixture.store, fixture.wire, fixture.pre_probe,
                                        ReadyGate(fixture.key), PlayerGate(fixture.key))
    fixture.controller = Controller(fixture.store, fixture.wire, fixture.post_probe,
                                    ReadyGate(fixture.key), PlayerGate(fixture.key))
    docker.run("start", fixture.edge)
    wait_edge_bootstrap(docker, fixture.edge)
    startup = ColdStart(fixture.pre_controller)
    startup.preview(fixture.baseline)
    wait_until(lambda: docker.execute(fixture.edge, "test", "-s", PREVIEW_CA).returncode == 0)
    assert fixture.db.signature() == fixture.schema and fixture.db.index_proof() == fixture.index
    assert fixture.edge_http.request(API_HOST, "/media/bgv2-handoff.txt", slot="BLUE").require(200).body == MEDIA_CONTENT
    active = cpu_memory(docker, {"legacy_api": fixture.api, "legacy_frontend": fixture.old, "edge": fixture.edge,
                                "private_frontend": fixture.frontend, "postgres": fixture.db.pg})
    assert all(item["cpuset"] == "0-3" and item["peak_bytes"] < item["limit_bytes"]
               for row in (fixture.idle, active) for item in row.values())
    assert max(sum(item["limit_bytes"] for item in row.values()) for row in (fixture.idle, active)) < 8_000_000_000
    fixture.capacity = {"before_edge_preview": fixture.idle, "edge_preview_and_legacy_ingress_running": active}
    receipts, fixture.perf = readonly_ready(fixture, fixture.pre_probe, fixture.edge_http, fixture.token, fixture.key)
    startup.initialize(fixture.baseline, RELEASES["BLUE"], receipts, digest(fixture.baseline))
    fixture.initial_commit_at = now()


def prepare_trial(fixture):
    from edge_boot import ColdStart, preview_config
    from qa_proxy_headers import _check_forwarded_policy

    docker = fixture.docker
    if not docker.inspect("container", fixture.edge)["State"]["Running"]:
        connect(docker, fixture.proxy, fixture.edge, PREVIEW_IP)
        docker.run("start", fixture.edge)
        wait_edge_bootstrap(docker, fixture.edge)
        # Reuse the same committed artifact. No second release initialization.
        config, etag = fixture.wire.raw_effective()
        from edge_boot import BOOTSTRAP
        assert config == BOOTSTRAP
        ColdStart(fixture.pre_controller)._load(preview_config(fixture.baseline), etag)
    assert not fixture.wire.ingress_open()
    assert not any(docker.inspect("container", fixture.edge)["NetworkSettings"]["Ports"].values())
    assert published(docker, fixture.old) == fixture.ports
    assert docker.identity(fixture.old)[0] == fixture.old_identity
    assert cert_hashes(docker, fixture.old) == fixture.tls_fingerprint
    assert cert_hashes(docker, fixture.edge) == fixture.tls_fingerprint
    assert docker.read(fixture.edge, "sha256sum", PREVIEW_CA).split()[0] != docker.read(
        fixture.old, "sha256sum", QA_CADDY_CA).split()[0]
    fixture.pre_probe.classify_legacy_blue()
    route = RouteConfig(fixture.baseline)
    _check_forwarded_policy(route, "api")
    _check_forwarded_policy(route, "frontend")
    assert fixture.wire.raw_effective()[0] == preview_config(fixture.baseline)
    assert fixture.edge_http.request(APP_HOST, "/", slot="BLUE").require(200).body
    fixture.edge_http.request(API_HOST, "/api/auth/me", headers={"Authorization": "Bearer " + fixture.token},
                              slot="BLUE").require(200)
    trusted_headers(docker, fixture.edge_http, fixture.api, PREVIEW_IP, slot="BLUE")
    trusted_headers(docker, fixture.http, fixture.api, fixture.client_ip)
    assert service_probe(docker, fixture.client, PUBLIC_IP)
    assert fixture.store.load()["routing"]["active"] == {"api": "BLUE", "frontend": "BLUE"}


def handoff(fixture, timeline, before_open=None, after_open=None):
    from edge_boot import ColdStart

    docker = fixture.docker
    assert networks(docker, fixture.old) == fixture.old_networks
    edge_identity = docker.identity(fixture.edge)
    timeline.mark("T_R3_STOP_COMMAND")
    docker.run("stop", "--time", "3", fixture.old)
    release_endpoint(docker, fixture.proxy, fixture.old, PROXY_IP)
    release_endpoint(docker, fixture.ingress, fixture.old, PUBLIC_IP)
    no_host_port_owner(fixture.ports)
    timeline.mark("T_80_443_RELEASED")
    release_endpoint(docker, fixture.proxy, fixture.edge, PREVIEW_IP)
    connect(docker, fixture.proxy, fixture.edge, PROXY_IP)
    assert networks(docker, fixture.edge)[fixture.proxy][0] == PROXY_IP
    timeline.mark("T_PROXY_IDENTITY_READY")
    connect(docker, fixture.ingress, fixture.edge, PUBLIC_IP)
    assert published(docker, fixture.edge) == fixture.ports
    timeline.mark("T_EDGE_OWNS_PORTS")
    assert docker.identity(fixture.edge) == edge_identity
    assert not docker.inspect("container", fixture.old)["State"]["Running"]
    assert_tls_mount(docker, fixture.tls, fixture.edge)
    timeline.mark("T_TLS_WRITER_READY")
    if before_open is not None:
        before_open(fixture, fixture.controller)
    fixture.wire.timeline = timeline
    try:
        assert ColdStart(fixture.controller).recover()["generation"] == 0
    finally:
        fixture.wire.timeline = None
    first_http(fixture.published_http, edge=True)
    timeline.mark("T_FIRST_PUBLIC_HTTP_200")
    if after_open is not None:
        after_open(fixture, fixture.controller, timeline)
    assert docker.identity(fixture.edge) == edge_identity
    assert service_smoke(docker, fixture.edge_http, PUBLIC_IP) == fixture.html
    trusted_headers(docker, fixture.edge_http, fixture.api, fixture.client_ip)
    assert cert_hashes(docker, fixture.edge) == fixture.tls_fingerprint
    assert_tls_mount(docker, fixture.tls, fixture.edge)
    assert fixture.controller.status()["routing"]["active"] == {"api": "BLUE", "frontend": "BLUE"}
    assert fixture.post_probe.classify_legacy_blue()[0] == "LIVE_BACKEND_IS_ADOPTED_LEGACY_BLUE_EXACT"


def reverse(fixture, timeline):
    docker = fixture.docker
    timeline.mark("T_ROLLBACK_DECISION")
    with fixture.wire.lock():
        fixture.wire.close_ingress()
    docker.run("stop", "--time", "3", fixture.edge)
    release_endpoint(docker, fixture.proxy, fixture.edge, PROXY_IP)
    release_endpoint(docker, fixture.ingress, fixture.edge, PUBLIC_IP)
    no_host_port_owner(fixture.ports)
    timeline.mark("T_EDGE_RELEASED")
    connect(docker, fixture.ingress, fixture.old, PUBLIC_IP)
    connect(docker, fixture.proxy, fixture.old, PROXY_IP)
    docker.run("start", fixture.old)
    timeline.mark("T_R3_START_RETURN")
    # The Caddy process is starting asynchronously; check TCP readiness and
    # retain it in the same timed interval, then issue exactly one HTTP probe.
    wait_tcp(docker, fixture.old, 443)
    first_http(fixture.published_http, edge=False)
    timeline.mark("T_FIRST_R3_HTTP_200")
    assert service_smoke(docker, fixture.http, PUBLIC_IP) == fixture.html
    assert published(docker, fixture.old) == fixture.ports
    trusted_headers(docker, fixture.http, fixture.api, fixture.client_ip)
    assert_tls_mount(docker, fixture.tls, fixture.old)
    assert cert_hashes(docker, fixture.old) == fixture.tls_fingerprint
    assert docker.identity(fixture.old)[0] == fixture.old_identity
    assert docker.read(fixture.old, "sha256sum", "/etc/caddy/Caddyfile.r3-qa") == fixture.historical_cfg
    assert networks(docker, fixture.old) == fixture.old_networks
    assert networks(docker, fixture.api) == fixture.backend_networks
    assert docker.identity(fixture.api) == fixture.backend_identity
    assert docker.identity(fixture.frontend) == fixture.frontend_identity and docker.identity(fixture.db.pg) == fixture.pg_identity
    assert fixture.db.signature() == fixture.schema and fixture.db.index_proof() == fixture.index


def summarize(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"min": round(ordered[0], 6), "p50": round(statistics.median(values), 6),
            "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 6), "max": round(ordered[-1], 6)}


def run(fixture, report):
    from qa_player_observer import Capture

    docker = fixture.docker
    prepare(fixture)
    report.update(scope="R3_SHAPED_DISPOSABLE_CACHED_4D320B4_STAND_INS",
                  r3_exact_images_present={role: docker.invoke("image", "inspect", image).returncode == 0
                                           for role, image in R3_IMAGES.items()},
                  readiness=fixture.perf, capacity=fixture.capacity)
    for index in range(1, TRIALS + 1):
        prepare_trial(fixture)
        forward, backward = Timeline(), Timeline()
        trial = {"trial": index, "T_BLUE_BLUE_COMMITTED": fixture.initial_commit_at}
        report["trials"].append(trial)
        capture_topology = SimpleNamespace(docker=docker, edge=fixture.edge, bindings={"api:BLUE": fixture.binding("api")})
        capture = Capture(capture_topology, 30, slot="BLUE", defer=True)

        def start_capture(_fixture, _controller):
            capture.start()
            forward.mark("T_PLAYER_CAPTURE_READY")

        try:
            handoff(fixture, forward, before_open=start_capture)
            trial["handoff_seconds"] = forward.elapsed("T_R3_STOP_COMMAND", "T_FIRST_PUBLIC_HTTP_200")
            assert trial["handoff_seconds"] <= HANDOFF_LIMIT_SECONDS, "HANDOFF_DOWNTIME_LIMIT_EXCEEDED"
            reverse(fixture, backward)
            trial["rollback_seconds"] = backward.elapsed("T_ROLLBACK_DECISION", "T_FIRST_R3_HTTP_200")
        finally:
            trial.update(handoff=forward.receipt(), rollback=backward.receipt())
            capture.close()
            docker.run("stop", "--time", "1", capture.container)
        print(f"TRIAL={index} HANDOFF_SECONDS={trial['handoff_seconds']:.6f} "
              f"ROLLBACK_SECONDS={trial['rollback_seconds']:.6f}", flush=True)
        assert trial["rollback_seconds"] <= HANDOFF_LIMIT_SECONDS, "ROLLBACK_DOWNTIME_LIMIT_EXCEEDED"
    report.update(result="PASS", port_80_443_ownership="PASS_DISPOSABLE_LOOPBACK_HOST_PORTS",
                  tls_single_writer="PASS_SEPARATE_PRIVATE_STORAGE", trusted_proxy_identity=PROXY_IP,
                  first_public_200_transport="STRICT_TLS_THROUGH_DOCKER_HOST_PUBLISHED_443",
                  same_edge_process_for_preparation_and_open=True, backend_and_postgres_preserved=True)
    if sys.argv[1:] == ["--phase3-observer"]:
        from qa_phase3_player import verify_prepared

        report["phase3_passive_blue_observer"] = {"result": "FAIL"}
        report["phase3_passive_blue_observer"] = verify_prepared(fixture, epoch=True)


def main():
    if not __debug__:
        raise RuntimeError("Disposable QA requires Python assertions enabled")
    docker = LocalDocker()
    scratch = PROJECT_DIRECTORY / ".tmp"
    scratch.mkdir(exist_ok=True)
    result = {"result": "FAIL", "trials": []}
    failure = None
    try:
        with tempfile.TemporaryDirectory(prefix="bgv22-handoff-", dir=scratch) as directory:
            run(Handoff(docker, Path(directory)), result)
    except Exception as exc:
        failure = exc
        if sys.argv[1:] == ["--phase3-observer"] and result["result"] == "PASS":
            result["phase3_passive_blue_observer"] = {"result": "FAIL", "error_class": type(exc).__name__}
        result["error_class"] = type(exc).__name__
    finally:
        try:
            docker.cleanup()
        except Exception as exc:
            result["cleanup"] = {"result": "FAIL", "error_class": type(exc).__name__}
            if failure is None:
                failure = exc
        else:
            result["cleanup"] = {"result": "PASS"}
    for metric in ("handoff_seconds", "rollback_seconds"):
        values = [trial[metric] for trial in result["trials"] if metric in trial]
        result[metric] = summarize(values)
        result[metric + "_count"] = len(values)
    output = scratch / f"bgv22-initial-handoff-{docker.nonce}.json"
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(f"HANDOFF_EVIDENCE={output}", file=sys.stderr, flush=True)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
