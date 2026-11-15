from base64 import b64decode
import subprocess
import unittest

from docker_logs import collect_docker_logs, verify_caddy_proxy_log
from release_state import Stop, canonical, digest


EDGE = "e" * 64
WRONG_EDGE = "a" * 64
SINCE = "2026-11-15T17:33:51.766656001Z"
UNTIL = "2026-11-15T17:33:55.104684Z"
UPSTREAM = "172.30.250.3:8000"


def trace(index=1):
    return f"919a5031-3d14-4ceb-a36b-{index:012d}"


def row(index=1, **changes):
    event = {"logger": "http.log.access.log0", "status": 200, "ingress": "public",
             "route_generation": "0", "upstream": UPSTREAM, "request_id": trace(index)}
    return {**event, **changes}


def line(event=None, stamp="2026-11-15T17:33:52.000000003Z"):
    return stamp.encode() + b" " + canonical(row() if event is None else event) + b"\n"


def collected(stdout=b"", stderr=b"", *, container=EDGE):
    def invoke(*args):
        return subprocess.CompletedProcess(["docker", "--host", "unix:///var/run/docker.sock", *args],
                                           0, stdout, stderr)
    return collect_docker_logs(invoke, container, since=SINCE, until=UNTIL)


def check(capture, index=1):
    return verify_caddy_proxy_log(capture, EDGE, request_id=trace(index), generation=0, upstream=UPSTREAM)


