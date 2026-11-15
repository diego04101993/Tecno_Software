from base64 import b64decode
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from phase3_evidence import verify_public_proxy
from release_state import Stop, canonical


EDGE = "e" * 64
BACKEND = "b" * 64
TRACE = "3fdb3c9a-84ed-44fa-81b7-45ec155823a2"
MARKER = "a" * 32
CLIENT = "192.0.2.45"
SINCE = "2026-11-15T17:33:52.000000000Z"
UNTIL = "2026-11-15T17:33:54.000000000Z"
EDGE_TIME = "2026-11-15T17:33:53.000000019Z"
BACKEND_TIME = "2026-11-15T17:33:53.000000002Z"


def streams(edge_trace=TRACE, backend_ip=CLIENT):
    edge = b"".join(
        EDGE_TIME.encode() + b" " + canonical({
            "logger": "http.log.access.phase3", "ingress": "public",
            "route_generation": "0", "upstream": "172.30.250.3:8000", "status": 200,
            "request_id": edge_trace if i == 0 else f"{i:08x}-1111-2222-3333-444444444444",
        }) + b"\n" for i in range(19)
    )
    backend = (f'{BACKEND_TIME} INFO:     {backend_ip}:0 - '
               f'"GET /api/player/time?bgv2_probe={MARKER} HTTP/1.1" 200 OK\n').encode()
    return {EDGE: (b"", edge), BACKEND: (backend, b"")}


class Phase3ProxyEvidenceTests(unittest.TestCase):
    def verify(self, evidence_dir, data):
        def invoke(*args):
            stdout, stderr = data[args[-1]]
            return CompletedProcess(["docker", "--host", "unix:///var/run/docker.sock", *args], 0, stdout, stderr)

        return verify_public_proxy(invoke, edge_id=EDGE, backend_id=BACKEND, since=SINCE, until=UNTIL,
                                   request_id=TRACE, marker=MARKER, expected_client=CLIENT, evidence_dir=evidence_dir)

    def test_stderr_edge_and_stdout_backend_prove_same_probe(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            data = streams()
            self.assertEqual(self.verify(path, data), {
                "status": "PASS", "edge_stream": "stderr", "backend_stream": "stdout",
                "edge_timestamp": EDGE_TIME, "backend_timestamp": BACKEND_TIME,
            })
            for id, role in ((EDGE, "edge"), (BACKEND, "backend")):
                evidence = json.loads((path / f"public-{role}-logs.json").read_bytes())
                self.assertEqual(evidence["container_id"], id)
                self.assertEqual(b64decode(evidence["RAW_DOCKER_LOG_STDOUT"]), data[id][0])
                self.assertEqual(b64decode(evidence["RAW_DOCKER_LOG_STDERR"]), data[id][1])
            self.assertEqual(json.loads((path / "public-proxy-probe.json").read_bytes()), {
                "edge_id": EDGE, "backend_id": BACKEND, "since_inclusive": SINCE, "until_exclusive": UNTIL,
                "request_id": TRACE, "target": f"/api/player/time?bgv2_probe={MARKER}", "expected_client": CLIENT,
            })

    def test_missing_exact_edge_trace_preserves_both_captures_but_fails(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaisesRegex(Stop, "CADDY_PROXY_LOG_NOT_UNIQUE"):
                self.verify(path, streams(edge_trace="946256d9-dd11-4981-931b-bce825245329"))
            self.assertTrue((path / "public-edge-logs.json").is_file())
            self.assertTrue((path / "public-backend-logs.json").is_file())
            self.assertEqual(json.loads((path / "public-proxy-probe.json").read_bytes())["request_id"], TRACE)

    def test_spoofed_backend_client_fails(self):
        with TemporaryDirectory() as directory, self.assertRaisesRegex(Stop, "BACKEND_PROXY_CLIENT_MISMATCH"):
            self.verify(Path(directory), streams(backend_ip="203.0.113.99"))
