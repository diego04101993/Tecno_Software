"""Instantiate the V2 Caddy templates in a private Docker Desktop fixture."""

import base64
import hashlib
import json
import sys
from pathlib import Path

from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, RELEASES, wait_until
from qa_http import API_HOST, APP_HOST, QA_CADDY_CA, QA_CLIENT_CA, QAHTTP

sys.path.insert(0, str(DEPLOY_DIRECTORY))

from gates import BACKEND_ARGV, FRONTEND_ARGV, FRONTEND_CONFIG, DockerProbe
from routectl import CaddyEdge


def binding(source, destination):
    return f"type=bind,source={Path(source).resolve()},target={destination},readonly"


def mount_volume(source, destination):
    return f"type=volume,source={source},target={destination}"


def wait_edge_bootstrap(docker, container):
    def probe():
        state = docker.inspect("container", container)["State"]
        if state["Running"] is not True:
            raise RuntimeError(f"Disposable Edge exited before bootstrap, status {state['Status']}, exit {state['ExitCode']}")
        return docker.execute(container, "test", "-S", "/run/bgv2/admin.sock").returncode == 0

    wait_until(probe)


def choose_subnet(docker):
    from ipaddress import IPv4Network

    network_ids = docker.run("network", "ls", "-q").splitlines()
    networks = json.loads(docker.run("network", "inspect", *network_ids))
    used = [IPv4Network(item["Subnet"], strict=False) for network in networks
            for item in network["IPAM"].get("Config") or [] if ":" not in item.get("Subnet", "")]
    for third in range(224, 255):
        subnet = IPv4Network(f"172.30.{third}.0/24")
        if all(not subnet.overlaps(other) for other in used):
            return str(subnet)
    raise RuntimeError("No isolated Docker subnet for QA")


