"""Collect and verify the existing Phase 3 public proxy evidence."""

import argparse
from ipaddress import ip_address
import os
from pathlib import Path
import re
import subprocess
import sys

from docker_logs import collect_docker_logs, validate_caddy_request_id, verify_caddy_proxy_log
from edge_config import seal_new
from release_state import Stop, canonical, require, require_private


def backend_probe_target(marker, expected_client):
    require(re.fullmatch(r"[0-9a-f]{32}", marker) is not None, "BACKEND_PROXY_MARKER_INVALID")
    try:
        expected_ip = ip_address(expected_client)
    except ValueError:
        raise Stop("BACKEND_PROXY_EXPECTED_CLIENT_INVALID") from None
    return b"/api/player/time?bgv2_probe=" + marker.encode("ascii"), expected_ip


def verify_backend_proxy_log(capture, backend_id, marker, expected_client):
    target, expected_ip = backend_probe_target(marker, expected_client)
    matches = [line for line in capture.timeline(backend_id) if target in line.payload]
    require(len(matches) == 1, "BACKEND_PROXY_LOG_NOT_UNIQUE")
    line = matches[0]
    pattern = (rb'(?:^|\s)(\[?[0-9a-fA-F:.]+\]?):\d+\s+-\s+"GET '
               + re.escape(target) + rb' HTTP/[0-9.]+" 200(?:\s|$)')
    match = re.search(pattern, line.payload)
    require(match is not None, "BACKEND_PROXY_LOG_MISMATCH")
    try:
        actual_ip = ip_address(match[1].decode("ascii").strip("[]"))
    except ValueError:
        raise Stop("BACKEND_PROXY_LOG_MISMATCH") from None
    require(actual_ip == expected_ip, "BACKEND_PROXY_CLIENT_MISMATCH")
    return line


def verify_public_proxy(invoke, *, edge_id, backend_id, since, until, request_id, marker, expected_client, evidence_dir):
    require_private(evidence_dir, directory=True)

    def capture_role(role, container_id):
        path = evidence_dir / f"public-{role}-logs.json"
        return collect_docker_logs(invoke, container_id, since=since, until=until,
                                   preserve=lambda capture: seal_new(path, canonical(capture.evidence()) + b"\n"))

    edge = capture_role("edge", edge_id)
    backend = capture_role("backend", backend_id)
    validate_caddy_request_id(request_id)
    target, expected_ip = backend_probe_target(marker, expected_client)
    seal_new(evidence_dir / "public-proxy-probe.json", canonical({
        "edge_id": edge.container_id, "backend_id": backend.container_id,
        "since_inclusive": edge.since, "until_exclusive": edge.until,
        "request_id": request_id, "target": target.decode("ascii"), "expected_client": str(expected_ip),
    }) + b"\n")
    edge_line = verify_caddy_proxy_log(edge, edge_id, request_id=request_id,
                                      generation=0, upstream="172.30.250.3:8000")
    backend_line = verify_backend_proxy_log(backend, backend_id, marker, expected_client)
    return {"status": "PASS", "edge_stream": edge_line.stream, "backend_stream": backend_line.stream,
            "edge_timestamp": edge_line.timestamp, "backend_timestamp": backend_line.timestamp}


def docker_logs(*args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
    return subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *args],
                          capture_output=True, timeout=15, check=False, env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("edge-id", "backend-id", "since", "until", "request-id", "marker", "expected-client"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_public_proxy(docker_logs, **vars(args))
    except (Stop, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, Stop) else "PHASE3_PROXY_EVIDENCE_COLLECTION_FAILED"
        print(f"STOP: {reason}", file=sys.stderr)
        return 1
    print(canonical(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
