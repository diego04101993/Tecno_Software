"""Linux passive collector for Edge's dedicated IPv4 HTTP upstream link.

Run in the Edge network namespace using a private stdout pipe and CAP_NET_RAW
only. A locked kernel filter limits the socket to the exact two endpoints; all
capabilities are dropped after the socket is configured. No pcap is written.
"""

import argparse
from collections import deque
import ctypes
import json
import os
import re
import socket
import struct
import sys
from time import monotonic, monotonic_ns, time_ns
from uuid import UUID


TELEMETRY = {b"/api/player/heartbeat": "heartbeat", b"/api/player/playback-events": "playback-events"}
HEADER_MAX = 32768
ACK_MAX = 1024
MAX_ACTIVE = 1024
ETH_ALL = 3
SOL_PACKET = 263
PACKET_STATISTICS = 6
PHASE3_CAPTURE_SECONDS = 165
NS_PER_SECOND = 1_000_000_000


class CaptureError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CaptureError(code)


def fields(head, selected):
    result = {}
    for line in head[1:]:
        name, separator, value = line.partition(b":")
        require(separator and name and not name.startswith((b" ", b"\t")), "BAD_HTTP_HEADER")
        name = name.lower()
        if name in selected:
            require(name not in result, "AMBIGUOUS_HTTP_HEADER")
            result[name] = value.strip()
    return result


def observation_stamp():
    observed_monotonic_ns = monotonic_ns()
    return time_ns(), observed_monotonic_ns


class Conversation:
    def __init__(self, emit, *, phase3_monotonic=False):
        self.emit = emit
        self.phase3_monotonic = phase3_monotonic
        self.requests = deque()
        self.request = Stream(self, True)
        self.response = Stream(self, False)
        self.half_closed = set()
        self.disabled = False

    def outstanding(self):
        candidates = list(self.requests)
        if self.response.current is not None:
            candidates.append(self.response.current)
        keys = ("trace", "endpoint", "started_ns")
        if self.phase3_monotonic:
            keys += ("request_start_monotonic_ns",)
        return [{key: item[key] for key in keys} for item in candidates if item.get("trace")]