class Topology:
    def __init__(self, docker):
        self.docker = docker
        self.subnet = choose_subnet(docker)
        self.prefix = self.subnet.rsplit(".", 1)[0]
        self.proxy = docker.create_network("proxy", internal=True, subnet=self.subnet)
        self.app_net = docker.create_network("app", internal=True)
        self.edge_net = docker.create_network("edge")
        self.uploads = docker.create_volume("uploads")
        self.socket_volume = docker.create_volume("admin")
        self.config_volume = docker.create_volume("config")
        self.tls_volume = docker.create_volume("tls")
        self.sealed_volume = docker.create_volume("sealed")
        self.bindings = {}
        self.containers = {}
        self.config = DEPLOY_DIRECTORY / "Caddyfile.edge"
        self.slot_config = DEPLOY_DIRECTORY / "Caddyfile.slot"

    def add_slot(self, slot, db):
        release = RELEASES[slot]
        offset = 3 if slot == "BLUE" else 5
        for role, index in (("api", offset), ("frontend", offset + 1)):
            lower = slot.lower()
            alias = f"{'backend' if role == 'api' else 'frontend'}-{lower}"
            name = f"{self.docker.name}-{alias}"
            image = release["images"][role]
            ip = self.prefix + f".{index}"
            args = ["--network", self.proxy, "--network-alias", alias, "--ip", ip,
                    "--label", f"io.tecnocontrol.bgv2.slot={slot}",
                    "--label", f"io.tecnocontrol.bgv2.role={role}", "--health-interval", "1s",
                    "--health-timeout", "2s", "--health-retries", "15"]
            config = {"name": name, "network": self.proxy, "ip": ip, "image_id": image,
                      "revision": release["revision"], "tree": release["tree"],
                      "upstream": f"{alias}:{8000 if role == 'api' else 80}"}
            if role == "api":
                args += ["--mount", mount_volume(self.uploads, "/app/storage/uploads"), "--health-cmd",
                         'python -c "import socket; socket.create_connection((\'127.0.0.1\',8000),1).close()"']
                env = {"APP_NAME": f"BGV2 QA {slot}", "APP_ENV": "production", "AUTO_SEED": "false",
                       "DEBUG": "false", "WEB_CONCURRENCY": "1", "SECRET_KEY": db.jwt_secret,
                       "DATABASE_URL": db.db_url, "CORS_ORIGINS": f"https://{APP_HOST}",
                       "MEDIA_ROOT": "/app/storage/uploads", "FORWARDED_ALLOW_IPS": self.prefix + ".2"}
                command = BACKEND_ARGV
                config.update(proxy_ip=self.prefix + ".2", uploads_volume=self.uploads)
            else:
                args += ["--mount", binding(self.slot_config, FRONTEND_CONFIG),
                         "--tmpfs", "/config:ro", "--tmpfs", "/data:ro",
                         "--health-cmd", "nc -z -w 1 127.0.0.1 80"]
                env = {}
                command = FRONTEND_ARGV
                config.update(frontend_config_sha256=hashlib.sha256(self.slot_config.read_bytes()).hexdigest())
            identity = self.docker.create_container(alias, image, *command, args=args, env=env, name=name,
                                                    memory="768m" if role == "api" else "256m")
            if role == "api":
                self.docker.run("network", "connect", self.app_net, identity)
            self.docker.run("start", identity)
            self.containers[role, slot] = identity
            self.bindings[f"{role}:{slot}"] = config
        self.wait_health(slot)

    def wait_health(self, slot):
        for role in ("api", "frontend"):
            wait_until(lambda: self.docker.inspect("container", self.containers[role, slot])[
                "State"]["Health"]["Status"] == "healthy")

    def probe(self):
        return DockerProbe(self.bindings, "desktop-linux" if sys.platform == "win32" else "default",
                           edge_name=f"{self.docker.name}-edge")

    def start_edge(self):
        from edge_boot import ColdStart
        from release_state import StateStore
        from routectl import Controller
        import tempfile

        args = ("--network", self.edge_net, "--publish", "127.0.0.1::443",
                "--mount", binding(DEPLOY_DIRECTORY, "/opt/bgv2"),
                "--mount", mount_volume(self.socket_volume, "/run/bgv2"),
                "--mount", mount_volume(self.config_volume, "/config"),
                "--mount", mount_volume(self.tls_volume, "/data"))
        self.edge = self.docker.create_container("edge", CADDY_IMAGE, "sh", "/opt/bgv2/edge-entrypoint.sh", args=args,
            env={"BGV2_TLS": "internal", "BGV2_API_HOST": API_HOST, "BGV2_APP_HOST": APP_HOST},
            memory="512m")
        self.docker.run("network", "connect", "--ip", self.prefix + ".2", self.proxy, self.edge)
        self.relay = self.start_relay()
        self.docker.read(self.relay.relay, "sh", "-ec", "mkdir -p /config/caddy; chmod 700 /run/bgv2 /config /config/caddy /sealed")
        self.docker.run("start", self.edge)
        self.wait_bootstrap()
        adapted = self.docker.read(self.edge, "caddy", "adapt", "--config", "/opt/bgv2/Caddyfile.edge",
                                   "--adapter", "caddyfile")
        self.docker.read(self.relay.relay, "chmod", "700", "/sealed")
        baseline_receipt = self.docker.read(self.relay.relay, "python", "-B", "/tooling/edge_config.py",
            "--config-dir", "/config", "--baseline", "/sealed/edge-baseline.json", input=adapted.encode())
        self.baseline_hash = baseline_receipt.removeprefix("EDGE_BASELINE_SHA256=")
        self.baseline = json.loads(self.docker.read(self.relay.relay, "cat", "/sealed/edge-baseline.json"))
        self.public_port = int(self.docker.run("port", self.edge, "443/tcp").rsplit(":", 1)[1])
        with tempfile.TemporaryDirectory(prefix="bgv2-preview-", dir=DEPLOY_DIRECTORY.parents[1] / ".tmp") as directory:
            stage = Controller(StateStore(Path(directory) / "state"), self.relay, None, None, None)
            ColdStart(stage).preview(self.baseline)
        edge_settings = self.docker.inspect("container", self.edge)["NetworkSettings"]["Networks"]
        self.edge_address = edge_settings[self.edge_net]["IPAddress"]
        client_args = ("--network", self.edge_net)
        self.client = self.docker.create_container("client", CADDY_IMAGE, "sleep", "900", args=client_args)
        self.docker.run("start", self.client)
        self.linux_client = self.docker.create_container("websocket-client", RELEASES["GREEN"]["images"]["api"],
            "sleep", "900", args=(*client_args, "--mount", binding(DEPLOY_DIRECTORY, "/tooling")))
        self.docker.run("start", self.linux_client)
        ca = (self.docker.read(self.edge, "cat", QA_CADDY_CA) + "\n").encode()
        for client in (self.client, self.linux_client):
            self.docker.read(client, "sh", "-ec", 'umask 077; cat > "$1"', "qa-ca", QA_CLIENT_CA, input=ca)
        self.client_ip = self.docker.inspect("container", self.client)["NetworkSettings"]["Networks"][
            self.edge_net]["IPAddress"]
        return QAHTTP(self.docker, self.edge, self.client, self.edge_address)

    def wait_bootstrap(self):
        wait_edge_bootstrap(self.docker, self.edge)

    def wait_edge(self):
        wait_until(lambda: self.docker.execute(self.edge, "test", "-s", QA_CADDY_CA).returncode == 0)
        wait_until(lambda: self.docker.execute(self.edge, "nc", "-z", "-w", "1",
                                               "127.0.0.1", "443").returncode == 0)

    def check_isolation(self):
        edge = self.docker.inspect("container", self.edge)
        published = {key: value for key, value in edge["NetworkSettings"]["Ports"].items() if value}
        assert published == {"443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.public_port)}]}
        assert self.docker.execute(self.edge, "nc", "-z", "-w", "1", "127.0.0.1", "2019").returncode != 0
        for role, slot in self.containers:
            self.probe()(role, slot)
        ports = self.docker.read(self.edge, "cat", "/proc/net/tcp")
        for port in (8443, 8444):
            match = [line for line in ports.splitlines() if f":{port:04X}" in line.split()[1]]
            assert len(match) == 1 and match[0].split()[1].startswith("0100007F:")
            assert self.docker.execute(self.client, "nc", "-z", "-w", "1",
                                       self.edge_address, str(port)).returncode != 0
        return True

    def start_relay(self):
        relay = self.docker.create_container("admin-relay", RELEASES["GREEN"]["images"]["api"],
            "sleep", "900", args=("--network", "none", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "PYTHONPATH=/tooling", "--mount", binding(DEPLOY_DIRECTORY, "/tooling"),
                "--mount", mount_volume(self.socket_volume, "/run/bgv2"),
                "--mount", mount_volume(self.config_volume, "/config"),
                "--mount", mount_volume(self.sealed_volume, "/sealed")))
        self.docker.run("start", relay)
        return QAEdge(self.docker, relay, self.edge)


class QAEdge(CaddyEdge):
    """Delegate wire operations only, keeping controller logic unchanged."""

    def __init__(self, docker, relay, edge):
        super().__init__()
        self.docker = docker
        self.relay = relay
        self.edge = edge
        self.patch_count = 0

    def _relay(self, method, etag=None, config=None):
        data = json.dumps({"method": method, "etag": etag, "config": config}).encode()
        result = self.docker.execute(self.relay, "python", "/tooling/tests/qa_admin_relay.py", input=data)
        if result.returncode:
            # No config or response payload reaches stderr of the outer QA runner.
            code = result.stderr.decode("utf-8", errors="replace").splitlines()[-1].split(":")[0]
            raise RuntimeError(f"Disposable admin relay failed with {code}")
        return json.loads(result.stdout)

    def request(self, method, etag=None, config=None):
        if method == "PATCH":
            self.patch_count += 1
        result = self._relay(method, etag, config)
        return result["status"], result["etag"], base64.b64decode(result["body"])

    def read_autosave(self):
        result = self._relay("AUTOSAVE")
        if result.get("missing"):
            raise FileNotFoundError("Disposable Edge autosave missing")
        return base64.b64decode(result["body"])

    def close_ingress(self):
        self._relay("INGRESS_CLOSE")

    def open_ingress(self, config_hash):
        self._relay("INGRESS_OPEN", config=config_hash)

    def ingress_open(self):
        return self._relay("INGRESS_STATUS")["open"]
