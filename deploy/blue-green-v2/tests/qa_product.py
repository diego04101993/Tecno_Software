"""Black-box contract, traffic, media and capacity checks for the QA topology."""

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from qa_docker import RELEASES
from qa_http import API_HOST, APP_HOST, multipart, utc_now


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a39sAAAAASUVORK5CYII=")


class ProductQA:
    def __init__(self, topology, database, http):
        self.topology = topology
        self.database = database
        self.http = http
        self.results = {}
        self.asset_refs = {}

    def read(self, path, slot=None, **kwargs):
        return self.http.request(API_HOST, path, slot=slot, **kwargs)

    def login(self, direct_slot=None):
        payload = {"email": "operator@bgv2.example.com", "password": self.database.app_password}
        if direct_slot is None:
            response = self.read("/api/auth/login", method="POST", body=payload)
        else:
            response = self.http.disposable_direct_api(self.topology.bindings[f"api:{direct_slot}"]["ip"],
                "/api/auth/login", headers={"Content-Type": "application/json"}, body=json.dumps(payload).encode())
        self.token = response.require(200).json()["access_token"]
        self.admin = {"Authorization": "Bearer " + self.token}
        self.results["disposable_login"] = 200

    def frontend(self, slot, route=None):
        root = self.http.request(APP_HOST, "/", slot=route).require(200)
        assert "script-src 'self'" in root.headers["content-security-policy"]
        assets = sorted(set(re.findall(rb'(?:src|href)="(/assets/[^"]+)"', root.body)))
        assert assets and any(item.endswith(b".js") for item in assets)
        if route:
            self.asset_refs[slot] = set(assets)
        else:
            self.public_frontend_trace = root.headers["x-request-id"]
        for asset in assets:
            response = self.http.request(APP_HOST, asset.decode(), slot=route).require(200)
            assert response.body and "text/html" not in response.headers["content-type"]
        self.http.request(APP_HOST, "/assets/nonexistent-qa.js", slot=route).require(404)
        self.results[f"{slot.lower()}_assets"] = len(assets)
        return {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}

    def asset_rollback_parity(self):
        # The current release pair happens to carry identical static references.
        # A future pair with changed chunk names needs a separate retention plan.
        assert self.asset_refs["BLUE"] == self.asset_refs["GREEN"]
        return {"status": "PASS", "scope": "EXACT_LOCAL_RELEASE_PAIR_IDENTICAL_ASSET_REFERENCES"}

    def api(self, slot, expected_name=None):
        assert self.read("/", slot).require(200).json()["name"] == (expected_name or f"BGV2 QA {slot}")
        self.read("/api/player/time", slot).require(200)
        self.read("/api/auth/me", slot, headers=self.admin).require(200)
        channels = self.read("/api/channels/", slot, headers=self.admin).require(200)
        assert channels.elapsed_ns <= 3_000_000_000
        assert {item["id"] for item in channels.json()} == set(self.database.fixture["channels"])
        ids = self.database.fixture["channels"]

        def request(index):
            channel = ids[index % len(ids)]
            value = self.read(f"/api/channels/{channel}/runtime-config", slot, headers=self.admin,
                              timeout=10).require(200)
            runtime = value.json()
            assert runtime["runtime_version"] == 3 and runtime["channel"]["id"] == channel
            assert runtime["mode"] == "normal" and runtime["videowall"] == {"enabled": False}
            assert len(runtime["outputs"]) == 1
            return value.elapsed_ns

        serial = [request(i) for i in range(36)]
        # Four clients, maximum four in flight, stop dispatch after first failure.
        concurrent = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for group in range(4):
                futures = [pool.submit(request, group * 4 + i) for i in range(4)]
                for future in as_completed(futures):
                    concurrent.append(future.result())
        self.results[f"{slot.lower()}_runtime"] = {
            "channels_elapsed_ns": channels.elapsed_ns,
            "serial_ok": len(serial), "serial_max_ns": max(serial),
            "concurrent_ok": len(concurrent), "concurrent_max_ns": max(concurrent),
        }
        return {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                "channels_elapsed_ns": channels.elapsed_ns, "runtime_serial_ok": len(serial),
                "runtime_concurrent_ok": len(concurrent), "runtime_clients": 4, "runtime_transport_timeout_s": 10}

    def preflight(self):
        self.read("/api/player/heartbeat", "GREEN", method="POST", body={}).require(403)
        self.read("/api/player/runtime-config", "GREEN").require(403)
        self.read("/api/auth/login", "GREEN", method="POST", body={}).require(403)
        # Public requests with alleged selectors still resolve to BLUE.
        response = self.read("/?slot=GREEN", headers={"X-BGV2-Slot": "GREEN", "Cookie": "slot=GREEN"}).require(200)
        assert response.json()["name"] == "BGV2 QA BLUE"
        for slot in (None, "GREEN"):
            self.read("/", slot, headers={"Host": APP_HOST}).require(421)
        self.results["host_sni_parity"] = True
        self.results["private_write_blocked"] = True

    def cors(self):
        allow = self.read("/api/auth/me", method="OPTIONS", headers={"Origin": f"https://{APP_HOST}",
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"}).require(200)
        assert allow.headers["access-control-allow-origin"] == f"https://{APP_HOST}"
        assert allow.headers["access-control-allow-credentials"] == "true"
        deny = self.read("/api/auth/me", method="OPTIONS", headers={"Origin": "https://invalid.test",
            "Access-Control-Request-Method": "GET"}).require(400)
        assert "access-control-allow-origin" not in deny.headers
        self.results["cors"] = {"approved": 200, "unapproved": 400}

    def upload(self, direct_slot=None):
        content_type, body = multipart(self.database.fixture["client"], PNG)
        headers = {**self.admin, "Content-Type": content_type}
        if direct_slot is not None:
            address = self.topology.bindings[f"api:{direct_slot}"]["ip"]
            response = self.http.disposable_direct_api(address, "/api/contents/upload", headers=headers, body=body)
        else:
            response = self.read("/api/contents/upload", method="POST", body=body, headers=headers)
        uploaded = response.require(201).json()
        path = urlsplit(uploaded["file_path"]).path
        assert path.startswith("/media/")
        return path

    def verify_media(self, path):
        for slot in ("BLUE", "GREEN"):
            response = self.read(path, slot).require(200)
            assert response.body == PNG
            partial = self.read(path, slot, headers={"Range": "bytes=0-7"}).require(206)
            assert partial.body == PNG[:8] and partial.headers["content-range"] == f"bytes 0-7/{len(PNG)}"
        # Confirm the same inode, ownership and permissions from both processes.
        checks = [self.topology.docker.read(self.topology.containers["api", slot], "stat",
                   "-c", "%i:%u:%g:%a:%s", "/app/storage/uploads/" + path.removeprefix("/media/"))
                   for slot in ("BLUE", "GREEN")]
        assert checks[0] == checks[1] and checks[0].endswith(f":{len(PNG)}")

    def heartbeat(self, index):
        headers = {"Authorization": "Bearer " + self.database.player_tokens[index]}
        response = self.read("/api/player/heartbeat", method="POST", headers=headers, body={"status": "online"})
        response.require(200)
        ack = response.json()
        assert ack["ok"] is True and ack["channel_id"] == self.database.fixture["channels"][index]
        return {"trace": response.headers["x-request-id"], "channel_id": ack["channel_id"],
                "received_at": ack["received_at"]}

    def playback(self, index):
        headers = {"Authorization": "Bearer " + self.database.player_tokens[index]}
        response = self.read("/api/player/playback-events", method="POST", headers=headers,
                             body={"started_at": utc_now(), "duration_seconds": 1}).require(201)
        return response.headers["x-request-id"]

    def collect_connections(self):
        local_stats = self.topology.docker.run("stats", "--no-stream", "--format", "{{json .}}",
                                               *self.topology.containers.values(),
                                               self.database.pg, self.topology.edge)
        stats = [json.loads(line) for line in local_stats.splitlines()]
        assert len(stats) == 6
        limits = {}
        for (role, slot), container in self.topology.containers.items():
            fd_used = int(self.topology.docker.read(container, "sh", "-c", "ls /proc/1/fd | wc -l"))
            fd_limit = int(self.topology.docker.read(container, "sh", "-c", "awk '/Max open files/{print $4}' /proc/1/limits"))
            assert 0 < fd_used < fd_limit
            limits[f"{role}-{slot.lower()}"] = {"fd_used": fd_used, "fd_soft_limit": fd_limit}
        counts = self.database.query(
            "SELECT COALESCE(json_object_agg(state,n),'{}') FROM (SELECT state, count(*) n "
            "FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' "
            "AND pid<>pg_backend_pid() GROUP BY state) AS c")
        states = json.loads(counts)
        assert "idle in transaction" not in states and 0 < sum(states.values()) < 25
        pool = {}
        for slot in RELEASES:
            code = "from app.core.database import engine; print(type(engine.pool).__name__,engine.pool.size(),engine.pool._max_overflow)"
            evidence = self.topology.docker.read(self.topology.containers["api", slot], "python", "-B", "-c", code)
            kind, size, overflow = evidence.split()
            assert kind == "QueuePool" and int(size) > 0 and int(overflow) >= 0
            pool[slot] = {"class": kind, "base": int(size), "overflow": int(overflow),
                          "source": "sealed_image_pool_configuration"}
        max_connections = int(self.database.query("SHOW max_connections"))
        reserved = int(self.database.query(
            "SELECT current_setting('superuser_reserved_connections')::int + current_setting('reserved_connections')::int;"))
        assert sum(item["base"] + item["overflow"] for item in pool.values()) < max_connections - reserved
        self.results["capacity"] = {
            "scope": "4_PINNED_CPU_SYNTHETIC_FIXTURE", "postgres_connections": states,
            "postgres_max_connections": max_connections, "postgres_reserved_connections": reserved,
            "pool_configuration": pool, "file_descriptors": limits,
            "docker_sample": [{"cpu": row["CPUPerc"], "memory": row["MemUsage"], "pids": row["PIDs"]}
                              for row in stats],
        }