class Stream:
    def __init__(self, conversation, request):
        self.conversation = conversation
        self.request = request
        self.next_seq = None
        self.pending = {}
        self.header = bytearray()
        self.chunk = bytearray()
        self.body = bytearray()
        self.current = None
        self.remaining = 0
        self.phase = "head"
        self.request_start = None
        self.response_complete = None

    def feed_tcp(self, seq, syn, payload, observed=None):
        if syn:
            expected = (seq + 1) & 0xffffffff
            if self.next_seq is None:
                self.next_seq = expected
            seq = expected
        if self.next_seq is None or self.conversation.disabled:
            return
        if self.conversation.phase3_monotonic and observed is None:
            observed = observation_stamp()
        self.insert(seq, payload, observed)
        while self.pending:
            for start in self.pending:
                if self.offset(start) <= 0:
                    break
            else:
                break
            self.insert(start, *self.pending.pop(start))

    def offset(self, seq):
        return ((seq - self.next_seq + 0x80000000) & 0xffffffff) - 0x80000000

    def insert(self, seq, payload, observed):
        offset = self.offset(seq)
        if offset > 0:
            if not payload:
                return
            require(len(self.pending) < 64 and offset + len(payload) <= 131072, "TCP_GAP_LIMIT")
            old = self.pending.get(seq)
            require(old is None or old[0] == payload, "AMBIGUOUS_TCP_OVERLAP")
            if old is None:
                self.pending[seq] = payload, observed
            return
        payload = payload[-offset:]
        if payload:
            fragments = self.observed_fragments(payload, observed)
            self.next_seq = (self.next_seq + len(payload)) & 0xffffffff
            for left, right, stamp in fragments:
                self.feed_http(payload[left:right], stamp)

    def observed_fragments(self, payload, observed):
        """Retain when already buffered bytes were first observed."""
        fragments = [(0, len(payload), observed)]
        if not self.conversation.phase3_monotonic:
            return fragments
        for start, (queued, stamp) in self.pending.items():
            offset = self.offset(start)
            left, right = max(0, offset), min(len(payload), offset + len(queued))
            if left >= right:
                continue
            require(payload[left:right] == queued[left - offset:right - offset], "AMBIGUOUS_TCP_OVERLAP")
            fragments = earlier_fragment(fragments, left, right, stamp)
        return fragments

    def finish(self):
        if self.current is not None:
            event = self.current
            if event["endpoint"] == "heartbeat" and event["status"] == 200:
                ack = json.loads(self.body, object_pairs_hook=unique)
                require(type(ack) is dict and set(ack) == {"ok", "received_at", "channel_id"}
                        and ack["ok"] is True, "BAD_ACK_SHAPE")
                channel = ack["channel_id"]
                require(type(channel) is str and str(UUID(channel)) == channel
                        and isinstance(ack["received_at"], str) and len(ack["received_at"]) <= 40, "BAD_ACK_FIELDS")
                event.update(channel_id=channel, received_at=ack["received_at"])
            if self.conversation.phase3_monotonic:
                utc_ns, mono_ns = self.response_complete
                event.update(response_complete_ns=utc_ns, response_complete_monotonic_ns=mono_ns)
            self.conversation.emit(event)
        self.body.clear()
        self.current = None
        self.response_complete = None
        self.phase = "head"

    def headers(self, head):
        line = head[0].split()
        values = fields(head, {b"content-length", b"transfer-encoding", b"content-encoding", b"x-request-id"})
        length = values.get(b"content-length", b"0")
        require(length.isdigit() and len(length) <= 12, "BAD_HTTP_LENGTH")
        transfer = values.get(b"transfer-encoding")
        require(transfer is None or transfer.lower() == b"chunked" and b"content-length" not in values,
                "UNSUPPORTED_HTTP_FRAMING")
        self.remaining = int(length)
        self.phase = "chunk-size" if transfer else "body"
        if self.request:
            require(len(line) == 3 and line[2] == b"HTTP/1.1" and len(self.conversation.requests) < 16,
                    "BAD_HTTP_REQUEST_SEQUENCE")
            endpoint = TELEMETRY.get(line[1]) if line[0] == b"POST" else None
            record = {"method": line[0]}
            if endpoint:
                trace = values.get(b"x-request-id", b"").decode("ascii")
                require(str(UUID(trace)) == trace, "BAD_EDGE_TRACE")
                record.update(trace=trace, endpoint=endpoint)
                if self.conversation.phase3_monotonic:
                    utc_ns, mono_ns = self.request_start
                    record.update(started_ns=utc_ns, request_start_monotonic_ns=mono_ns)
                else:
                    record["started_ns"] = time_ns()
            self.conversation.requests.append(record)
        else:
            require(len(line) >= 2 and line[0] == b"HTTP/1.1" and len(line[1]) == 3 and line[1].isdigit()
                    and self.conversation.requests, "BAD_HTTP_RESPONSE_SEQUENCE")
            status = int(line[1])
            if status == 101:
                require(not any(item.get("trace") for item in self.conversation.requests), "TELEMETRY_UPGRADE")
                self.conversation.disabled = True
                return
            if 100 <= status < 200:
                self.phase = "head"
                return
            request = self.conversation.requests.popleft()
            method = request.pop("method")
            if method == b"HEAD" or status in (204, 304):
                self.phase, self.remaining = "body", 0
            if request:
                self.current = {**request, "status": status}
                if request["endpoint"] == "heartbeat" and status == 200:
                    require(not transfer and 0 < self.remaining <= ACK_MAX
                            and b"content-encoding" not in values, "UNSUPPORTED_ACK_FRAMING")
            if (not transfer and b"content-length" not in values and method != b"HEAD"
                    and status not in (204, 304)):
                # If a non-telemetry response is framed by connection close,
                # no further HTTP request can legitimately reuse this flow.
                if self.current is not None:
                    raise CaptureError("UNSUPPORTED_HTTP_FRAMING")
                self.conversation.disabled = True
                return
        if self.phase == "body" and self.remaining == 0:
            self.finish()

    def feed_http(self, data, observed=None):
        phase3 = self.conversation.phase3_monotonic
        if phase3 and observed is None:
            observed = observation_stamp()
        while data and not self.conversation.disabled:
            if phase3:
                if self.request and self.phase == "head" and not self.header:
                    self.request_start = observed
                if not self.request and (self.response_complete is None or observed[1] > self.response_complete[1]):
                    self.response_complete = observed
            if self.phase in ("body", "chunk-body"):
                take = min(self.remaining, len(data))
                if self.current and self.current["endpoint"] == "heartbeat" and self.current["status"] == 200:
                    self.body.extend(data[:take])
                self.remaining -= take
                data = data[take:]
                if self.remaining == 0:
                    if self.phase == "chunk-body":
                        self.phase = "chunk-end"
                    else:
                        self.finish()
                continue
            if self.phase == "chunk-end":
                take = min(2 - len(self.chunk), len(data))
                self.chunk.extend(data[:take])
                data = data[take:]
                if len(self.chunk) == 2:
                    require(self.chunk == b"\r\n", "BAD_HTTP_CHUNK_DELIMITER")
                    self.chunk.clear()
                    self.phase = "chunk-size"
                continue
            self.header.extend(data)
            delimiter = b"\r\n\r\n" if self.phase == "head" else b"\r\n"
            stop = self.header.find(delimiter)
            require((len(self.header) if stop < 0 else stop) <= HEADER_MAX, "HTTP_HEADER_LIMIT")
            if stop < 0:
                return
            row = bytes(self.header[:stop])
            data = bytes(self.header[stop + len(delimiter):])
            self.header.clear()
            if self.phase == "head":
                self.headers(row.split(b"\r\n"))
            elif self.phase == "trailers":
                if not row:
                    self.finish()
            else:
                size = row.partition(b";")[0]
                require(0 < len(size) <= 12 and all(c in b"0123456789abcdefABCDEF" for c in size), "BAD_HTTP_CHUNK_SIZE")
                self.remaining = int(size, 16)
                self.phase = "chunk-body" if self.remaining else "trailers"


