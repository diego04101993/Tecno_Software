"""Replay the immutable stream-tagged projection of the public Edge records."""

import argparse
import hashlib
import json
from pathlib import Path
from subprocess import CompletedProcess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docker_logs import collect_docker_logs
from release_state import canonical, decode, require


ARCHIVE_SHA256 = "d281ff46beaf11d73951b6eff923508ebdbfb6d3de0273b670920020c119c047"
EDGE_ID = "f8f7f22f4c97cd1983f84d8e5371b9eccdbafe3f1eca3964ec72cba7dd68ed22"


def replay(data):
    require(hashlib.sha256(data).hexdigest() == ARCHIVE_SHA256, "V26_EDGE_ARCHIVE_CHANGED")
    archive = decode(data)["edge_proxy_logs"]
    require(archive["stdout"] == [] and len(archive["stderr"]) == 21, "V26_EDGE_ARCHIVE_STREAM_COUNT_CHANGED")
    # The extraction retained attributes and their stream but omitted the Docker
    # timestamp and request ID. Prefix a replay-only transport timestamp without
    # representing it as an observed time or inventing IDs for those requests.
    stamp = b"2020-01-01T00:00:01.000000000Z "
    stderr = b"".join(stamp + canonical(record) + b"\n" for record in archive["stderr"])

    def docker_replay(*args):
        return CompletedProcess(["replay", *args], 0, b"", stderr)

    capture = collect_docker_logs(docker_replay, EDGE_ID, since="2020-01-01T00:00:00Z",
                                  until="2020-01-01T00:00:02Z")
    lines = capture.timeline(EDGE_ID)
    restored = [decode(line.payload) for line in lines]
    require(all(line.stream == "stderr" for line in lines) and restored == archive["stderr"],
            "V26_EDGE_ARCHIVE_RECORDS_LOST")
    eligible = sum(row["ingress"] == "public" and row["route_generation"] == "0"
                   and row["upstream"] == "172.30.250.3:8000" and row["status"] == 200
                   and row["request_id_present"] is True for row in restored)
    require(eligible > 0, "V26_EDGE_ARCHIVE_PUBLIC_ROUTE_EVIDENCE_MISSING")
    return {
        "collector_replay": "PASS",
        "scope": "PRESERVED_SANITIZED_EDGE_PROJECTION",
        "archive_sha256": ARCHIVE_SHA256,
        "stdout_records": 0,
        "stderr_records": len(lines),
        "eligible_public_r3_records": eligible,
        "original_request_id_correlation": "UNAVAILABLE",
        "original_docker_timestamps": "UNAVAILABLE",
        "player_gate": "NOT_ADJUDICATED",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    print(json.dumps(replay(parser.parse_args().archive.read_bytes()), sort_keys=True))
