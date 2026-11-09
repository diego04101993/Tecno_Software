"""Normal runtime v3 regressions using only the isolated tenant fixture.

The failed production smoke required ``videowall is None``. A legacy normal
channel has always represented a disabled videowall as ``{"enabled": False}``;
only the explicitly opted-in standalone flow uses ``None``.
"""

from test_tenant_isolation import TenantFixture

from app.models.entities import VideowallNode
from app.services.device_outputs import sync_channel_device_outputs
from app.services.player_runtime import build_player_runtime_config


class NormalRuntimeV3ContractTests(TenantFixture):
    def setUp(self):
        super().setUp()
        self.channel = self.res["A"]["channel"]
        # This regression exercises a plain media playlist, without the tenant
        # fixture's optional layout/dataset integration (Retention V1 omits it).
        self.res["A"]["campaign"].layout_id = None
        sync_channel_device_outputs(self.channel)
        self.db.commit()

    def assert_normal_contract(self, runtime, *, standalone=False):
        self.assertEqual(runtime["runtime_version"], 3)
        self.assertEqual(runtime["channel"]["id"], self.channel.id)
        self.assertEqual(runtime["mode"], "normal")
        self.assertIsInstance(runtime["playlist"], list)
        self.assertIsInstance(runtime["contents"], list)
        self.assertIsInstance(runtime["outputs"], list)
        self.assertIsInstance(runtime["device"], dict)
        if standalone:
            self.assertIsNone(runtime["videowall"])
        else:
            self.assertEqual(set(runtime["videowall"]), {"enabled"})
            self.assertIs(runtime["videowall"]["enabled"], False)

        # A smoke must recognize the two supported disabled representations,
        # without accepting an enabled wall or inventing another contract alias.
        wall = runtime["videowall"]
        self.assertTrue(wall is None or (
            isinstance(wall, dict) and set(wall) == {"enabled"}
            and wall["enabled"] is False
        ))
        self.assertEqual(runtime["device"]["mode"], "single")
        self.assertEqual(runtime["device"]["root_channel_id"], self.channel.id)
        self.assertEqual(runtime["device"]["expected_outputs"], 1)
        self.assertEqual(runtime["device"]["canvas_width"], self.channel.canvas_width)
        self.assertEqual(runtime["device"]["canvas_height"], self.channel.canvas_height)
        self.assertEqual(len(runtime["outputs"]), 1)
        output = runtime["outputs"][0]
        self.assertEqual(output["output_id"], self.channel.device_outputs[0].id)
        self.assertEqual(output["channel_id"], self.channel.id)
        self.assertIs(output["enabled"], True)
        self.assertEqual(output["rotation"], 0)
        self.assertEqual(output["geometry"], dict(
            x=0, y=0, width=self.channel.resolution_width,
            height=self.channel.resolution_height,
        ))
        self.assertEqual(output["source"], output["geometry"])
        retention = runtime["media_retention"]
        self.assertEqual(retention["schema_version"], 1)
        self.assertEqual(retention["channel_id"], self.channel.id)
        self.assertIsInstance(retention["assets"], list)

    def test_coherent_legacy_normal_preserves_disabled_object_and_exposes_bad_smoke_assumption(self):
        runtime = build_player_runtime_config(
            self.channel, self.db, "http://testserver", snapshot_coherent=True,
        )
        self.assert_normal_contract(runtime)
        # All other conjuncts of NORMAL_RUNTIME_V3_CONTRACT_MISMATCH pass.
        self.assertTrue(
            runtime["runtime_version"] == 3
            and runtime["channel"]["id"] == self.channel.id
            and runtime["mode"] == "normal"
            and isinstance(runtime["playlist"], list)
        )
        self.assertFalse(runtime["videowall"] is None)
        self.assertTrue(runtime["media_retention"]["complete"])
        self.assertTrue(runtime["playlist"])
        self.assertIn(self.res["A"]["content"].id, {
            item["id"] for item in runtime["contents"]
        })

    def test_player_http_normal_runtime_uses_the_same_legacy_contract(self):
        response = self.request("GET", "/api/player/runtime-config", who="player_A")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_normal_contract(response.json())

    def test_administrative_http_normal_runtime_uses_the_same_legacy_contract(self):
        response = self.request(
            "GET", f"/api/channels/{self.channel.id}/runtime-config",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_normal_contract(response.json())

    def test_explicit_standalone_opt_in_preserves_null_videowall(self):
        wall = self.res["A"]["wall"]
        wall.playback_groups_enabled = True
        self.db.add(VideowallNode(
            videowall_id=wall.id, channel_id=self.channel.id,
            position_index=1, row_index=0, column_index=0,
            x=0, y=0, width=1920, height=1080,
        ))
        self.db.commit()
        runtime = build_player_runtime_config(
            self.channel, self.db, "http://testserver", snapshot_coherent=True,
        )
        self.assert_normal_contract(runtime, standalone=True)
        self.assertEqual(runtime["playlist"], [])
        self.assertEqual(runtime["campaigns"], [])