def earlier_fragment(fragments, left, right, stamp):
    result = []
    for start, stop, previous in fragments:
        begin, end = max(start, left), min(stop, right)
        if begin >= end or previous[1] <= stamp[1]:
            result.append((start, stop, previous))
            continue
        if start < begin:
            result.append((start, begin, previous))
        result.append((begin, end, stamp))
        if end < stop:
            result.append((end, stop, previous))
    return result


def unique(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "DUPLICATE_ACK_FIELD")
    return result


def parse_packet(frame):
    require(len(frame) >= 54 and frame[12:14] == b"\x08\x00" and frame[23] == 6, "BAD_FILTERED_PACKET")
    end = 14 + int.from_bytes(frame[16:18], "big")
    tcp = 14 + (frame[14] & 15) * 4
    require(tcp >= 34 and tcp + 20 <= end <= len(frame), "TRUNCATED_PACKET")
    src, dst, seq = struct.unpack_from("!HHI", frame, tcp)
    payload_start = tcp + (frame[tcp + 12] >> 4) * 4
    require(tcp + 20 <= payload_start <= end, "BAD_TCP_HEADER")
    return frame[26:30], src, dst, seq, frame[tcp + 13], frame[payload_start:end]


def filter_program(edge, backend, port):
    # cBPF over the Ethernet frame. Reject non-IPv4/TCP/fragments. IPv4
    # header size determines the two TCP port offsets. No other link traffic
    # (including public TLS or other backends) reaches the Python process.
    edge, backend = int.from_bytes(edge, "big"), int.from_bytes(backend, "big")
    return [
        (0x28, 0, 0, 12), (0x15, 0, 17, 0x0800),
        (0x30, 0, 0, 23), (0x15, 0, 15, 6),
        (0x28, 0, 0, 20), (0x45, 13, 0, 0x3FFF),
        (0x20, 0, 0, 26), (0x15, 0, 5, edge),
        (0x20, 0, 0, 30), (0x15, 0, 9, backend),
        (0xB1, 0, 0, 14), (0x48, 0, 0, 16), (0x15, 7, 6, port),
        (0x15, 0, 5, backend), (0x20, 0, 0, 30), (0x15, 0, 3, edge),
        (0xB1, 0, 0, 14), (0x48, 0, 0, 14), (0x15, 1, 0, port),
        (0x06, 0, 0, 0), (0x06, 0, 0, 65550),
    ]


class FilterInstruction(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Filter(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(FilterInstruction))]


def constrain(capture, interface, edge, backend, port):
    code = filter_program(edge, backend, port)
    instructions = (FilterInstruction * len(code))(*(FilterInstruction(*item) for item in code))
    capture.setsockopt(socket.SOL_SOCKET, 26, bytes(Filter(len(code), instructions)))  # SO_ATTACH_FILTER
    capture.setsockopt(socket.SOL_SOCKET, 44, 1)  # SO_LOCK_FILTER
    capture.bind((interface, ETH_ALL))
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(38, 1, 0, 0, 0) == 0, "NO_NEW_PRIVILEGES_FAILED")
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)
    caps = (ctypes.c_uint32 * 6)()
    require(libc.capset(header, caps) == 0, "CAPABILITY_DROP_FAILED")
    # An attached SOCK_RAW descriptor could otherwise still transmit even
    # after capability drop. Disable every syscall that can send on it.
    # x86_64 Linux is the approved deployment platform for this collector.
    require(sys.platform.startswith("linux") and struct.calcsize("P") == 8
            and os.uname().machine == "x86_64", "CAPTURE_REQUIRES_LINUX_X86_64")
    install_send_guard(libc, capture.fileno())


