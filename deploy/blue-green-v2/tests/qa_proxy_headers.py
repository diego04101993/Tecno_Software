"""Verify trusted proxy behavior against the unmodified sealed Uvicorn slots."""

import re
from uuid import uuid4

from qa_http import API_HOST


def _check_forwarded_policy(config, role):
    request = config.proxies[role]["headers"]["request"]
    expected = {
        "X-Forwarded-For": ["{http.request.remote.host}"],
        "X-Forwarded-Host": ["{http.request.host}"],
        "X-Forwarded-Proto": ["https"], "X-Forwarded-Port": ["443"],
        "Host": ["{http.request.host}"], "X-Request-Id": ["{http.request.uuid}"],
    }
    assert request["set"] == expected
    assert {value.lower() for value in request["delete"]} == {"x-real-ip", "forwarded"}


def verify_proxy_headers(topology, http):
    effective, _ = topology.relay.effective()
    for role in ("api", "frontend"):
        # RouteConfig also requires each preview proxy handler to have the
        # identical policy, apart from the expected fixed upstream address.
        _check_forwarded_policy(effective, role)
    for slot, route, client_ip in ((effective.active["api"], None, topology.client_ip),
                                  ("GREEN", "GREEN", "127.0.0.1")):
        marker = uuid4().hex
        forged_id = uuid4().hex
        response = http.request(API_HOST, f"/api/player/time?bgv2_probe={marker}", slot=route, headers={
            "X-Forwarded-For": "203.0.113.99", "X-Real-IP": "203.0.113.99",
            "Forwarded": "for=203.0.113.99;host=invalid.test;proto=http",
            "X-Forwarded-Host": "invalid.test", "X-Forwarded-Proto": "http",
            "X-Forwarded-Port": "81", "X-Request-ID": forged_id,
        }).require(200)
        trace = response.headers["x-request-id"]
        assert trace != forged_id and re.fullmatch(r"[0-9a-f-]{36}", trace)
        backend = topology.containers["api", slot]
        matching = [line for line in topology.docker.run("logs", backend).splitlines() if marker in line]
        assert len(matching) == 1, "Expected one correlated backend access log"
        assert re.search(r"(?:\s|^)" + re.escape(client_ip) + r":\d+\s+-\s+", matching[0]), (
            "Backend did not observe actual proxy peer source IP"
        )
        assert "203.0.113.99" not in matching[0]
        logs = topology.docker.invoke("logs", topology.edge)
        assert logs.returncode == 0
        edge = [line for line in (logs.stdout + logs.stderr).decode().splitlines() if trace in line]
        assert len(edge) == 1 and f"backend-{slot.lower()}:8000" in edge[0]
        assert all(value not in edge[0] for value in (marker, forged_id, "invalid.test", "203.0.113.99"))
    return {"public_client_ip": "PASS", "preview_loopback_ip": "PASS",
            "forwarding_config_parity": "PASS", "trace_replaced_and_redacted": "PASS"}
