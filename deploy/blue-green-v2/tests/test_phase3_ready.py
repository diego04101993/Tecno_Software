from collections import Counter
from copy import deepcopy
import json
from threading import Lock
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from legacy_frontend_blue import R3_POLICY
from phase3_ready import PREVIEW_CA, Response, private_checks, request, validate_reviewed_checks
from release_state import Stop, digest


CHANNEL = str(UUID(int=1))


class ReviewPolicyTests(TestCase):
    def setUp(self):
        release = R3_POLICY.release
        self.qualification = {"scope": "R3_EXACT_IMAGE_QUALIFICATION", "status": "PASS", "release": release,
                              "roles": {role: {"image_id": image} for role, image in release["images"].items()}}
        self.reviewed = {"qualification_sha256": digest(self.qualification), "schema_unchanged": True,
                         "media_unchanged": True, "phase3_infra_capacity": True, "pool": "ISOLATED_NOT_APPLICABLE",
                         "phase3_product_contracts": "UNCHANGED_PRODUCT_NOT_REQUALIFIED",
                         "phase3_real_player_continuity_gate": "REQUIRED_AFTER_HANDOFF"}

    def test_exact_r3_has_no_historical_product_contract_precondition(self):
        self.assertEqual(validate_reviewed_checks(self.qualification, self.reviewed), R3_POLICY.release)

    def test_changed_backend_release_or_qualified_image_cannot_reuse_phase3_policy(self):
        different_image = "sha256:" + "6" * 64
        for target in ("pinned", "observed"):
            with self.subTest(target=target):
                qualification = deepcopy(self.qualification)
                if target == "pinned":
                    qualification["release"]["images"]["api"] = different_image
                else:
                    qualification["roles"]["api"]["image_id"] = different_image
                with self.assertRaisesRegex(Stop, "PHASE3_R3_QUALIFICATION_REQUIRED"):
                    validate_reviewed_checks(qualification, {**self.reviewed, "qualification_sha256": digest(qualification)})

    def test_review_stays_bound_to_qualification_and_defers_continuity(self):
        for override in ({"qualification_sha256": "3" * 64}, {"phase3_real_player_continuity_gate": "PASS"},
                         {"media_unchanged": False}, {"phase3_infra_capacity": False}):
            with self.subTest(override=override), self.assertRaisesRegex(Stop, "PHASE3_REVIEWED_EVIDENCE_INVALID"):
                validate_reviewed_checks(self.qualification, {**self.reviewed, **override})


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
