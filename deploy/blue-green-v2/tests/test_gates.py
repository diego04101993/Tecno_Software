from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from gates import (
    BACKEND_ARGV, REVISION_LABEL, TREE_LABEL, GateError, PlayerGate, ReadyGate,
    attribute_heartbeats, device_ref, live_identity, sign_receipt,
)


KEY = bytes(range(32))
CONTAINER = "a" * 64
IMAGE = "sha256:" + "b" * 64
REVISION = "c" * 40
TREE = "d" * 40
NETWORK = "e" * 64
DEVICE = "7d8d9bcd-82f2-40cc-af02-986282366f04"
OTHER = "7d8d9bcd-82f2-40cc-af02-986282366f05"
CHANNEL = "f8d61867-163f-4636-b51c-9363a9a3714d"
TRACE = "49b266e4-82a3-48d1-8099-87cca776ec65"
NOW = lambda: datetime(2026, 11, 12, 18, 10, 0, tzinfo=timezone.utc)


def api_slot():
    image = {"Id": IMAGE, "Config": {"Labels": {REVISION_LABEL: REVISION, TREE_LABEL: TREE}}}
    item = {
        "Id": CONTAINER, "Name": "/bgv2-backend-green", "Image": IMAGE, "RestartCount": 0,
        "Config": {"Labels": {**image["Config"]["Labels"], "io.tecnocontrol.bgv2.slot": "GREEN",
                              "io.tecnocontrol.bgv2.role": "api"},
                   "Env": ["AUTO_SEED=false", "APP_ENV=production", "WEB_CONCURRENCY=1",
                           "FORWARDED_ALLOW_IPS=172.30.250.2", "MEDIA_ROOT=/app/storage/uploads",
                           "SECRET_KEY=synthetic-fixture-value"]},
        "State": {"Running": True, "Status": "running", "OOMKilled": False, "Restarting": False, "Dead": False,
                  "Paused": False, "Pid": 642, "StartedAt": "2026-11-12T18:00:00.000000000Z",
                  "Health": {"Status": "healthy"}},
        "HostConfig": {"PortBindings": {}, "PublishAllPorts": False, "Dns": None, "DnsOptions": None,
                       "DnsSearch": None, "CapAdd": []},
        "NetworkSettings": {"Ports": {"8000/tcp": None}, "Networks": {
            "fixture-net": {"NetworkID": NETWORK, "Aliases": ["backend-green"], "IPAddress": "172.30.250.4"}}},
        "Mounts": [{"Type": "volume", "Name": "fixture-uploads", "Destination": "/app/storage/uploads", "RW": True}],
    }
    network = {"Id": NETWORK, "Name": "fixture-net", "Driver": "bridge", "Internal": True}
    binding = {"name": "bgv2-backend-green", "image_id": IMAGE, "revision": REVISION, "tree": TREE,
               "upstream": "backend-green:8000", "ip": "172.30.250.4", "network": "fixture-net",
               "proxy_ip": "172.30.250.2", "uploads_volume": "fixture-uploads"}
    return item, image, network, [CONTAINER], binding


