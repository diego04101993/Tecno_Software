"""Initial legacy frontend provenance through the qualification and READY entrypoints."""

from hashlib import sha256
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from gates import REVISION_LABEL, TREE_LABEL
from legacy_frontend_blue import LEGACY_FRONTEND_IMAGE_EXACT
from phase3_ready import Response, legacy_frontend_ready_pin, private_checks
from qualify_r3 import R3_CADDY_ARGV, R3_IMAGES, qualify_role, require_private_network_available
from release_state import Stop
from test_phase3_ready import CHANNEL
from test_qualify_r3 import IDS, RELEASE, fixture


class LegacyQualificationTests(TestCase):
    def test_qualified_image_id_is_only_returned_after_manifest_classification(self):
        container, image = fixture("frontend")
        for key in (REVISION_LABEL, TREE_LABEL):
            container["Config"]["Labels"].pop(key)
            image["Config"]["Labels"].pop(key)
        caddyfile = b"disposable historical Caddyfile"
        manifest, pin = {"payload": {}}, "e" * 64

        def read(*args):
            self.assertEqual(args, ("container", "inspect", IDS["frontend"]))
            return json.dumps([container]).encode()

        with (patch("qualify_r3.R3_CADDYFILE_SHA256", sha256(caddyfile).hexdigest()),
              patch("qualify_r3.verify_manifest", return_value={"release": RELEASE, "private_contract": {}}) as verify,
              patch("qualify_r3.classify_historical", return_value=LEGACY_FRONTEND_IMAGE_EXACT) as classify,
              patch("qualify_r3.require_private_network_available") as availability):
            result = qualify_role("frontend", RELEASE, IDS["frontend"], container, container, image,
                                  R3_CADDY_ARGV, caddyfile, legacy_frontend_manifest=manifest,
                                  legacy_frontend_sha256=pin, read=read)
            verify.assert_called_once_with(manifest, pin)
            classify.assert_called_once_with(container, image, read, manifest, pin)
            availability.assert_called_once_with({}, read)
        self.assertEqual(result["frontend_blue_create_image_id"], R3_IMAGES["frontend"])
        self.assertEqual(result["provenance"], "PASS_LEGACY_MANIFEST")
        self.assertEqual(result["frontend_blue_feasibility"], "PASS")

    def test_legacy_qualification_does_not_accept_pin_without_evidence(self):
        container, image = fixture("frontend")
        with self.assertRaisesRegex(Stop, "R3_LEGACY_FRONTEND_MANIFEST_PIN_REQUIRED"):
            qualify_role("frontend", RELEASE, IDS["frontend"], container, container, image,
                         R3_CADDY_ARGV, legacy_frontend_sha256="e" * 64)

    def test_private_creation_requires_free_exact_address_and_alias(self):
        network_id, peer_id = "f" * 64, "e" * 64
        contract = {"network_name": "proxy", "network_id": network_id, "ipv4": "172.30.250.4",
                    "alias": "frontend-blue"}
        network = {"Name": "proxy", "Id": network_id, "Driver": "bridge", "Internal": True,
                   "IPAM": {"Config": [{"Subnet": "172.30.250.0/24", "Gateway": "172.30.250.1"}]},
                   "Containers": {peer_id: {"IPv4Address": "172.30.250.3/24"}}}
        endpoint = {"NetworkID": network_id, "Aliases": ["backend"], "DNSNames": None}
        peer = {"Id": peer_id, "Name": "/backend", "NetworkSettings": {"Networks": {"proxy": endpoint}}}

        def read(kind, operation, name):
            self.assertEqual(operation, "inspect")
            if kind == "network":
                self.assertEqual(name, "proxy")
                return json.dumps([network]).encode()
            self.assertEqual(name, peer_id)
            return json.dumps([peer]).encode()

        require_private_network_available(contract, read)
        for address in ("172.30.250.1", "172.30.250.3", "172.30.250.255"):
            with self.subTest(address=address), self.assertRaisesRegex(Stop, "PRIVATE_ADDRESS_OCCUPIED"):
                require_private_network_available({**contract, "ipv4": address}, read)
        endpoint["DNSNames"] = ["frontend-blue"]
        with self.assertRaisesRegex(Stop, "PRIVATE_ALIAS_OCCUPIED"):
            require_private_network_available(contract, read)

    def test_readiness_requires_same_pin_and_exact_creation_image(self):
        pin = "e" * 64
        frontend = {"frontend_classifier": LEGACY_FRONTEND_IMAGE_EXACT, "provenance": "PASS_LEGACY_MANIFEST",
                    "legacy_frontend_manifest_sha256": pin, "frontend_blue_feasibility": "PASS",
                    "frontend_blue_create_image_id": R3_IMAGES["frontend"]}
        qualification = {"roles": {"frontend": frontend}}
        probe = SimpleNamespace(legacy_frontend_blue_sha256=pin)
        self.assertEqual(legacy_frontend_ready_pin(qualification, probe), pin)
        for field, value in (("legacy_frontend_manifest_sha256", "f" * 64),
                             ("frontend_blue_create_image_id", "mutable-tag"),
                             ("frontend_blue_feasibility", "FAIL")):
            with self.subTest(field=field), patch.dict(frontend, {field: value}):
                with self.assertRaisesRegex(Stop, "PHASE3_LEGACY_FRONTEND_QUALIFICATION_MISMATCH"):
                    legacy_frontend_ready_pin(qualification, probe)

    def test_private_http_checks_match_exact_index_and_assets(self):
        index = b'<script src="/assets/exact.js"></script>'
        asset = b"document.title = 'R3';"
        content = {"index_sha256": sha256(index).hexdigest(),
                   "asset_sha256": {"/assets/exact.js": sha256(asset).hexdigest()}}
        responses = {"/": index, "/assets/exact.js": asset}

        def call(role, path, jwt=None):
            if role == "frontend":
                return Response(responses[path], 1, "text/html" if path == "/" else "text/javascript")
            if path == "/api/channels/":
                value = [{"id": CHANNEL}]
            elif path.endswith("/runtime-config"):
                value = {"runtime_version": 3, "channel": {"id": CHANNEL}, "mode": "normal",
                         "videowall": {"enabled": False}, "outputs": [{}]}
            else:
                value = {}
            return Response(json.dumps(value).encode(), 1, "application/json")

        self.assertTrue(private_checks(call, [CHANNEL], "test", legacy_frontend_content=content)["frontend"]["assets_ok"])
        for path, code in (("/", "INDEX"), ("/assets/exact.js", "ASSET")):
            with self.subTest(path=path), patch.dict(responses, {path: responses[path] + b" "}):
                with self.assertRaisesRegex(Stop, f"PHASE3_LEGACY_FRONTEND_{code}_MISMATCH"):
                    private_checks(call, [CHANNEL], "test", legacy_frontend_content=content)