def install_send_guard(libc, fd):
    # x86_64 syscall ABI. Block sendto/sendmsg/sendmmsg and operations with
    # alternative asynchronous/splice write paths. Restrict write/writev to
    # descriptors other than the capture socket so the pipe can report.
    numbers = (41, 42, 44, 46, 307, 275, 278, 425, 426, 427, 40, 326, 296, 328,
               32, 33, 72, 292, 101, 310, 311)
    program = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e), (0x06, 0, 0, 0), (0x20, 0, 0, 0),
               (0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050001)]  # Block the alternate x32 syscall ABI.
    for number in numbers:
        program.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x00050001)))
    program.extend(((0x15, 1, 0, 1), (0x15, 0, 3, 20), (0x20, 0, 0, 16),
                    (0x15, 0, 1, fd), (0x06, 0, 0, 0x00050001), (0x06, 0, 0, 0x7fff0000)))
    instructions = (FilterInstruction * len(program))(*(FilterInstruction(*item) for item in program))
    seccomp = Filter(len(program), instructions)
    require(libc.prctl(22, 2, ctypes.byref(seccomp), 0, 0) == 0, "PASSIVE_SOCKET_GUARD_FAILED")


def observe(interface, edge, backend, port, seconds, emit, *, phase3_monotonic=False):
    edge, backend = map(socket.inet_aton, (edge, backend))
    require(edge != backend and 0 < port < 65536 and 1 <= seconds <= 600, "INVALID_CAPTURE_SCOPE")
    if phase3_monotonic:
        require(seconds == PHASE3_CAPTURE_SECONDS, "INVALID_PHASE3_CAPTURE_LIFETIME")
        namespace = os.readlink("/proc/self/ns/time")
        require(re.fullmatch(r"time:\[\d+\]", namespace), "INVALID_CAPTURE_TIME_NAMESPACE")
    # Open with protocol zero so no packet queues before the filter is installed.
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0) as capture:
        constrain(capture, interface, edge, backend, port)
        capture.settimeout(0.1)
        if phase3_monotonic:
            started, ready_ns = observation_stamp()
            deadline_ns = ready_ns + seconds * NS_PER_SECOND
            emit({"type": "ready", "started_ns": started})
            emit({"type": "monotonic_clock", "ready_monotonic_ns": ready_ns,
                  "deadline_monotonic_ns": deadline_ns, "time_namespace": namespace})
        else:
            emit({"type": "ready", "started_ns": time_ns()})
            deadline = monotonic() + seconds
        flows = {}
        def emit_response(event):
            emit({"type": "response", **event})
        while (monotonic_ns() < deadline_ns if phase3_monotonic else monotonic() < deadline):
            try:
                frame = capture.recv(65550)
            except TimeoutError:
                continue
            observed = observation_stamp() if phase3_monotonic else None
            if observed is not None and observed[1] >= deadline_ns:
                break
            src, src_port, dst_port, seq, flags, payload = parse_packet(frame)
            outbound = src == edge
            flow_port = src_port if outbound else dst_port
            syn = bool(flags & 2)
            if outbound and syn:
                require(len(flows) < MAX_ACTIVE or flow_port in flows, "ACTIVE_FLOW_LIMIT")
                if flow_port not in flows:
                    flows[flow_port] = Conversation(emit_response, phase3_monotonic=phase3_monotonic)
            flow = flows.get(flow_port)
            if flow is None:
                # A socket predating observer startup cannot be attributed.
                continue
            stream = flow.request if outbound else flow.response
            stream.feed_tcp(seq, syn, payload, observed=observed)
            if flags & 1:
                flow.half_closed.add(outbound)
            if flags & 4 or len(flow.half_closed) == 2:
                for event in flow.outstanding():
                    emit({"type": "incomplete", **event})
                flows.pop(flow_port)
        for flow in flows.values():
            for event in flow.outstanding():
                emit({"type": "incomplete", **event})
        packets, drops = struct.unpack("II", capture.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
        done = {"type": "done", "ended_ns": time_ns(), "kernel_packets": packets, "kernel_dropped": drops}
        if phase3_monotonic:
            done["ended_monotonic_ns"] = monotonic_ns()
        emit(done)
        require(drops == 0, "CAPTURE_KERNEL_DROPS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface")
    parser.add_argument("edge_ip")
    parser.add_argument("backend_ip")
    parser.add_argument("seconds", type=int)
    parser.add_argument("--phase3-monotonic", action="store_true")
    args = parser.parse_args()
    try:
        observe(args.interface, args.edge_ip, args.backend_ip, 8000, args.seconds,
                lambda event: print(json.dumps(event), flush=True), phase3_monotonic=args.phase3_monotonic)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        print('{"type":"failed"}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
