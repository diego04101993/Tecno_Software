"""Isolated Caddy 2.11.4 parity, redaction and logging cost checks."""

import argparse
from collections import Counter
from contextlib import closing
from decimal import Decimal
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter_ns


BASELINE = "e2d265fe4b8a58d31c93a6beea9d5c32a11c6b4a"
PYTHON_IMAGE = "sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535"
API_HOST = "api.tecnocontrolcloud.com"
EDGE_VERSION = "v2.11.4"
TRACE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ROUTES = (
    ("POST", "/api/player/heartbeat", 200, "PLAYER_HEARTBEAT"),
    ("GET", "/api/player/runtime-config", 200, "PLAYER_RUNTIME_CONFIG"),
    ("POST", "/api/player/playback-events", 201, "PLAYER_PLAYBACK_EVENT"),
)
OTHER = (
    ("GET", "/api/player/heartbeat", 200, "OTHER"),
    ("POST", "/api/player/runtime-config", 200, "OTHER"),
    ("GET", "/api/player/playback-events", 200, "OTHER"),
    ("GET", "/api/player/runtime-config/", 200, "OTHER"),
    ("GET", "/api/channels/2561a89b-3756-486f-804c-6215e9aff2e9/runtime-config", 200, "OTHER"),
    ("GET", "/qa-sensitive-path", 200, "OTHER"),
)
REQUEST_HEADERS = {
    "Authorization": "Bearer qa-secret-authorization-sentinel",
    "Cookie": "session=qa-secret-cookie-sentinel",
    "X-Player-Token": "qa-secret-player-token-sentinel",
    "X-Forwarded-For": "203.0.113.249",
    "X-Forwarded-Host": "forged-forwarded.qa.invalid",
    "X-Forwarded-Proto": "http",
    "X-Forwarded-Port": "81",
    "X-Real-IP": "203.0.113.250",
    "Forwarded": "for=203.0.113.251;proto=http",
    "X-Request-ID": "qa-secret-client-trace-sentinel",
    "Content-Type": "application/json",
}
HASH_HEADERS = (
    "Authorization", "Cookie", "X-Player-Token", "Content-Type", "Content-Length",
    "Host", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
    "X-Forwarded-Port", "X-Real-IP", "Forwarded",
)
QUERY = "?opaque=qa-secret-query-sentinel"
BODY = b'{"player_devices.id":"qa-secret-raw-player-id-sentinel","body":"qa-secret-body-sentinel"}'
RESPONSE_COOKIE = "probe=qa-secret-response-cookie-sentinel"
BLOCK_REQUESTS = 300
BENCHMARK_ORDER = ("off", "new", "new", "off", "new", "off", "off", "new")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        request_hash = sha256(canonical([
            self.command, self.path, self.request_version, body.hex(),
            {name: self.headers.get_all(name) for name in HASH_HEADERS},
        ]))
        response = canonical({
            "upstream": self.server.slot,
            "request_sha256": request_hash,
            "opaque": "qa-secret-response-body-sentinel",
        })
        status = 201 if self.command == "POST" and self.path.split("?")[0] == ROUTES[2][1] else 200
        self.send_response_only(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("X-QA-Contract", "unchanged")
        self.send_header("X-Request-ID", "qa-secret-upstream-trace-sentinel")
        self.send_header("Set-Cookie", RESPONSE_COOKIE)
        self.end_headers()
        self.wfile.write(response)

    do_POST = do_GET


def serve(slot):
    with ThreadingHTTPServer(("0.0.0.0", 8000), EchoHandler) as server:
        server.slot = slot
        server.serve_forever()


def exchange(connection, case, *, benchmark=False):
    method, path, expected_status, route_class = case
    start_ns = perf_counter_ns()
    connection.request(method, path + QUERY, body=BODY if method == "POST" else None, headers=REQUEST_HEADERS)
    response = connection.getresponse()
    body = response.read()
    elapsed_ns = perf_counter_ns() - start_ns
    request_id = response.getheader("X-Request-ID")
    if response.status != expected_status or TRACE.fullmatch(request_id or "") is None:
        raise AssertionError(f"Unexpected HTTP fixture result for {route_class}: {response.status}")
    if benchmark:
        return elapsed_ns
    decoded = json.loads(body)
    return {
        "class": route_class,
        "request_id": request_id,
        "status": response.status,
        "headers_sha256": sha256(canonical(sorted(
            (name.lower(), value) for name, value in response.getheaders()
            if name.lower() not in {"date", "x-request-id"}
        ))),
        "set_cookie_match": response.getheader("Set-Cookie") == RESPONSE_COOKIE,
        "response_sha256": sha256(body),
        "request_sha256": decoded["request_sha256"],
        "upstream": decoded["upstream"],
    }


def exercise(edge, ca_file, cases, *, benchmark=False):
    # SNI/Host remain the sealed API hostname while only the isolated Docker alias is dialed.
    context = ssl.create_default_context(cafile=ca_file)
    with closing(http.client.HTTPSConnection(API_HOST, context=context, timeout=5)) as connection:
        connection._create_connection = lambda _target, timeout, source: socket.create_connection(
            (edge, 443), timeout, source,
        )
        for case in ROUTES * 4:
            exchange(connection, case, benchmark=True)
        return [exchange(connection, case, benchmark=benchmark) for case in cases]


def driver(edges, mode):
    if mode == "benchmark":
        runs = {"off": [], "new": []}
        for name in BENCHMARK_ORDER:
            edge, ca_file = edges[name]
            runs[name].extend(exercise(edge, ca_file, ROUTES * (BLOCK_REQUESTS // len(ROUTES)), benchmark=True))
        return runs
    edge, ca_file = edges[mode]
    cases = ROUTES * 100 + OTHER if mode in ("old", "new") else ROUTES
    rows = exercise(edge, ca_file, cases)
    if mode == "rewrite":
        assert rows[1]["upstream"] == "GREEN"
    return rows


def adapted(docker, container, path):
    config = json.loads(docker.read(container, "caddy", "adapt", "--config", path, "--adapter", "caddyfile"))
    servers = config["apps"]["http"]["servers"]
    servers["public"]["@id"] = "bgv2-generation-0"
    servers["redirect"].setdefault("routes", [])
    return config


def runtime_rewrite_caddyfile(candidate):
    """Exercise Caddy's actual directive ordering on a disposable copy."""
    public_api_proxy = b"\timport upstream backend-blue:8000 0 public BLUE"
    assert candidate.count(public_api_proxy) == 1, "No unique public API fixture insertion point"
    rewrite = (b"\t@qa_runtime_rewrite {\n"
               b"\t\tmethod GET\n"
               b"\t\tpath /api/player/runtime-config\n"
               b"\t}\n"
               b"\trewrite @qa_runtime_rewrite /qa-runtime-rewritten\n")
    return candidate.replace(public_api_proxy, rewrite + public_api_proxy)


def verify_rewrite(direct, rewritten):
    assert [row["class"] for row in direct] == [row["class"] for row in rewritten]
    for left, right in zip(direct, rewritten, strict=True):
        assert (left["upstream"], right["upstream"]) == ("GREEN", "GREEN")
        assert (left["request_sha256"] != right["request_sha256"]) == (left["class"] == "PLAYER_RUNTIME_CONFIG")


def verify_log(capture, cid, responses, *, generation, slot):
    from player_latency import EDGE_CLASSES, _edge_json, _measurement

    rows_by_id = {}
    count = Counter()
    parsed_count = 0
    sought = {response["request_id"] for response in responses}
    if len(sought) != len(responses):
        raise AssertionError("Response request IDs must be unique")
    for raw_stream in (capture.stdout, capture.stderr):
        assert b"qa-secret-" not in raw_stream
    for line in capture.timeline(cid):
        row = _edge_json(line.payload)
        if not row.get("logger", "").startswith("http.log.access"):
            continue
        assert not {"request", "resp_headers", "headers", "body", "uri"} & row.keys()
        assert all(value not in line.payload for value in (
            b"qa-secret-", b"2561a89b-3756-486f-804c-6215e9aff2e9", b"/qa-sensitive-path",
            b"/api/player/", b"/qa-runtime-rewritten", b"forged-forwarded.qa.invalid",
        ))
        request_id = row.get("request_id")
        if request_id in sought:
            assert request_id not in rows_by_id
            rows_by_id[request_id] = row
    for response in responses:
        row = rows_by_id[response["request_id"]]
        assert row["route_class"] == response["class"], ("route_class", row.get("route_class"), response["class"])
        assert row["route_generation"] == str(generation)
        assert row["slot"] == slot
        assert row["upstream"] == f"backend-{slot.lower()}:8000"
        assert row["ingress"] == "public" and row["status"] == response["status"]
        assert type(row["duration"]) in (Decimal, int) and row["duration"] >= 0
        upstream_ms = Decimal(row["upstream_duration_ms"])
        headers_ms = Decimal(row["upstream_headers_ms"])
        assert 0 <= headers_ms <= upstream_ms
        native_endpoint = EDGE_CLASSES.get(response["class"])
        if native_endpoint is not None:
            # Verify the real Caddy encoding with the promotion parser. Wire
            # membership is covered separately by the monotonic replay.
            observed = _measurement(row, {
                "endpoint": native_endpoint, "trace": response["request_id"], "status": response["status"],
                "request_start_monotonic_ns": 1, "response_complete_monotonic_ns": 2,
            }, {"generation": generation, "slot": slot, "upstream": f"backend-{slot.lower()}:8000"})
            assert observed["EDGE_TOTAL_DURATION_NS"] >= 0
            assert Decimal(observed["UPSTREAM_DURATION_MS"]) == upstream_ms
            assert Decimal(observed["UPSTREAM_HEADER_LATENCY_MS"]) == headers_ms
            parsed_count += 1
        count[row["route_class"]] += 1
    return dict(count), parsed_count


def compare_contract(old, new):
    assert len(old) == len(new)
    for left, right in zip(old, new, strict=True):
        assert left["request_id"] != right["request_id"]
        assert left["set_cookie_match"] is right["set_cookie_match"] is True
        left_contract = {key: value for key, value in left.items() if key != "request_id"}
        right_contract = {key: value for key, value in right.items() if key != "request_id"}
        assert left_contract == right_contract, ("HTTP parity", left["class"])


def statistics_ns(values):
    ordered = sorted(values)
    return {"p50": int(statistics.median(ordered)), "p95": ordered[math.ceil(len(ordered) * .95) - 1],
            "max": ordered[-1]}


def comparison(docker, scratch):
    from qa_docker import CADDY_IMAGE, DEPLOY_DIRECTORY, PROJECT_DIRECTORY, wait_until
    from qa_topology import binding
    from routectl import RouteConfig

    qa_source = binding(Path(__file__).resolve().parent, "/source")
    config_mount = binding(scratch, "/fixture")
    env = {"BGV2_API_HOST": API_HOST, "BGV2_APP_HOST": "app.tecnocontrolcloud.com",
           "BGV2_TLS_DIRECTIVE": "tls internal"}
    internal = docker.create_network("telemetry", internal=True)
    for slot in ("BLUE", "GREEN"):
        container = docker.create_container("echo-" + slot.lower(), PYTHON_IMAGE,
            "python", "-I", "-B", "/source/qa_v268_caddy.py", "serve", slot,
            args=("--network", internal, "--network-alias", "backend-" + slot.lower(), "--mount", qa_source), cpus="0-1")
        docker.run("start", container)
        wait_until(lambda: docker.execute(container, "python", "-c",
            "import socket; socket.create_connection(('127.0.0.1', 8000), 1).close()").returncode == 0)
    client = docker.create_container("telemetry-client", PYTHON_IMAGE, "sleep", "900",
        args=("--network", internal, "--mount", qa_source), cpus="2")
    docker.run("start", client)
    adapter = docker.create_container("telemetry-adapt", CADDY_IMAGE, "sleep", "900",
        args=("--network", "none", "--mount", config_mount, "--tmpfs", "/config", "--tmpfs", "/data"),
        env=env, cpus="3")
    docker.run("start", adapter)
    version = docker.read(adapter, "caddy", "version")
    assert version.split()[0] == EDGE_VERSION, version
    baseline = subprocess.check_output(["git", "show", f"{BASELINE}:deploy/blue-green-v2/Caddyfile.edge"],
                                       cwd=PROJECT_DIRECTORY)
    candidate = (DEPLOY_DIRECTORY / "Caddyfile.edge").read_bytes()
    (scratch / "old.caddyfile").write_bytes(baseline)
    (scratch / "new.caddyfile").write_bytes(candidate)
    (scratch / "rewrite.caddyfile").write_bytes(runtime_rewrite_caddyfile(candidate))
    old = adapted(docker, adapter, "/fixture/old.caddyfile")
    new = adapted(docker, adapter, "/fixture/new.caddyfile")
    off = json.loads(canonical(old))
    for server in off["apps"]["http"]["servers"].values():
        server.pop("logs", None)
    promoted = {"api": "GREEN", "frontend": "BLUE"}
    green = RouteConfig(new).changed(promoted, 1).config
    rewritten = RouteConfig(adapted(docker, adapter, "/fixture/rewrite.caddyfile")).changed(promoted, 1).config
    configs = {"old": old, "off": off, "new": new, "green": green, "rewrite": rewritten}
    ids, endpoints = {}, {}
    for name, config in configs.items():
        (scratch / (name + ".json")).write_bytes(canonical(config))
        ids[name] = docker.create_container("edge-" + name, CADDY_IMAGE, "caddy", "run",
            "--config", "/fixture/" + name + ".json",
            args=("--network", internal, "--mount", config_mount, "--tmpfs", "/config",
                  "--tmpfs", "/data", "--tmpfs", "/run/bgv2"), cpus="3")
        docker.run("start", ids[name])
        ca_path = "/data/caddy/pki/authorities/local/root.crt"
        wait_until(lambda: docker.execute(ids[name], "test", "-s", ca_path).returncode == 0)
        ca = docker.read(ids[name], "cat", ca_path)
        ca_file = f"/tmp/qa-caddy-{name}.crt"
        docker.read(client, "sh", "-ec", 'umask 077; cat > "$1"', "qa-ca", ca_file, input=ca.encode() + b"\n")
        host = docker.inspect("container", ids[name])["Name"].lstrip("/")
        endpoints[name] = (host, ca_file)
        # Readiness uses TLS on the same profile as the measured requests.
        wait_until(lambda: docker.execute(client, "python", "-I", "-B", "/source/qa_v268_caddy.py",
                                           "warmup", host, ca_file).returncode == 0)

    def run_client(mode):
        return json.loads(docker.read(client, "python", "-I", "-B", "/source/qa_v268_caddy.py", "driver",
                                      mode, input=canonical(endpoints), timeout=240))

    old_responses, new_responses = run_client("old"), run_client("new")
    compare_contract(old_responses, new_responses)
    normal_counts, normal_parsed = verify_log(
        docker.logs(ids["new"]), ids["new"], new_responses, generation=0, slot="BLUE",
    )
    green_responses, rewrite_responses = run_client("green"), run_client("rewrite")
    verify_rewrite(green_responses, rewrite_responses)
    green_counts, green_parsed = verify_log(
        docker.logs(ids["green"]), ids["green"], green_responses, generation=1, slot="GREEN",
    )
    rewrite_counts, rewrite_parsed = verify_log(
        docker.logs(ids["rewrite"]), ids["rewrite"], rewrite_responses, generation=1, slot="GREEN",
    )
    runs = run_client("benchmark")
    assert not any(b"http.log.access" in line.payload for line in docker.logs(ids["off"]).timeline(ids["off"]))
    assert all(len(runs[name]) == 1200 for name in ("off", "new"))
    stats = {key: statistics_ns(value) for key, value in runs.items()}
    deltas = {key: stats["new"][key] - stats["off"][key] for key in stats["new"]}
    return {
        "status": "PASS",
        "caddy": {"version": version, "image": CADDY_IMAGE},
        "baseline_caddyfile_sha256": sha256(baseline),
        "new_caddyfile_sha256": sha256(candidate),
        "native_request_counts": normal_counts,
        "green_generation_one_counts": green_counts,
        "after_rewrite_counts": rewrite_counts,
        "promotion_latency_parser_rows": normal_parsed + green_parsed + rewrite_parsed,
        "runtime_early_class_preserved": "PASS",
        "routing_behavior_unchanged": "YES",
        "http_contract_unchanged": "YES",
        "edge_access_log_redaction": "PASS",
        "benchmark": {
            "mode": "serial HTTPS keep-alive over same disposable Docker network; alternating measured blocks",
            "baseline_access_logging": "V2.6.7 with access logging OFF",
            "request_count": {key: len(value) for key, value in runs.items()},
            "latency_ns": stats,
            "overhead_ns_new_minus_off": deltas,
            "order": list(BENCHMARK_ORDER),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from qa_docker import LocalDocker, PROJECT_DIRECTORY

    docker = LocalDocker()
    root = PROJECT_DIRECTORY / ".tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bgv268-caddy-", dir=root) as name:
        try:
            receipt = comparison(docker, Path(name))
        finally:
            docker.cleanup()
    receipt["owned_qa_resources_cleanup"] = "PASS"
    with args.receipt.open("xb") as out:
        out.write(canonical(receipt) + b"\n")
    print(canonical(receipt).decode())


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve(sys.argv[2])
    elif sys.argv[1:2] == ["warmup"]:
        exercise(sys.argv[2], sys.argv[3], ())
    elif sys.argv[1:2] == ["driver"]:
        print(canonical(driver(json.load(sys.stdin), sys.argv[2])).decode())
    else:
        main()