class DockerLogTests(unittest.TestCase):
    def test_stdout_only(self):
        capture = collected(stdout=line())
        self.assertEqual(check(capture).stream, "stdout")
        self.assertEqual(capture.stderr, b"")

    def test_stderr_only(self):
        capture = collected(stderr=line())
        self.assertEqual(check(capture).stream, "stderr")
        self.assertEqual(capture.stdout, b"")

    def test_both_streams_merge_by_exact_nanosecond_timestamp(self):
        later = line(row(2), "2026-11-15T17:33:52.000000003Z")
        earlier = line(row(1), "2026-11-15T11:33:52.000000002-06:00")
        capture = collected(stdout=later, stderr=earlier)
        timeline = capture.timeline(EDGE)
        self.assertEqual([entry.stream for entry in timeline], ["stderr", "stdout"])
        self.assertEqual(timeline[1].timestamp_ns - timeline[0].timestamp_ns, 1)
        self.assertEqual(check(capture).stream, "stderr")
        self.assertEqual(check(capture, 2).stream, "stdout")

    def test_nineteen_caddy_records_in_stderr(self):
        capture = collected(stderr=b"".join(line(row(index)) for index in range(1, 20)))
        self.assertEqual(len(capture.timeline(EDGE)), 19)
        for index in range(1, 20):
            with self.subTest(index=index):
                self.assertEqual(check(capture, index).stream, "stderr")

    def test_both_empty_produce_no_positive_evidence(self):
        capture = collected()
        self.assertEqual(capture.timeline(EDGE), [])
        with self.assertRaisesRegex(Stop, "^CADDY_PROXY_LOG_NOT_UNIQUE$"):
            check(capture)

    def test_outside_window_does_not_count(self):
        records = line(row(1), "2026-11-15T17:33:51.766656000Z") + line(row(1), UNTIL)
        capture = collected(stderr=records)
        self.assertEqual(capture.timeline(EDGE), [])
        with self.assertRaisesRegex(Stop, "^CADDY_PROXY_LOG_NOT_UNIQUE$"):
            check(capture)
        self.assertEqual(check(collected(stderr=line(row(), SINCE))).timestamp, SINCE)

    def test_wrong_container_cannot_supply_evidence(self):
        capture = collected(stderr=line(), container=WRONG_EDGE)
        with self.assertRaisesRegex(Stop, "^DOCKER_LOG_CONTAINER_MISMATCH$"):
            check(capture)

    def test_wrong_container_in_docker_invocation_stops_collection(self):
        def invoke(*args):
            return subprocess.CompletedProcess(["docker", *args[:-1], WRONG_EDGE], 0, line(), b"")
        with self.assertRaisesRegex(Stop, "^DOCKER_LOG_COMMAND_MISMATCH$"):
            collect_docker_logs(invoke, EDGE, since=SINCE, until=UNTIL)

    def test_malformed_record_does_not_count(self):
        valid_prefix = b"2026-11-15T17:33:52.000000003Z "
        invalid_json = valid_prefix + b'{"request_id":"not-json\n'
        invalid_timestamp = b"not-a-timestamp " + canonical(row()) + b"\n"
        duplicate_key = valid_prefix + canonical(row())[:-1] + b',"status":200}\n'
        capture = collected(stdout=invalid_json + invalid_timestamp, stderr=duplicate_key)
        with self.assertRaisesRegex(Stop, "^CADDY_PROXY_LOG_NOT_UNIQUE$"):
            check(capture)

    def test_wrong_http_status_route_or_redaction_blocks(self):
        for change in ({"status": "200"}, {"status": 201}, {"route_generation": "1"},
                       {"upstream": "backend-green:8000"}, {"ingress": "preview"}, {"request": {}}):
            with self.subTest(change=change), self.assertRaisesRegex(Stop, "^CADDY_PROXY_LOG_MISMATCH$"):
                check(collected(stderr=line(row(**change))))

    def test_duplicate_across_streams_still_fails_closed(self):
        with self.assertRaisesRegex(Stop, "^CADDY_PROXY_LOG_NOT_UNIQUE$"):
            check(collected(stdout=line(), stderr=line()))

    def test_evidence_round_trips_exact_streams_and_records_provenance(self):
        capture = collected(stdout=b"unparsed diagnostic\xff\r\n", stderr=line())
        evidence = capture.evidence()
        self.assertEqual(b64decode(evidence["RAW_DOCKER_LOG_STDOUT"]), capture.stdout)
        self.assertEqual(b64decode(evidence["RAW_DOCKER_LOG_STDERR"]), capture.stderr)
        self.assertEqual(evidence["container_id"], EDGE)
        self.assertEqual(evidence["time_window"], {"since_inclusive": SINCE, "until_exclusive": UNTIL})
        self.assertEqual(evidence["command_sha256"], digest([
            "docker", "--host", "unix:///var/run/docker.sock", "container", "logs", "--timestamps",
            "--since", SINCE, "--until", UNTIL, EDGE,
        ]))

    def test_invalid_window_fails_before_running_docker(self):
        def invoke(*_):
            self.fail("Invalid window should not start Docker")
        for since, until in ((UNTIL, SINCE), ("not-a-time", UNTIL)):
            with self.subTest(since=since), self.assertRaisesRegex(Stop, "^DOCKER_LOG_WINDOW_INVALID$"):
                collect_docker_logs(invoke, EDGE, since=since, until=until)

    def test_failure_and_timeout_preserve_both_raw_streams_but_supply_no_positive_evidence(self):
        for returncode in (1, None):
            recorded = []

            def invoke(*args):
                command = ["docker", *args]
                if returncode is None:
                    raise subprocess.TimeoutExpired(command, 5, output=line(), stderr=b"partial diagnostic")
                return subprocess.CompletedProcess(command, returncode, line(), b"partial diagnostic")

            with self.subTest(returncode=returncode), self.assertRaisesRegex(Stop, "^DOCKER_LOG_COLLECTION_FAILED$"):
                collect_docker_logs(invoke, EDGE, since=SINCE, until=UNTIL, preserve=recorded.append)
            evidence, = recorded
            self.assertEqual(evidence.returncode, returncode)
            self.assertEqual(evidence.stdout, line())
            self.assertEqual(evidence.stderr, b"partial diagnostic")
            with self.assertRaisesRegex(Stop, "^DOCKER_LOG_COLLECTION_FAILED$"):
                check(evidence)


if __name__ == "__main__":
    unittest.main()
