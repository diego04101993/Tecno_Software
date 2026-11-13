"""TLS, raw response and WebSocket helpers for the disposable integration run."""

import hashlib
import json
import os
import socket
import ssl
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote


API_HOST = "api.tecnocontrolcloud.com"
APP_HOST = "app.tecnocontrolcloud.com"
PREVIEW_PORTS = {"BLUE": 8444, "GREEN": 8443}
QA_CADDY_CA = "/data/caddy/pki/authorities/local/root.crt"
QA_CLIENT_CA = "/tmp/bgv2-qa-root.crt"


def utc_now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes
    elapsed_ns: int

    def json(self):
        return json.loads(self.body)

    def require(self, status):
        if self.status != status:
            raise AssertionError(f"Expected HTTP {status}, got HTTP {self.status}")
        return self


def split_headers(raw):
    header, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("HTTP response did not include a complete header")
    first, *rest = header.decode("iso-8859-1").split("\r\n")
    status = int(first.split(" ", 2)[1])
    headers = {}
    for line in rest:
        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()
    if status < 200 and status != 101:
        return split_headers(body)
    return status, headers, body


class QAHTTP:
    def __init__(self, docker, edge, client, edge_address, *, preview_ca=QA_CADDY_CA, public_port=443):
        self.docker = docker
        self.edge = edge
        self.client = client
        self.edge_address = edge_address
        self.preview_ca = preview_ca
        self.public_port = public_port

    def request(self, host, path, *, method="GET", headers=None, body=None, slot=None, timeout=10):
        headers = headers or {}
        if isinstance(body, dict):
            body = json.dumps(body, separators=(",", ":")).encode()
            headers = {**headers, "Content-Type": "application/json"}
        address = f"127.0.0.1:{PREVIEW_PORTS[slot]}" if slot else f"{self.edge_address}:{self.public_port}"
        args = ["curl", "--silent", "--show-error", "--include", "--http1.1", "--noproxy", "*",
                "--max-time", str(timeout), "--connect-to", f"{host}:443:{address}",
                "--cacert", self.preview_ca if slot else QA_CLIENT_CA,
                "--request", method, "--write-out", "\nBGV2=%{time_total}"]
        args.extend(part for key, value in headers.items() for part in ("--header", f"{key}: {value}"))
        if body is not None:
            args += ["--data-binary", "@-"]
        proc = self.docker.execute(self.edge if slot else self.client, *args, "https://" + host + path, input=body,
                                   timeout=timeout + 10)
        if proc.returncode:
            raise RuntimeError(f"QA HTTPS {method} failed with transport RC {proc.returncode}")
        raw, time_separator, seconds = proc.stdout.rpartition(b"\nBGV2=")
        if not time_separator:
            raise RuntimeError("QA HTTPS measurement missing")
        status, response_headers, response_body = split_headers(raw)
        return Response(status, response_headers, response_body, round(float(seconds) * 1_000_000_000))

    def disposable_direct_api(self, address, path, *, headers, body):
        """Exercise synthetic writes on a slot before making it READY."""
        args = ["curl", "--silent", "--show-error", "--include", "--http1.1", "--noproxy", "*",
                "--max-time", "10", "--request", "POST", "--data-binary", "@-"]
        args.extend(part for key, value in headers.items() for part in ("--header", f"{key}: {value}"))
        proc = self.docker.execute(self.edge, *args, f"http://{address}:8000" + path, input=body)
        if proc.returncode:
            raise RuntimeError("Disposable direct API write transport failure")
        status, response_headers, response_body = split_headers(proc.stdout)
        return Response(status, response_headers, response_body, 0)

    def websocket(self, token, client_id, linux_client):
        return WebSocketRelay(self.docker, linux_client, self.edge_address, token, client_id)


