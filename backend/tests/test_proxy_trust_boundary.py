"""R2A: Compose contracts and opt-in real Caddy/Uvicorn boundary tests.

Run: python -B -m unittest discover -s backend/tests -p test_proxy_trust_boundary.py -v
Real containers: set SEC1H_PROXY_BACKEND_IMAGE to an EXISTING local image with
FastAPI 0.115.0 / Uvicorn 0.32.0 / websockets. No pull, build, application startup,
DB, repository .env, production host, published ports or repository writes.
Compose fixtures use unique resource names; the approved proxy subnet/IPs stay
exact. A pre-existing overlapping network is an explicit failure, never removed.
"""
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
CADDY_IMAGE = "caddy:2.11.4-alpine"
PROXY_SUBNET = "172.30.250.0/24"
CADDY_IP = "172.30.250.2"
BACKEND_IP = "172.30.250.3"


def command(arguments, *, env=None, check=True, timeout=40):
    result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise AssertionError(f"Fixture command failed ({arguments[:3]}): {result.stderr[:1500]}")
    return result


def local_docker_environment():
    env = dict(os.environ)
    if env.get("DOCKER_HOST"):
        endpoint = env["DOCKER_HOST"]
    else:
        context = command(["docker", "context", "inspect"], env=env)
        endpoint = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("npipe://", "unix://")):
        raise AssertionError("Only a local Docker socket is allowed for these fixtures")
    return env


def compose_model():
    if not shutil.which("docker"):
        raise unittest.SkipTest("Docker Compose NOT_AVAILABLE")
    with TemporaryDirectory(prefix="sec1hr2a-config-") as temporary:
        env_path = Path(temporary) / "empty.env"
        env_path.write_text("", encoding="utf-8")
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("COMPOSE_"):
                del env[key]
        env.update(POSTGRES_DB="fixture", POSTGRES_USER="fixture", POSTGRES_PASSWORD="fixture",
                   APP_NAME="fixture", SECRET_KEY="fixture-not-a-secret", AUTO_SEED="false",
                   CORS_ORIGINS="http://fixture.invalid", PUBLIC_BASE_URL="http://fixture.invalid",
                   APP_DOMAIN="http://:8080", API_DOMAIN="http://:8081",
                   VITE_API_BASE_URL="https://api.fixture.invalid/api", VITE_API_URL="")
        result = command(["docker", "compose", "--env-file", str(env_path), "-p", "sec1hr2a-config",
                          "-f", str(ROOT / "docker-compose.production.yml"), "config", "--format", "json"], env=env)
        return json.loads(result.stdout)


def production_command():
    source = (ROOT / "backend/Dockerfile.production").read_text(encoding="utf-8")
    matches = re.findall(r"^CMD (\[.*\])$", source, re.MULTILINE)
    assert len(matches) == 1, "Exactly one exec-form backend CMD required"
    return json.loads(matches[0])


def check_contract(model, cmd):
    services = model["services"]
    frontend, backend, postgres = (services[key] for key in ("frontend", "backend", "postgres"))
    assert set(services) == {"frontend", "backend", "postgres"}, "Unexpected production service"
    assert set(frontend["networks"]) == {"edge", "proxy_net"}, "Caddy must not share app_net"
    assert set(backend["networks"]) == {"app_net", "proxy_net"}, "Backend network segmentation"
    assert set(postgres["networks"]) == {"app_net"}, "Postgres must remain only on app_net"
    proxy = model["networks"]["proxy_net"]
    assert proxy["name"] == "tecnocontrol_proxy_net", "Explicit proxy network name"
    assert proxy["driver"] == "bridge", "Proxy bridge driver"
    assert proxy.get("internal") is True, "Proxy network must be internal"
    assert proxy["ipam"]["config"] == [{"subnet": PROXY_SUBNET}], "Approved proxy subnet only"
    assert frontend["networks"]["proxy_net"]["ipv4_address"] == CADDY_IP, "Caddy static IP"
    assert backend["networks"]["proxy_net"]["ipv4_address"] == BACKEND_IP, "Backend static IP"
    assert backend["environment"]["FORWARDED_ALLOW_IPS"] == CADDY_IP, "Only exact Caddy peer trusted"
    assert not backend.get("ports"), "Backend host publication prohibited"
    assert not postgres.get("ports"), "Postgres host publication prohibited"
    assert sorted((p["target"], str(p["published"])) for p in frontend["ports"]) == [(80, "80"), (443, "443")]
    assert not backend.get("command") and not backend.get("entrypoint"), "Do not override backend CMD"
    assert cmd == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                   "--proxy-headers", "--workers", "1"], "Exact single-worker CMD; no wildcard override"


class ProxyConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = compose_model()
        cls.cmd = production_command()

    def test_production_compose_and_single_worker_contract(self):
        check_contract(self.model, self.cmd)

    def test_mutations_are_rejected_without_touching_source_files(self):
        cases = []

        def variant(name, mutate):
            model, cmd = deepcopy(self.model), self.cmd[:]
            mutate(model, cmd)
            cases.append((name, model, cmd))

        variant("uvicorn_wildcard", lambda m, c: c.append("--forwarded-allow-ips=*"))
        variant("frontend_on_app_net", lambda m, c: m["services"]["frontend"]["networks"].update(app_net={}))
        variant("postgres_on_proxy_net", lambda m, c: m["services"]["postgres"]["networks"].update(proxy_net={}))
        variant("proxy_not_internal", lambda m, c: m["networks"]["proxy_net"].update(internal=False))
        variant("different_authorized_peer", lambda m, c: m["services"]["backend"]["environment"].update(FORWARDED_ALLOW_IPS="172.30.250.4"))
        variant("workers_not_explicit", lambda m, c: c.__delitem__(slice(-2, None)))
        variant("multiple_workers", lambda m, c: c.__setitem__(-1, "2"))
        variant("backend_published", lambda m, c: m["services"]["backend"].update(ports=[{"target": 8000, "published": "8000"}]))
        variant("postgres_published", lambda m, c: m["services"]["postgres"].update(ports=[{"target": 5432, "published": "5432"}]))
        variant("caddy_static_ip_changed", lambda m, c: m["services"]["frontend"]["networks"]["proxy_net"].update(ipv4_address="172.30.250.4"))
        for name, model, cmd in cases:
            with self.subTest(mutation=name):
                self.assertNotEqual((model, cmd), (self.model, self.cmd))
                with self.assertRaises(AssertionError):
                    check_contract(model, cmd)


DIAGNOSTIC_APP = '''
import fastapi, uvicorn
from fastapi import FastAPI, Request, WebSocket
app = FastAPI()
def snapshot(connection):
    return dict(client_host=connection.client.host, client_port=connection.client.port,
        xff=connection.headers.getlist("x-forwarded-for"),
        xreal=connection.headers.get("x-real-ip"), forwarded=connection.headers.get("forwarded"),
        host=connection.headers.get("host"), proto=connection.headers.get("x-forwarded-proto"),
        scheme=connection.scope["scheme"], uvicorn=uvicorn.__version__, fastapi=fastapi.__version__)
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def echo(request: Request, path: str):
    return snapshot(request)
@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    await ws.send_json(snapshot(ws))
    await ws.close()
'''

CLIENT_PROBE = '''
import http.client, json, sys
data=json.loads(sys.argv[1])
headers=data["headers"]
if data.get("ws"):
    from websockets.sync.client import connect
    with connect(data["url"], additional_headers=headers, proxy=None, open_timeout=5) as ws:
        print(ws.recv())
else:
    from urllib.parse import urlsplit
    url=urlsplit(data["url"])
    conn=http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    conn.putrequest("POST", url.path or "/")
    conn.putheader("Content-Length", "2")
    for key, value in headers: conn.putheader(key, value)
    conn.endheaders(b"{}")
    response=conn.getresponse()
    assert response.status==200, response.status
    print(response.read().decode())
    conn.close()
'''


class RealProxyBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = os.environ.get("SEC1H_PROXY_BACKEND_IMAGE")
        if not image:
            raise unittest.SkipTest("Set SEC1H_PROXY_BACKEND_IMAGE for isolated real proxy tests")
        cls.env = local_docker_environment()
        for key in list(cls.env):
            if key.startswith("COMPOSE_"):
                del cls.env[key]
        command(["docker", "info", "--format", "{{.ServerVersion}}"], env=cls.env)
        for candidate in (image, CADDY_IMAGE):
            command(["docker", "image", "inspect", candidate], env=cls.env)
        version = command(["docker", "run", "--rm", "--pull=never", "--network=none", "--entrypoint", "python",
                           image, "-B", "-c", "import uvicorn,fastapi; assert uvicorn.__version__=='0.32.0'; assert fastapi.__version__=='0.115.0'"], env=cls.env)
        assert version.returncode == 0
        model, cmd = compose_model(), production_command()
        check_contract(model, cmd)
        ids = command(["docker", "network", "ls", "-q"], env=cls.env).stdout.split()
        networks = json.loads(command(["docker", "network", "inspect", *ids], env=cls.env).stdout)
        used = [ipaddress.ip_network(c["Subnet"]) for n in networks for c in n.get("IPAM", {}).get("Config") or [] if c.get("Subnet")]
        proxy_subnet = ipaddress.ip_network(PROXY_SUBNET)
        if any(n.version == 4 and n.overlaps(proxy_subnet) for n in used):
            raise AssertionError("Approved proxy subnet is already occupied locally; no existing network will be changed")
        free = [ipaddress.ip_network(f"198.18.{i}.0/24") for i in range(220, 250)]
        free = [n for n in free if all(u.version != 4 or not n.overlaps(u) for u in used)]
        assert len(free) >= 2, "Two isolated fixture subnets required"
        cls.edge_base = str(free[0].network_address).rsplit(".", 1)[0]
        cls.app_base = str(free[1].network_address).rsplit(".", 1)[0]
        cls.prefix = "sec1hr2a-" + uuid.uuid4().hex[:10]
        cls.temporary = TemporaryDirectory(prefix=cls.prefix + "-")
        cls.directory = Path(cls.temporary.name).resolve()
        assert not cls.directory.is_relative_to(ROOT), "Fixtures must stay outside the checkout"
        cls.addClassCleanup(cls.temporary.cleanup)
        (cls.directory / "empty.env").write_text("", encoding="utf-8")
        (cls.directory / "diagnostic.py").write_text(DIAGNOSTIC_APP, encoding="utf-8")
        shutil.copyfile(ROOT / "frontend/Caddyfile.production", cls.directory / "Caddyfile")
        cls.env.update(APP_DOMAIN="http://:8080", API_DOMAIN="http://:8081")
        mount = {"type": "bind", "source": str(cls.directory), "target": "/fixture", "read_only": True}
        sleeper = ["-B", "-c", "import time; time.sleep(600)"]

        def peer(nets):
            return dict(image=image, pull_policy="never", entrypoint=["python"], command=sleeper, networks=nets)

        cls.fixture = {
            "services": {
                "backend": dict(image=image, pull_policy="never", entrypoint=["python"],
                    command=["-B", "-m", cmd[0], "diagnostic:app", *cmd[2:], "--lifespan", "off", "--no-access-log"],
                    working_dir="/fixture", volumes=[mount], environment={"FORWARDED_ALLOW_IPS": model["services"]["backend"]["environment"]["FORWARDED_ALLOW_IPS"]},
                    networks={"app_net": {"ipv4_address": cls.app_base + ".3"}, "proxy_net": deepcopy(model["services"]["backend"]["networks"]["proxy_net"])}),
                "frontend": dict(image=CADDY_IMAGE, pull_policy="never", entrypoint=["caddy"],
                    command=["run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile"],
                    environment={"APP_DOMAIN": "http://:8080", "API_DOMAIN": "http://:8081"}, volumes=[mount],
                    networks={"edge": {"ipv4_address": cls.edge_base + ".2"}, "proxy_net": deepcopy(model["services"]["frontend"]["networks"]["proxy_net"])}),
                # A sleeping process proves segmentation without starting any database.
                "postgres": peer({"app_net": {}}),
                "edge-client": peer({"edge": {"ipv4_address": cls.edge_base + ".4"}}),
                "app-client": peer({"app_net": {"ipv4_address": cls.app_base + ".4"}}),
                "proxy-client": peer({"proxy_net": {"ipv4_address": "172.30.250.4"}}),
            },
            "networks": {
                "edge": {"internal": True, "ipam": {"config": [{"subnet": str(free[0])}]}},
                "app_net": {"internal": True, "ipam": {"config": [{"subnet": str(free[1])}]}},
                "proxy_net": {**deepcopy(model["networks"]["proxy_net"]), "name": cls.prefix + "-proxy"},
            },
        }
        cls.fixture_path = cls.directory / "compose.json"
        cls.fixture_path.write_text(json.dumps(cls.fixture), encoding="utf-8")
        cls.addClassCleanup(cls.clean_compose)
        cls.compose("up", "-d", "--no-build", "--pull", "never", timeout=90)
        for _ in range(40):
            ready = cls.compose("exec", "-T", "frontend", "wget", "-q", "-O", "-", "http://127.0.0.1:8081/ready", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Isolated Caddy/Uvicorn did not become ready")

    @classmethod
    def compose(cls, *args, check=True, timeout=40):
        return command(["docker", "compose", "--env-file", str(cls.directory / "empty.env"), "-p", cls.prefix,
                        "-f", str(cls.fixture_path), *args], env=cls.env, check=check, timeout=timeout)

    @classmethod
    def clean_compose(cls):
        cls.compose("down", "--volumes", "--remove-orphans", timeout=60)
        for resource in ("container", "network"):
            remaining = command(["docker", resource, "ls", "-q", "--filter", f"label=com.docker.compose.project={cls.prefix}"], env=cls.env)
            assert not remaining.stdout.strip(), f"Owned {resource} cleanup incomplete"

    def probe(self, peer="edge-client", target=None, headers=(), *, path="/api/auth/login", ws=False):
        target = target or self.edge_base + ".2:8081"
        data = dict(url=("ws://" if ws else "http://") + target + path, headers=list(headers), ws=ws)
        result = self.compose("exec", "-T", peer, "python", "-B", "-c", CLIENT_PROBE, json.dumps(data))
        return json.loads(result.stdout)

    def assert_canonical(self, row, expected):
        expected = str(ipaddress.ip_address(expected))
        self.assertEqual(row["client_host"], expected)
        self.assertEqual(row["client_port"], 0, "Uvicorn must accept Caddy's forwarded identity")
        self.assertEqual(row["xff"], [expected], "Exactly one canonical XFF element/header")
        self.assertIsNone(row["xreal"])
        self.assertIsNone(row["forwarded"])
        self.assertEqual(row["uvicorn"], "0.32.0")
        self.assertEqual(row["fastapi"], "0.115.0")

    def test_caddy_2_11_4_validate_and_effective_no_upstream_trust(self):
        self.assertTrue(self.compose("exec", "-T", "frontend", "caddy", "version").stdout.startswith("v2.11.4 "))
        self.compose("exec", "-T", "frontend", "caddy", "validate", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
        adapted = json.loads(self.compose("exec", "-T", "frontend", "caddy", "adapt", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile").stdout)
        for server in adapted["apps"]["http"]["servers"].values():
            self.assertNotIn("trusted_proxies", server)

    def test_actual_compose_dns_and_network_membership(self):
        ids = self.compose("ps", "-q").stdout.split()
        containers = json.loads(command(["docker", "inspect", *ids], env=self.env).stdout)
        memberships = {"frontend": {"edge", "proxy_net"}, "backend": {"app_net", "proxy_net"}, "postgres": {"app_net"}}
        names = {"edge": self.prefix + "_edge", "app_net": self.prefix + "_app_net", "proxy_net": self.prefix + "-proxy"}
        for container in containers:
            service = container["Config"]["Labels"]["com.docker.compose.service"]
            self.assertFalse(container["HostConfig"].get("PortBindings"), "No fixture host ports")
            if service in memberships:
                self.assertEqual(set(container["NetworkSettings"]["Networks"]), {names[n] for n in memberships[service]})
        resolved = self.compose("exec", "-T", "frontend", "getent", "hosts", "backend").stdout
        self.assertEqual({line.split()[0] for line in resolved.splitlines()}, {BACKEND_IP})
        self.assertNotEqual(self.compose("exec", "-T", "frontend", "getent", "hosts", "postgres", check=False).returncode, 0)
        proxy = json.loads(command(["docker", "network", "inspect", names["proxy_net"]], env=self.env).stdout)[0]
        self.assertTrue(proxy["Internal"])
        self.assertEqual(proxy["IPAM"]["Config"][0]["Subnet"], PROXY_SUBNET)

    def test_http_spoofing_matrix_on_both_post_paths(self):
        variants = [[], [("X-Forwarded-For", "1.2.3.4")], [("X-Forwarded-For", "1.2.3.4, 8.8.8.8")],
                    [("X-Real-IP", "5.6.7.8")], [("Forwarded", "for=9.10.11.12")],
                    [("X-Forwarded-For", "1.2.3.4"), ("X-Real-IP", "5.6.7.8"), ("Forwarded", "for=9.10.11.12")],
                    [("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "8.8.8.8")],
                    [("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "forged.invalid")]]
        for path in ("/api/auth/login", "/api/player/activate"):
            for headers in variants:
                with self.subTest(path=path, headers=headers):
                    row = self.probe(headers=headers, path=path)
                    self.assert_canonical(row, self.edge_base + ".4")
                    self.assertEqual(row["host"], self.edge_base + ".2:8081")
                    self.assertEqual(row["proto"], "http")
                    self.assertEqual(row["scheme"], "http")

    def test_private_peer_via_caddy_cannot_supply_identity(self):
        row = self.probe(peer="proxy-client", target=CADDY_IP + ":8081", headers=[("X-Forwarded-For", "1.2.3.4, 8.8.8.8")])
        self.assert_canonical(row, "172.30.250.4")

    def test_direct_app_net_and_other_proxy_net_peer_are_not_trusted(self):
        headers = [("X-Forwarded-For", "1.2.3.4"), ("X-Real-IP", "5.6.7.8"),
                   ("Forwarded", "for=9.10.11.12"), ("X-Forwarded-Proto", "https")]
        for peer, target, expected in (("app-client", "backend:8000", self.app_base + ".4"),
                                      ("proxy-client", "backend:8000", "172.30.250.4")):
            for path in ("/api/auth/login", "/api/player/activate"):
                with self.subTest(peer=peer, path=path):
                    row = self.probe(peer=peer, target=target, headers=headers, path=path)
                    self.assertEqual(row["client_host"], expected)
                    self.assertGreater(row["client_port"], 0)
                    self.assertEqual(row["xff"], ["1.2.3.4"], "Header arrives but must not become identity")
                    self.assertEqual(row["scheme"], "http")

    def test_websocket_upgrade_preserves_peer_and_rejects_spoofed_headers(self):
        headers = [("X-Forwarded-For", "1.2.3.4, 8.8.8.8"), ("X-Real-IP", "5.6.7.8"), ("Forwarded", "for=9.10.11.12")]
        row = self.probe(headers=headers, path="/ws", ws=True)
        self.assert_canonical(row, self.edge_base + ".4")
        self.assertEqual(row["scheme"], "ws")
        self.assertEqual(row["host"], self.edge_base + ".2:8081")

    def test_ipv6_peer_via_caddy(self):
        # A separate temporary dual-stack edge leaves approved IPv4 proxy IPs intact.
        name = self.prefix + "-v6"
        subnet = "fd30:" + uuid.uuid4().hex[:4] + ":250::/64"
        base = subnet.split("::")[0]
        created = command(["docker", "network", "create", "--internal", "--ipv6", "--subnet", subnet, name], env=self.env, check=False)
        if created.returncode:
            self.skipTest("IPv6 NOT_AVAILABLE: local Docker cannot create the IPv6 fixture network")
        connected = []
        try:
            for service, suffix in (("frontend", "2"), ("edge-client", "4")):
                identifier = self.compose("ps", "-q", service).stdout.strip()
                result = command(["docker", "network", "connect", "--ip6", base + "::" + suffix, name, identifier], env=self.env, check=False)
                if result.returncode:
                    self.skipTest("IPv6 NOT_AVAILABLE: local Docker cannot attach the IPv6 fixture peer")
                connected.append(identifier)
            row = self.probe(target="[" + base + "::2]:8081", headers=[("X-Forwarded-For", "1.2.3.4")])
            self.assert_canonical(row, base + "::4")
        finally:
            for identifier in reversed(connected):
                command(["docker", "network", "disconnect", name, identifier], env=self.env)
            command(["docker", "network", "rm", name], env=self.env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
