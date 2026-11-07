"""Shared publication runtime; DB/file fixtures are synthetic and isolated."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from unittest import mock

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from test_videowall_publications import VideowallPublicationFixture
from app.models.entities import CampaignChannelAssignment, ContentItem, ContentType, VideowallPublicationAsset, VideowallNode
from app.services import player_runtime, videowall_publications as authority
from app.services.device_outputs import sync_channel_device_outputs
from app.services.tenant_queries import readable_query
from app.services.videowall_runtime import timeline_position


class VideowallRuntimeTests(VideowallPublicationFixture):
    def setUp(self):
        super().setUp()
        for channel in self.channels:
            sync_channel_device_outputs(channel)
        self.db.commit()

    def runtime(self, index=0, coherent=True):
        return player_runtime.build_player_runtime_config(
            self.channels[index], self.db, "http://qa.invalid", snapshot_coherent=coherent)

    def active(self):
        publication = self.ready()
        authority.activate_publication(self.db, self.wall, publication.id, self.actor)
        return publication

    def assert_no_master(self, runtime):
        raw = json.dumps(runtime)
        self.assertNotIn(self.master.file_path, raw)
        self.assertNotIn(self.master.id, raw)
        self.assertNotIn("node.assets", raw)
        self.assertNotIn("asset_mode", raw)

    def test_same_persisted_publication_epoch_ids_and_local_slices_across_three_requests(self):
        publication = self.active()
        runtimes = [self.runtime(index) for index in range(3)]
        derived = []
        for position, runtime in enumerate(runtimes, 1):
            with self.subTest(position=position):
                wall = runtime["videowall"]
                self.assertIsNone(runtime["fallback"])
                self.assertEqual(runtime["runtime_version"], 3)
                self.assertEqual(wall["publication_id"], publication.id)
                self.assertEqual(wall["media_delivery"], "node_slice")
                self.assertEqual(wall["sync"]["mode"], "epoch_timeline_v1")
                self.assertEqual(wall["sync"]["loop_start_epoch_ms"], publication.loop_start_epoch_ms)
                self.assertEqual(wall["node"], dict(position_index=position, row_index=0,
                    column_index=position - 1, crop_x=(position-1)*1920, crop_y=0,
                    crop_width=1920, crop_height=1080))
                item = runtime["playlist"][0]
                self.assertEqual((item["id"], item["campaign_id"], item["sort_order"], item["duration_ms"]),
                                 (self.item.id, self.campaign.id, 1, 2000))
                self.assertEqual(item["duration_seconds"], 2)
                self.assertEqual(item["content_id"], item["content"]["id"])
                self.assertEqual((item["content"]["width"], item["content"]["height"]), (1920, 1080))
                self.assertEqual(runtime["device"]["expected_outputs"], 1)
                self.assertEqual(len(runtime["outputs"]), 1)
                self.assertEqual(runtime["outputs"][0]["source"], dict(x=0, y=0, width=1920, height=1080))
                self.assert_no_master(runtime)
                derived.append(item["content_id"])
        self.assertEqual(len(set(derived)), 3)

    def test_clock_is_fresh_but_request_and_new_session_preserve_epoch(self):
        publication = self.active()
        instant = datetime(2026, 11, 4, tzinfo=UTC)
        with mock.patch.object(player_runtime, "now_utc", return_value=instant):
            first = self.runtime()
        with mock.patch.object(player_runtime, "now_utc", return_value=instant + timedelta(seconds=17)):
            with Session(self.engine, autoflush=False) as request_db:
                from app.models.entities import Channel
                reboot = player_runtime.build_player_runtime_config(
                    request_db.get(Channel, self.channels[0].id), request_db, "http://qa.invalid")
        self.assertNotEqual(first["server_time"], reboot["server_time"])
        self.assertEqual(first["videowall"]["sync"], reboot["videowall"]["sync"])
        self.assertEqual(reboot["videowall"]["sync"]["loop_start_epoch_ms"], publication.loop_start_epoch_ms)

    def test_no_active_publication_returns_empty_never_master_assignment(self):
        self.db.add(CampaignChannelAssignment(channel_id=self.channels[0].id, campaign_id=self.campaign.id))
        self.db.commit()
        runtime = self.runtime()
        self.assertEqual(runtime["playlist"], [])
        self.assertEqual(runtime["sequence_items"], [])
        self.assertEqual(runtime["contents"], [])
        self.assertEqual(runtime["videowall"]["media_delivery"], "node_slice")
        self.assertIsNotNone(runtime["fallback"])
        self.assert_no_master(runtime)

    def test_raw_node_slice_marker_on_invalid_primary_channel_never_falls_to_legacy(self):
        self.db.execute(delete(VideowallNode).where(VideowallNode.videowall_id == self.wall.id))
        self.wall.primary_channel_id = self.channels[0].id
        self.wall.render_mode = "hardware-single-input"
        self.db.add(CampaignChannelAssignment(channel_id=self.channels[0].id, campaign_id=self.campaign.id))
        self.db.commit()
        runtime = self.runtime()
        self.assertEqual(runtime["playlist"], [])
        self.assertEqual(runtime["videowall"]["media_delivery"], "node_slice")
        self.assert_no_master(runtime)

    def test_frozen_program_ignores_later_live_campaign_edits(self):
        publication = self.active()
        self.item.duration_seconds = 37
        self.item.is_enabled = False
        self.campaign.playback_mode = "shuffle"
        self.db.commit()
        runtime = self.runtime()
        self.assertEqual(runtime["playlist"][0]["duration_ms"], 2000)
        self.assertEqual(runtime["videowall"]["loop_duration_ms"], 2000)
        self.assertEqual(runtime["playback_mode"], "sequential")
        self.assertEqual(runtime["videowall"]["publication_id"], publication.id)

    def test_creator_deactivation_does_not_revoke_already_activated_wall(self):
        publication = self.active()
        self.actor.is_active = False
        self.db.commit()
        self.assertEqual(self.runtime()["videowall"]["publication_id"], publication.id)

    def test_foreign_derived_reference_rejects_entire_three_node_program_and_admin_list(self):
        publication = self.active()
        foreign = ContentItem(client_id=self.foreign.id, name="B", type=ContentType.IMAGE,
                              file_path=f"/media/{self.foreign.id}/foreign.png")
        self.db.add(foreign); self.db.commit()
        asset = self.db.scalar(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == publication.id,
            VideowallPublicationAsset.position_index == 3))
        self.db.execute(update(VideowallPublicationAsset).where(
            VideowallPublicationAsset.id == asset.id).values(derived_content_id=foreign.id))
        self.db.commit(); self.db.expire_all()
        for index in range(3):
            runtime = self.runtime(index)
            self.assertEqual(runtime["playlist"], [])
            self.assertEqual(runtime["media_retention"]["assets"], [])
            self.assertNotIn(foreign.id, json.dumps(runtime))
            self.assert_no_master(runtime)
        response = self.http.get(f"/api/videowalls/{self.wall.id}/publications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_missing_asset_rejects_read_before_pagination(self):
        publication = self.active()
        asset = self.db.scalar(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == publication.id))
        self.db.execute(delete(VideowallPublicationAsset).where(VideowallPublicationAsset.id == asset.id))
        self.db.commit(); self.db.expire_all()
        self.assertEqual(self.runtime()["playlist"], [])
        from app.models.entities import VideowallPublication
        eligible = readable_query(select(VideowallPublication), VideowallPublication, db=self.db).limit(1)
        self.assertEqual(list(self.db.scalars(eligible)), [])

    def test_missing_derived_file_never_plays_master(self):
        publication = self.active()
        derived_id = self.db.scalar(select(VideowallPublicationAsset.derived_content_id).where(
            VideowallPublicationAsset.publication_id == publication.id,
            VideowallPublicationAsset.position_index == 3))
        content = self.db.get(ContentItem, derived_id)
        path = self.root / content.file_path.removeprefix("/media/")
        path.unlink()
        runtime = self.runtime()
        self.assertEqual(runtime["playlist"], [])
        self.assert_no_master(runtime)

    def test_retention_v1_protects_current_and_ready_node_slices_never_masters(self):
        active = self.active()
        pending = self.ready()
        runtime = self.runtime()
        retention = runtime["media_retention"]
        self.assertTrue(retention["complete"])
        self.assertEqual(retention["schema_version"], 1)
        self.assertEqual(len(retention["assets"]), 2)
        self.assertEqual(retention["protected_campaign_ids"], [self.campaign.id])
        expected = set(self.db.scalars(select(VideowallPublicationAsset.derived_content_id).where(
            VideowallPublicationAsset.publication_id.in_((active.id, pending.id)),
            VideowallPublicationAsset.node_id == self.nodes[0].id)))
        self.assertEqual({a["content_id"] for a in retention["assets"]}, expected)
        self.assert_no_master(runtime)
        self.assertFalse(self.runtime(coherent=False)["media_retention"]["complete"])

    def test_legacy_path_unchanged_for_normal_screens_and_existing_walls(self):
        self.wall.media_delivery = "legacy"
        self.db.add(CampaignChannelAssignment(channel_id=self.channels[0].id, campaign_id=self.campaign.id))
        self.db.commit()
        instant = datetime(2026, 11, 4, tzinfo=UTC)
        with mock.patch.object(player_runtime, "now_utc", return_value=instant):
            actual = self.runtime()
            with mock.patch("app.services.videowall_runtime.build_node_slice_runtime", return_value=None):
                old_flow = self.runtime()
        self.assertEqual(actual, old_flow)
        self.assertNotIn("media_delivery", actual["videowall"])
        self.assertEqual(actual["playlist"][0]["content_id"], self.master.id)

    def test_shared_late_join_fixture(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/videowall_epoch_timeline_v1.json").read_text())
        self.assertEqual(sum(fixture["duration_ms"]), fixture["loop_duration_ms"])
        for case in fixture["cases"]:
            with self.subTest(case=case):
                expected = {key: case[key] for key in ("item_index", "item_offset_ms")}
                self.assertEqual(timeline_position(case["server_now_ms"], fixture["loop_start_epoch_ms"],
                                                  fixture["duration_ms"]), expected)

    def test_database_snapshot_wrapper_preserves_active_authority(self):
        publication = self.active()
        runtime = player_runtime.build_player_runtime_from_snapshot(self.channels[0], self.db, "http://qa.invalid")
        self.assertEqual(runtime["videowall"]["publication_id"], publication.id)
        self.assert_no_master(runtime)