class QAWebSocket:
    """One TLS upgrade and application ACK without external QA dependencies."""

    def __init__(self, address, tls_context, token, client_id):
        wire = socket.create_connection((address, 443), timeout=4)
        self.transport = tls_context.wrap_socket(wire, server_hostname=API_HOST)
        self.stream = self.transport.makefile("rb")
        # RFC 6455 key must decode to 16 bytes.
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        self.transport.sendall((
            f"GET /api/realtime/ws/clients/{client_id}?token={quote(token)} HTTP/1.1\r\n"
            f"Host: {API_HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        status = self.stream.readline(4096).split()
        if len(status) < 2 or status[1] != b"101":
            self.close()
            raise AssertionError("Authenticated product WebSocket upgrade failed")
        while self.stream.readline(4096) not in {b"\r\n", b""}:
            pass

    def ack(self):
        payload = b'{"type":"qa"}'
        mask = os.urandom(4)
        wire = bytes((0x81, 0x80 | len(payload))) + mask
        wire += bytes(byte ^ mask[i % len(mask)] for i, byte in enumerate(payload))
        self.transport.sendall(wire)
        opcode, data = self.frame()
        if opcode != 1 or json.loads(data) != {"type": "ack"}:
            raise AssertionError("Product WebSocket ACK failed")

    def frame(self):
        header = self.stream.read(2)
        if not header:
            return None, b""
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self.stream.read(2), "big")
        elif length == 127:
            raise AssertionError("Unexpected QA WebSocket frame size")
        return opcode, self.stream.read(length)

    def close(self):
        self.stream.close()
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def multipart(client_id, data):
    boundary = "bgv2-qa-" + hashlib.sha256(data).hexdigest()[:20]
    parts = []
    for key, value in (("name", "QA cross-slot asset"), ("client_id", client_id)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"qa.png\"\r\n"
                  "Content-Type: image/png\r\n\r\n").encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts) + data + f"\r\n--{boundary}--\r\n".encode()


class WebSocketRelay:
    def __init__(self, docker, container, address, token, client_id):
        self.child = subprocess.Popen([*docker.command, "exec", "-i", container, "python",
                                       "/tooling/tests/qa_http.py"], env=docker.env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            self._command(json.dumps({"address": address, "token": token, "client_id": client_id}), "OPEN")
        except BaseException:
            self.close()
            raise

    def _command(self, instruction, expected=None):
        self.child.stdin.write(instruction + "\n")
        self.child.stdin.flush()
        reply = self.child.stdout.readline().strip()
        if expected is not None and reply != expected:
            safe_reply = reply if reply in {"ERROR_AssertionError", "ERROR_OSError", "ERROR_TypeError",
                                            "ERROR_ValueError", "ERROR_SSLCertVerificationError"} else "UNKNOWN"
            raise AssertionError(f"Disposable WebSocket relay expected {expected}; observed {safe_reply}")
        return reply

    def ack(self):
        self._command("ack", "ACK")

    def after_reload(self):
        reply = self._command("frame")
        if reply not in {"CLOSED", "OPEN"}:
            raise AssertionError("Disposable WebSocket observation failed")
        return reply

    def close(self):
        self.child.stdin.close()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)
        self.child.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def socket_relay():
    import sys

    arguments = json.loads(sys.stdin.readline())
    context = ssl.create_default_context(cafile=QA_CLIENT_CA)
    context.set_alpn_protocols(["http/1.1"])
    try:
        client = QAWebSocket(tls_context=context, **arguments)
    except (AssertionError, OSError, TypeError, ValueError) as error:
        print("ERROR_" + type(error).__name__, flush=True)
        return
    with client:
        print("OPEN", flush=True)
        for operation in sys.stdin:
            if operation.strip() == "ack":
                client.ack()
                print("ACK", flush=True)
            elif operation.strip() == "frame":
                try:
                    opcode, _ = client.frame()
                except TimeoutError:
                    print("OPEN", flush=True)
                else:
                    print("CLOSED" if opcode in (None, 8) else "OPEN", flush=True)


if __name__ == "__main__":
    socket_relay()
