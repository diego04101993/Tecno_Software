"""Local in-process API coverage; run via run_screen_rotation_suite.py."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import test_screen_rotation as rotation_fixture
from app.models.entities import (
    Campaign, CampaignChannelAssignment, CampaignSequenceItem, CampaignSequenceItemType,
    ContentItem, ContentType, PlayerDevice, UserRole,
)
from app.services.output_mapping import normalize_output_mapping
from app.services.player_runtime import build_player_runtime_config


EXPECTED = json.loads((Path(__file__).parent / "fixtures/led_custom_516x860.json").read_text(encoding="utf-8"))


class LedCustomCanvasTests(TestCase):
    # Reuse only the guarded in-memory fixture, not its test methods.
    setUp = rotation_fixture.ScreenRotationTests.setUp
    tearDown = rotation_fixture.ScreenRotationTests.tearDown
    create = rotation_fixture.ScreenRotationTests.create
    update = rotation_fixture.ScreenRotationTests.update
    persisted = rotation_fixture.ScreenRotationTests.persisted
    output_state = rotation_fixture.ScreenRotationTests.output_state

    def test_create_persists_exact_mapping_without_repurposing_channel_or_outputs(self):
        created = self.create(resolution_width=1920, resolution_height=1080, output_mapping_json=EXPECTED)
        self.assertEqual(created["output_mapping_json"], EXPECTED)
        channel = self.persisted(created["id"])
        self.assertEqual(channel.output_mapping_json, EXPECTED)
        self.assertEqual((channel.resolution_width, channel.resolution_height), (1920, 1080))
        self.assertEqual((channel.canvas_width, channel.canvas_height), (1920, 1080))
        output = channel.device_outputs[0]
        self.assertEqual((output.width, output.height, output.source_width, output.source_height), (1920, 1080, 1920, 1080))
        self.assertEqual((output.x, output.y, output.source_x, output.source_y, output.rotation), (0, 0, 0, 0, 0))

    def test_authenticated_runtime_keeps_exact_mapping_and_existing_versions(self):
        created = self.create(output_mapping_json=EXPECTED)
        token = "led-local-test-token"
        self.db.add(PlayerDevice(channel_id=created["id"], client_id=self.tenant.id, branch_id=self.branch.id,
            hardware_id="led-fixture", device_name="LED fixture", player_token_hash=hashlib.sha256(token.encode()).hexdigest()))
        self.db.commit()
        response = self.http.get("/api/player/runtime-config", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()
        self.assertEqual(runtime["output_mapping"], EXPECTED)
        self.assertEqual(runtime["channel"]["output_mapping_json"], EXPECTED)
        self.assertEqual(runtime["runtime_version"], 3)
        self.assertEqual(runtime["media_retention"]["schema_version"], 1)
        self.assertEqual(runtime["outputs"][0]["geometry"]["width"], 1920)

    def test_name_notes_and_rotation_do_not_change_mapping(self):
        created = self.create(output_mapping_json=EXPECTED)
        before = self.output_state(created["id"])
        self.update(created["id"], name="LED renamed", notes="Edited notes")
        self.assertEqual(self.output_state(created["id"]), before)
        self.assertEqual(self.persisted(created["id"]).output_mapping_json, EXPECTED)
        self.update(created["id"], rotation=270)
        before[0]["rotation"] = 270
        self.assertEqual(self.output_state(created["id"]), before)
        self.assertEqual(self.persisted(created["id"]).output_mapping_json, EXPECTED)

    def test_mapping_edit_does_not_regenerate_historical_output_geometry(self):
        created = self.create(rotation=90)
        channel = self.persisted(created["id"])
        channel.canvas_width, channel.canvas_height = 700, 1100
        output = channel.device_outputs[0]
        output.source_x, output.x, output.width, output.display_uid = 19, 31, 1500, "persist-me"
        self.db.commit()
        before = self.output_state(channel.id)
        self.update(channel.id, output_mapping_json=EXPECTED)
        self.assertEqual(self.output_state(channel.id), before)
        channel = self.persisted(channel.id)
        self.assertEqual((channel.canvas_width, channel.canvas_height), (700, 1100))

    def test_full_replacement_keeps_resized_and_repositioned_slice(self):
        created = self.create(output_mapping_json=EXPECTED)
        replacement = deepcopy(EXPECTED)
        for key in ("physical_width", "source_canvas_width", "canvas_width", "viewport_width"):
            replacement[key] = 600
        for key in ("physical_height", "source_canvas_height", "canvas_height", "viewport_height"):
            replacement[key] = 900
        replacement["viewport_x"], replacement["viewport_y"] = 20, 30
        replacement["slices"][0].update(source_width=600, source_height=900, output_width=600, output_height=900, output_x=20, output_y=30)
        before = self.output_state(created["id"])
        updated = self.update(created["id"], output_mapping_json=replacement)
        self.assertEqual(updated["output_mapping_json"], replacement)
        self.assertEqual(self.persisted(created["id"]).output_mapping_json, replacement)
        self.assertEqual(self.output_state(created["id"]), before)

    def test_normal_explicitly_clears_custom_mode_and_slice(self):
        created = self.create(output_mapping_json=EXPECTED)
        normal = normalize_output_mapping({}, resolution_width=1920, resolution_height=1080)
        updated = self.update(created["id"], output_mapping_json=normal)["output_mapping_json"]
        self.assertFalse(updated["enabled"])
        self.assertEqual([updated[key] for key in ("profile", "mode", "mapping_mode")], ["normal"] * 3)
        self.assertEqual(updated["slices"], [])
        for key in ("output_width", "physical_width", "source_canvas_width", "canvas_width", "viewport_width"):
            self.assertEqual(updated[key], 1920)
        for key in ("output_height", "physical_height", "source_canvas_height", "canvas_height", "viewport_height"):
            self.assertEqual(updated[key], 1080)
        self.assertEqual((updated["viewport_x"], updated["viewport_y"], updated["scale_x"], updated["scale_y"]), (0, 0, 1, 1))

    def test_omitted_mapping_preserves_raw_legacy_even_when_resolution_changes(self):
        for mapping in ({}, {"mode": "custom", "viewport_x": -25}, {"mode": "sliced", "profile": "led_wide", "slice_count": 3}):
            with self.subTest(mapping=mapping):
                created = self.create()
                channel = self.persisted(created["id"])
                channel.output_mapping_json = deepcopy(mapping)
                self.db.commit()
                self.update(channel.id, name=f"Legacy renamed {self.next_name}", notes="Unrelated", rotation=180)
                self.assertEqual(self.persisted(channel.id).output_mapping_json, mapping)
                self.update(channel.id, resolution_width=1280, resolution_height=720)
                self.assertEqual(self.persisted(channel.id).output_mapping_json, mapping)

    def test_advanced_out_of_bounds_remains_accepted_and_untouched(self):
        advanced = deepcopy(EXPECTED)
        advanced["slices"][0].update(source_x=45, output_x=-100, output_width=2400, scale_x=2.5)
        created = self.create(output_mapping_json=advanced)
        stored = deepcopy(created["output_mapping_json"])
        self.assertEqual(stored["slices"][0]["output_x"], -100)
        self.assertEqual(stored["scale_x"], 2.5)
        self.update(created["id"], name="Unchanged advanced")
        self.assertEqual(self.persisted(created["id"]).output_mapping_json, stored)

    def test_led_writes_obey_client_and_branch_isolation(self):
        own = self.create(output_mapping_json=EXPECTED)
        other = self.create(branch_id=self.other_branch.id)
        for role, client_id, branch_id, target_id, target_branch in (
            (UserRole.CLIENT_ADMIN, self.other_tenant.id, None, own["id"], self.branch.id),
            (UserRole.BRANCH_MANAGER, self.tenant.id, self.branch.id, other["id"], self.other_branch.id),
            (UserRole.OPERATOR, self.tenant.id, self.branch.id, own["id"], self.branch.id),
        ):
            with self.subTest(role=role):
                self.user = SimpleNamespace(role=role, client_id=client_id, branch_id=branch_id)
                response = self.http.patch(f"/api/channels/{target_id}", json={"output_mapping_json": EXPECTED})
                self.assertEqual(response.status_code, 403, response.text)
                response = self.http.post("/api/channels/", json={"branch_id": target_branch, "name": "Forbidden", "output_mapping_json": EXPECTED})
                self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.persisted(own["id"]).output_mapping_json, EXPECTED)
        self.assertEqual(self.persisted(other["id"]).output_mapping_json, other["output_mapping_json"])

    def test_led_mapping_does_not_change_campaign_playlist_or_retention(self):
        created = self.create()
        campaign = Campaign(client_id=self.tenant.id, name="LED campaign")
        self.db.add(campaign)
        self.db.flush()
        for index, content_type in enumerate((ContentType.IMAGE, ContentType.VIDEO), 1):
            content = ContentItem(client_id=self.tenant.id, name=f"Mixed {index}", type=content_type,
                file_path=f"/media/{self.tenant.id}/led-{index}.{'jpg' if index == 1 else 'mp4'}")
            self.db.add(content)
            self.db.flush()
            self.db.add(CampaignSequenceItem(campaign_id=campaign.id, item_type=CampaignSequenceItemType.CONTENT,
                content_id=content.id, sort_order=index, duration_seconds=15))
        self.db.add(CampaignChannelAssignment(channel_id=created["id"], campaign_id=campaign.id))
        self.db.commit()
        with patch("app.services.player_runtime.now_utc", return_value=rotation_fixture.NOW):
            before = build_player_runtime_config(self.persisted(created["id"]), self.db, "http://testserver/", snapshot_coherent=True)
            self.update(created["id"], output_mapping_json=EXPECTED)
            after = build_player_runtime_config(self.persisted(created["id"]), self.db, "http://testserver/", snapshot_coherent=True)
        self.assertEqual([item["content"]["type"] for item in after["playlist"]], ["image", "video"])
        self.assertEqual(after["campaigns"][0]["campaign_id"], campaign.id)
        self.assertTrue(after["media_retention"]["complete"])
        for key in ("campaigns", "playlist", "media_retention", "outputs", "device"):
            self.assertEqual(after[key], before[key], key)
