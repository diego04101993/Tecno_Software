from collections import Counter
import json
from threading import Lock
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from phase3_ready import PREVIEW_CA, Response, private_checks, request
from release_state import Stop


CHANNEL = str(UUID(int=1))


class PrivateReadinessTests(TestCase):
    def test_existing_jwt_is_only_passed_through_stdin(self):
        jwt = "abc.def.ghi"
        with patch("phase3_ready.docker_read", return_value=b'{}\nBGV2\t200\t0.123456\tapplication/json') as read:
            result = request("container-id", "api", "/api/auth/me", jwt)
        self.assertEqual(result, Response(b"{}", 123456000, "application/json"))
        self.assertNotIn(jwt, repr(read.call_args.args))
        self.assertIn(jwt.encode(), read.call_args.kwargs["input"])
        self.assertIn(PREVIEW_CA, read.call_args.args)
        self.assertIn("api.tecnocontrolcloud.com:443:127.0.0.1:8444", read.call_args.args)

    def test_http_failure_and_unsafe_paths_fail_closed(self):
        with patch("phase3_ready.docker_read", return_value=b'{}\nBGV2\t401\t0.000100\tapplication/json') as read:
            with self.assertRaisesRegex(Stop, "PHASE3_PRIVATE_HTTP_FAILED"):
                request("id", "api", "/api/auth/me")
            with self.assertRaisesRegex(Stop, "PHASE3_PRIVATE_PATH_INVALID"):
                request("id", "api", "/media/../file")
        self.assertEqual(read.call_count, 1)

    def test_pre_handoff_checks_have_exact_counts_and_read_only_paths(self):
        requests = Counter()
        lock = Lock()

        def call(role, path, jwt=None):
            with lock:
                requests[role, path] += 1
            if path == "/api/channels/":
                value = [{"id": CHANNEL}]
            elif path.endswith("/runtime-config"):
                value = {"runtime_version": 3, "channel": {"id": CHANNEL}, "mode": "normal",
                         "videowall": {"enabled": False}, "outputs": [{}]}
            elif role == "frontend":
                return (Response(b'<script src="/assets/test.js"></script>', 1, "text/html")
                        if path == "/" else Response(b"const result=1", 1, "text/javascript"))
            else:
                value = {}
            return Response(json.dumps(value).encode(), 1, "application/json")

        result = private_checks(call, [CHANNEL], "secret")
        self.assertEqual(requests["api", f"/api/channels/{CHANNEL}/runtime-config"], 52)
        self.assertEqual(requests["frontend", "/"], 1)
        self.assertTrue(result["frontend"]["assets_ok"])
        self.assertEqual(result["api"]["runtime_concurrent_ok"], 16)
