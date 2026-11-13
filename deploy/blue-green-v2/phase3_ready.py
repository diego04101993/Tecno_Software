"""Sign initial BLUE READY receipts from the private Edge and approved evidence."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
import re
import sys

from gates import DockerProbe, ReadyGate, hex_digest, identifier, sign_receipt
from qualify_r3 import R3_IMAGES, docker_read
from release_state import Stop, canonical, decode, digest, now, read_private, require, validate_release, write_private_atomic
from routectl import DEFAULT_HOSTS


PREVIEW_CA = "/config/preview-tls/pki/authorities/local/root.crt"
ASSET = re.compile(rb'(?:src|href)="(/assets/[A-Za-z0-9._/-]+)"')
PATH = re.compile(r"/[A-Za-z0-9._/-]*")


@dataclass(frozen=True)
class Response:
    body: bytes
    elapsed_ns: int
    mime: str


def request(edge_id, role, path, jwt=None):
    require(PATH.fullmatch(path) and all(part not in (".", "..") for part in path.split("/")),
            "PHASE3_PRIVATE_PATH_INVALID")
    headers = b"" if jwt is None else ('header = "Authorization: Bearer ' + jwt + '"\n').encode()
    host = DEFAULT_HOSTS[role]
    raw = docker_read("exec", "-i", edge_id, "curl", "--config", "-", "--silent", "--show-error",
                      "--noproxy", "*", "--max-time", "10", "--proto", "=https", "--http1.1",
                      "--cacert", PREVIEW_CA, "--connect-to", f"{host}:443:127.0.0.1:8444",
                      "--write-out", "\nBGV2\t%{http_code}\t%{time_total}\t%{content_type}",
                      f"https://{host}{path}", input=headers)
    body, marker, metadata = raw.rpartition(b"\nBGV2\t")
    fields = metadata.decode().split("\t")
    require(marker and len(fields) == 3 and fields[0] == "200" and re.fullmatch(r"[0-9]+\.[0-9]{6}", fields[1]),
            "PHASE3_PRIVATE_HTTP_FAILED")
    _, elapsed, mime = fields
    seconds, fraction = elapsed.split(".")
    return Response(body, int(seconds) * 1_000_000_000 + int(fraction) * 1000, mime)


def validate_runtime(response, channel):
    value = decode(response.body)
    require(value["runtime_version"] == 3 and value["channel"]["id"] == channel
            and value["mode"] == "normal" and canonical(value["videowall"]) == canonical({"enabled": False})
            and type(value["outputs"]) is list and len(value["outputs"]) == 1, "PHASE3_R3_RUNTIME_CONTRACT")


def private_checks(call, channel_ids, jwt):
    call("api", "/")
    call("api", "/api/player/time")
    call("api", "/api/auth/me", jwt)
    channels = call("api", "/api/channels/", jwt)
    rows = decode(channels.body)
    require(type(rows) is list and set(channel_ids) <= {item["id"] for item in rows}
            and 0 < channels.elapsed_ns <= 3_000_000_000, "PHASE3_CHANNELS_GATE_FAILED")

    def check(index):
        channel = channel_ids[index % len(channel_ids)]
        validate_runtime(call("api", f"/api/channels/{channel}/runtime-config", jwt), channel)

    for index in range(36):
        check(index)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in range(4):
            results = [pool.submit(check, batch * 4 + offset) for offset in range(4)]
            for result in as_completed(results):
                result.result()

    root = call("frontend", "/")
    assets = set(ASSET.findall(root.body))
    require(root.mime.startswith("text/html") and any(path.endswith(b".js") for path in assets),
            "PHASE3_FRONTEND_INDEX_INVALID")
    for path in sorted(assets):
        asset = call("frontend", path.decode())
        require(asset.body and not asset.mime.startswith("text/html"), "PHASE3_FRONTEND_ASSET_INVALID")
    return {"api": {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                     "channels_elapsed_ns": channels.elapsed_ns, "runtime_serial_ok": 36, "runtime_concurrent_ok": 16,
                     "runtime_clients": 4, "runtime_transport_timeout_s": 10},
            "frontend": {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}}


def read_json(path):
    return decode(read_private(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("qualification", "bindings", "legacy-manifest", "key", "channels", "reviewed-checks", "receipts"):
        parser.add_argument("--" + option, required=True, type=Path)
    for option in ("legacy-sha256", "trusted-proxy-name", "edge-id"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    try:
        require(sys.platform.startswith("linux") and hex_digest(args.edge_id), "PHASE3_LOCAL_EDGE_REQUIRED")
        require(sys.stdin.isatty(), "PHASE3_PRIVATE_JWT_TTY_REQUIRED")
        qualification = read_json(args.qualification)
        release = qualification["release"]
        validate_release(release)
        require(qualification["scope"] == "R3_EXACT_IMAGE_QUALIFICATION" and qualification["status"] == "PASS"
                and release["images"] == R3_IMAGES, "PHASE3_R3_QUALIFICATION_REQUIRED")
        reviewed = read_json(args.reviewed_checks)
        exact = {"qualification_sha256": digest(qualification), "schema_unchanged": True, "media_unchanged": True,
                 "phase3_infra_capacity": True, "pool": "ISOLATED_NOT_APPLICABLE",
                 "contracts": {"windows_legacy": True, "android_normal": True}}
        require(canonical(reviewed) == canonical(exact), "PHASE3_REVIEWED_EVIDENCE_INVALID")
        channels = read_json(args.channels)
        require(type(channels) is list and 0 < len(channels) <= 36 and all(identifier(channel) for channel in channels)
                and len(channels) == len(set(channels)), "PHASE3_CHANNEL_FIXTURE_INVALID")
        probe = DockerProbe(read_json(args.bindings), edge_name=args.trusted_proxy_name,
                            legacy_blue=read_json(args.legacy_manifest), legacy_blue_sha256=args.legacy_sha256)
        before = {role: probe(role, "BLUE") for role in R3_IMAGES}
        original = qualification["roles"]["api"]["runtime"]
        require(all(before["api"][field] == original[field] for field in ("container_id", "pid", "started_at"))
                and all(before[role]["image_id"] == R3_IMAGES[role] for role in R3_IMAGES),
                "PHASE3_R3_READY_IDENTITY_MISMATCH")
        probe.frontend_listener("BLUE", before["frontend"])
        healthy_at = now()
        jwt = getpass("JWT administrativo existente: ")
        require(re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", jwt), "PHASE3_JWT_FORMAT_INVALID")
        checks = private_checks(lambda role, path, token=None: request(args.edge_id, role, path, token), channels, jwt)
        key = read_private(args.key)
        receipts = {}
        for role, expected in before.items():
            require(probe(role, "BLUE") == expected, "PHASE3_R3_READY_IDENTITY_CHANGED")
            context = {"role": role, "slot": "BLUE", "release": release,
                       "identity_hash": digest(expected), "started_at": expected["started_at"]}
            if role == "api":
                context["adoption"] = "LEGACY_BLUE"
                checks[role].update({name: value for name, value in reviewed.items() if name != "qualification_sha256"})
            receipt = sign_receipt("READY", {**context, "healthy_at": healthy_at, "ready_at": now(),
                                              "private_route": True, "checks": checks[role]}, key)
            require(ReadyGate(key)(receipt, context), "PHASE3_READY_RECEIPT_INVALID")
            receipts[role] = receipt
        write_private_atomic(args.receipts, canonical(receipts) + b"\n", replace=False)
    except (Stop, OSError, ValueError, KeyError, TypeError, AttributeError):
        print("STOP: PHASE3_PRIVATE_READINESS_FAILED", file=sys.stderr)
        return 1
    print("PHASE3_BLUE_PRIVATE_READY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