class IdentityTests(unittest.TestCase):
    def identity(self, *args, argv=BACKEND_ARGV):
        return live_identity(*args, "api", "GREEN", argv, 90122)

    def test_same_process_snapshot_excludes_raw_secrets(self):
        fixture = api_slot()
        first = self.identity(*fixture)
        self.assertEqual(first, self.identity(*fixture))
        self.assertEqual(first["container_id"], CONTAINER)
        self.assertEqual(first["start_ticks"], 90122)
        self.assertNotIn("synthetic-fixture-value", json.dumps(first))

    def test_restart_oom_health_digest_and_command_fail_closed(self):
        for field, value in (("OOMKilled", True), ("Health", {"Status": "unhealthy"}), ("Paused", True)):
            with self.subTest(field=field), self.assertRaises(GateError):
                fixture = api_slot()
                fixture[0]["State"][field] = value
                self.identity(*fixture)
        for field, value in (("RestartCount", 1), ("Image", "sha256:" + "9" * 64)):
            with self.subTest(field=field), self.assertRaises(GateError):
                fixture = api_slot()
                fixture[0][field] = value
                self.identity(*fixture)
        with self.assertRaises(GateError):
            self.identity(*api_slot(), argv=[*BACKEND_ARGV, "--reload"])

    def test_duplicate_dns_alias_mount_and_host_port_rejected(self):
        fixture = api_slot()
        with self.assertRaisesRegex(GateError, "NETWORK_NOT_EXCLUSIVE"):
            self.identity(*fixture[:3], [CONTAINER, "f" * 64], fixture[-1])
        fixture[0]["Mounts"][0]["RW"] = False
        with self.assertRaisesRegex(GateError, "WRONG_UPLOADS"):
            self.identity(*fixture)
        fixture = api_slot()
        fixture[0]["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostPort": "8000"}]}
        with self.assertRaisesRegex(GateError, "PUBLISHED_PORT"):
            self.identity(*fixture)

    def test_dns_exception_does_not_change_raw_snapshot_or_relax_other_fields(self):
        fixture = api_slot()
        original = deepcopy(fixture)
        start = self.identity(*fixture)
        self.assertEqual(fixture, original)
        for field in ("Dns", "DnsOptions", "DnsSearch"):
            fixture[0]["HostConfig"][field] = []
        self.assertEqual(start, self.identity(*fixture))
        fixture[0]["HostConfig"]["Dns"] = ["1.1.1.1", "8.8.8.8"]
        ordered = self.identity(*fixture)
        fixture[0]["HostConfig"]["Dns"].reverse()
        self.assertNotEqual(ordered, self.identity(*fixture))
        fixture[0]["HostConfig"].pop("Dns")
        self.assertNotEqual(start, self.identity(*fixture))
        fixture[0]["HostConfig"]["Dns"] = ""
        with self.assertRaises(GateError):
            self.identity(*fixture)
        fixture[0]["HostConfig"]["Dns"] = []
        fixture[0]["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
        self.assertNotEqual(start, self.identity(*fixture))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.context = {"slot": "GREEN", "role": "api", "identity_hash": "f" * 64,
                        "started_at": "2026-11-12T18:00:00+00:00",
                        "release": {"revision": REVISION, "tree": TREE, "images": {"api": IMAGE, "frontend": IMAGE}}}

    def ready(self):
        return {**self.context, "started_at": "2026-11-12T18:00:00+00:00", "healthy_at": "2026-11-12T18:00:05+00:00",
                "ready_at": "2026-11-12T18:00:40+00:00", "private_route": True,
                "checks": {"root_http": 200, "time_http": 200, "auth_me_http": 200, "channels_http": 200,
                           "channels_elapsed_ns": 3_000_000_000, "runtime_serial_ok": 36, "runtime_concurrent_ok": 16,
                           "runtime_clients": 4, "runtime_transport_timeout_s": 10, "pool": "BALANCED",
                           "dual_slot_schema": True, "media_cross_slot": True, "dual_slot_capacity": True,
                           "contracts": {"windows_legacy": True, "android_normal": True, "android_node_slice": True}}}

    def test_readiness_signed_exact_and_performance_boundary(self):
        gate = ReadyGate(KEY, NOW)
        payload = self.ready()
        self.assertTrue(gate(sign_receipt("READY", payload, KEY), self.context))
        payload["checks"]["channels_elapsed_ns"] += 1
        self.assertFalse(gate(sign_receipt("READY", payload, KEY), self.context))
        payload = self.ready()
        payload["checks"]["dual_slot_capacity"] = 1
        self.assertFalse(gate(sign_receipt("READY", payload, KEY), self.context))

    def test_readiness_not_reusable_across_processes_or_with_forged_signature(self):
        receipt = sign_receipt("READY", self.ready(), KEY)
        gate = ReadyGate(KEY, NOW)
        self.assertFalse(gate(receipt, {**self.context, "identity_hash": "a" * 64}))
        self.assertFalse(gate({**receipt, "hmac_sha256": "0" * 64}, self.context))

    def test_frontend_requires_single_get_and_listener_readiness(self):
        self.context["role"] = "frontend"
        payload = self.ready()
        payload["checks"] = {"startup_listener_ready": True, "root_http": 200, "root_get_count": 1, "assets_ok": True}
        gate = ReadyGate(KEY, NOW)
        self.assertTrue(gate(sign_receipt("READY", payload, KEY), self.context))
        payload["checks"]["root_get_count"] = 2
        self.assertFalse(gate(sign_receipt("READY", payload, KEY), self.context))

    def player(self):
        del self.context["role"]
        del self.context["started_at"]
        baseline = {"baseline_at": "2026-11-12T18:00:59+00:00", "baseline_refs": [device_ref(DEVICE, KEY)]}
        self.context.update(prior_slot="BLUE", generation=1, switched_at="2026-11-12T18:01:00+00:00",
                            baseline_hash=PlayerGate.baseline_hash(baseline))
        return {**self.context, **baseline, "started_at": self.context["switched_at"],
                "finished_at": "2026-11-12T18:03:00+00:00",
                "heartbeats": [{"device_ref": device_ref(DEVICE, KEY), "trace_ref": "a" * 64,
                                "received_at": "2026-11-12T18:01:02+00:00"}],
                "source": "EDGE_ACK_EXACT_DB_JOIN", "access_complete": True, "http_409": 0, "http_5xx": 0,
                "incompatible": 0, "playback_201": 0, "playback_evidence": "UNVERIFIED"}

    def test_player_exact_baseline_and_no_playback(self):
        payload = self.player()
        gate = PlayerGate(KEY, NOW)
        self.assertTrue(gate(sign_receipt("PLAYER", payload, KEY), self.context))
        payload["heartbeats"][0]["device_ref"] = device_ref(OTHER, KEY)
        self.assertFalse(gate(sign_receipt("PLAYER", payload, KEY), self.context))
        payload["baseline_refs"] = []
        self.assertFalse(gate(sign_receipt("PLAYER", payload, KEY), self.context))
        self.context["baseline_hash"] = payload["baseline_hash"] = PlayerGate.baseline_hash(payload)
        self.assertTrue(gate(sign_receipt("PLAYER", payload, KEY), self.context))

    def test_baseline_must_be_signed_fresh_and_bound_to_predecessor(self):
        context = {"slot": "GREEN", "prior_slot": "BLUE", "previous_generation": 2,
                   "identity_hash": self.context["identity_hash"], "release": self.context["release"]}
        baseline = {"baseline_at": "2026-11-12T18:09:59+00:00", "baseline_refs": [device_ref(DEVICE, KEY)]}
        gate = PlayerGate(KEY, NOW)
        receipt = sign_receipt("BASELINE", {**context, **baseline}, KEY)
        self.assertEqual(gate.capture_baseline(receipt, context), PlayerGate.baseline_hash(baseline))
        self.assertIsNone(gate.capture_baseline(receipt, {**context, "previous_generation": 3}))
        self.assertIsNone(gate.capture_baseline({**receipt, "hmac_sha256": "0" * 64}, context))
        baseline["baseline_at"] = "2026-11-12T18:07:59+00:00"
        self.assertIsNone(gate.capture_baseline(sign_receipt("BASELINE", {**context, **baseline}, KEY), context))

    def test_player_gate_rejects_older_generation_incomplete_and_bad_status(self):
        baseline = self.player()
        gate = PlayerGate(KEY, NOW)
        for field, value in (("finished_at", "2026-11-12T18:02:59+00:00"), ("http_409", 1), ("http_5xx", 1),
                             ("generation", 0), ("access_complete", False), ("source", "DB_ONLY"),
                             ("playback_evidence", "PASS")):
            with self.subTest(field=field):
                payload = {**baseline, field: value}
                self.assertFalse(gate(sign_receipt("PLAYER", payload, KEY), self.context))

    def test_real_ack_join_shape_hmac_no_raw_data_and_fail_closed(self):
        timestamp = "2026-11-12T18:01:02+00:00"
        ack = {"trace": TRACE, "public": True, "slot": "GREEN", "upstream": "backend-green:8000",
               "path_class": "heartbeat", "generation": 1, "status": 200,
               "channel_id": CHANNEL, "received_at": timestamp}
        row = {"player_device_id": DEVICE, "channel_id": CHANNEL, "received_at": timestamp}
        expected = attribute_heartbeats([ack], [row], KEY, 1, "GREEN", "backend-green:8000")
        self.assertEqual(expected[0]["device_ref"], device_ref(DEVICE, KEY))
        self.assertTrue(all(raw not in json.dumps(expected) for raw in (DEVICE, CHANNEL, TRACE)))
        for acks, rows in (([ack], []), ([ack], [row, {**row, "player_device_id": OTHER}]),
                           ([{**ack, "slot": "BLUE"}], [row]), ([{**ack, "public": False}], [row]),
                           ([ack, ack], [row]), ([{**ack, "generation": 0}], [row])):
            with self.subTest(acks=acks, rows=rows), self.assertRaises(GateError):
                attribute_heartbeats(acks, rows, KEY, 1, "GREEN", "backend-green:8000")


if __name__ == "__main__":
    unittest.main()
