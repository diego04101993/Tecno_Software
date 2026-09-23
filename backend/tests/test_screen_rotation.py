from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
import hashlib
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Use tests/run_screen_rotation_suite.py: only isolated SQLite memory is allowed")

from app.api.deps import get_current_user, get_db
from app.api.routes import channels, player
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignChannelAssignment, CampaignSequenceItem,
    CampaignSequenceItemType, Channel, ChannelMode, Client, ContentItem,
    ContentType, DeviceMode, DeviceOutput, Orientation, PlayerDevice, Schedule, UserRole,
)
from app.services.device_outputs import build_device_output_definitions, sync_channel_device_outputs
from app.services.player_runtime import build_player_runtime_config


NOW = datetime(2026, 9, 23, 18, tzinfo=UTC)
ANGLES = (0, 90, 180, 270)


class ScreenRotationTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        # Verify the target before creating any fixture tables.
        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        self.assertIn(self.engine.url.database, (None, "", ":memory:"))
        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.tenant = Client(name="Rotation A", slug="rotation-a")
        self.other_tenant = Client(name="Rotation B", slug="rotation-b")
        self.db.add_all([self.tenant, self.other_tenant])
        self.db.flush()
        self.branch = Branch(client_id=self.tenant.id, name="A1", code="A1")
        self.other_branch = Branch(client_id=self.tenant.id, name="A2", code="A2")
        self.foreign_branch = Branch(client_id=self.other_tenant.id, name="B1", code="B1")
        self.db.add_all([self.branch, self.other_branch, self.foreign_branch])
        self.db.commit()
        self.user = SimpleNamespace(role=UserRole.CLIENT_ADMIN, client_id=self.tenant.id, branch_id=None)
        self.next_name = 0
        app = FastAPI()  # Deliberately no app.main, lifespan, seed or init_db.
        app.include_router(channels.router, prefix="/api/channels")
        app.include_router(player.router, prefix="/api/player")

        def isolated_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.http = TestClient(app)

    def tearDown(self) -> None:
        self.http.close()
        self.db.close()
        self.engine.dispose()

    def create(self, **fields) -> dict:
        self.next_name += 1
        body = {"branch_id": self.branch.id, "name": f"Screen {self.next_name}", **fields}
        response = self.http.post("/api/channels/", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update(self, channel_id: str, **fields) -> dict:
        response = self.http.patch(f"/api/channels/{channel_id}", json=fields)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def persisted(self, channel_id: str) -> Channel:
        self.db.expire_all()
        channel = self.db.get(Channel, channel_id)
        self.assertIsNotNone(channel)
        return channel

    def output_state(self, channel_id: str) -> list[dict]:
        channel = self.persisted(channel_id)
        return [
            {column.name: getattr(output, column.name) for column in DeviceOutput.__table__.columns
             if column.name not in {"created_at", "updated_at"}}
            for output in channel.device_outputs
        ]

    def test_create_accepts_all_four_angles_and_persists_them(self):
        for angle in ANGLES:
            with self.subTest(angle=angle):
                created = self.create(rotation=angle)
                self.assertEqual(created["device_outputs"][0]["rotation"], angle)
                self.assertEqual(self.persisted(created["id"]).device_outputs[0].rotation, angle)

    def test_edit_accepts_all_four_angles_including_explicit_zero(self):
        created = self.create(rotation=270)
        output_id = created["device_outputs"][0]["id"]
        for angle in ANGLES:
            with self.subTest(angle=angle):
                updated = self.update(created["id"], rotation=angle)
                self.assertEqual(updated["device_outputs"][0]["rotation"], angle)
                self.assertEqual(updated["device_outputs"][0]["id"], output_id)
                self.assertEqual(self.persisted(created["id"]).device_outputs[0].rotation, angle)

    def test_invalid_values_null_booleans_strings_and_floats_are_rejected(self):
        created = self.create(rotation=180)
        invalid_values = (None, True, False, -90, 45, 360, "90", "", 90.0, 0.0, [], {})
        for invalid in invalid_values:
            with self.subTest(value=invalid):
                response = self.http.post("/api/channels/", json={"branch_id": self.branch.id, "name": "Invalid", "rotation": invalid})
                self.assertEqual(response.status_code, 422, response.text)
                response = self.http.patch(f"/api/channels/{created['id']}", json={"name": "Must not save", "rotation": invalid})
                self.assertEqual(response.status_code, 422, response.text)
                persisted = self.persisted(created["id"])
                self.assertEqual(persisted.name, created["name"])
                self.assertEqual(persisted.device_outputs[0].rotation, 180)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Channel)), 1)

    def test_create_without_rotation_preserves_existing_defaults(self):
        created = self.create()
        self.assertEqual(created["device_outputs"][0]["rotation"], 0)
        self.assertEqual(created["orientation"], "horizontal")
        self.assertEqual((created["resolution_width"], created["resolution_height"]), (1920, 1080))

    def test_omitted_rotation_and_name_notes_edits_preserve_all_output_settings(self):
        created = self.create(rotation=270)
        channel = self.persisted(created["id"])
        output = channel.device_outputs[0]
        output.name, output.display_uid, output.enabled = "Custom output", "screen-uid", False
        output.x, output.source_x, output.sync_group = 17, 23, "custom-sync"
        channel.canvas_width, channel.canvas_height = 1500, 900
        self.db.commit()
        before = self.output_state(channel.id)
        self.update(channel.id, name="Renamed", notes="Notes edited")
        self.assertEqual(self.output_state(channel.id), before)
        channel = self.persisted(channel.id)
        self.assertEqual(channel.name, "Renamed")
        self.assertEqual(channel.notes, "Notes edited")
        self.assertEqual((channel.canvas_width, channel.canvas_height), (1500, 900))
        self.update(channel.id)
        self.assertEqual(self.output_state(channel.id), before)

    def test_unchanged_device_fields_do_not_regenerate_outputs(self):
        created = self.create(rotation=90)
        channel = self.persisted(created["id"])
        channel.device_outputs[0].source_x = 42
        self.db.commit()
        before = self.output_state(channel.id)
        self.update(channel.id, name="Full form", mode="normal", device_mode="single", orientation="horizontal",
                    screen_count=1, resolution_width=1920, resolution_height=1080, canvas_width=1920, canvas_height=1080)
        self.assertEqual(self.output_state(channel.id), before)

    def test_historical_vertical_zero_is_not_reinterpreted(self):
        created = self.create(orientation="vertical", resolution_width=1080, resolution_height=1920)
        self.update(created["id"], name="Historical vertical", notes="Still zero")
        channel = self.persisted(created["id"])
        self.assertEqual(channel.orientation, Orientation.VERTICAL)
        self.assertEqual(channel.device_outputs[0].rotation, 0)
        self.assertEqual((channel.resolution_width, channel.resolution_height), (1080, 1920))

    def test_rotation_only_preserves_historical_dimensions_orientation_and_mapping(self):
        created = self.create(orientation="vertical", resolution_width=1080, resolution_height=1920)
        channel = self.persisted(created["id"])
        channel.canvas_width, channel.canvas_height = 700, 1100
        channel.output_mapping_json = {"enabled": True, "mode": "custom", "viewport_x": 19}
        self.db.commit()
        before = self.output_state(channel.id)
        self.update(channel.id, rotation=180)
        after = self.output_state(channel.id)
        before[0]["rotation"] = 180
        self.assertEqual(after, before)
        channel = self.persisted(channel.id)
        self.assertEqual((channel.canvas_width, channel.canvas_height), (700, 1100))
        self.assertEqual((channel.resolution_width, channel.resolution_height), (1080, 1920))
        self.assertEqual(channel.orientation, Orientation.VERTICAL)
        self.assertEqual(channel.output_mapping_json, {"enabled": True, "mode": "custom", "viewport_x": 19})

    def test_multi_output_rotations_follow_display_order_and_new_output_defaults_zero(self):
        for mode in ("mirror", "extended", "independent"):
            with self.subTest(mode=mode):
                created = self.create(mode="expanded", device_mode=mode, screen_count=3)
                channel = self.persisted(created["id"])
                for index, output in enumerate(channel.device_outputs):
                    output.rotation = ANGLES[index + 1]
                    output.display_uid = f"{mode}-{index}"
                self.db.commit()
                identities = {output.display_order: (output.id, output.rotation, output.display_uid) for output in channel.device_outputs}
                channel.device_outputs.reverse()
                definitions = build_device_output_definitions(channel)
                self.assertEqual([item["rotation"] for item in definitions], [90, 180, 270])
                sync_channel_device_outputs(channel)
                self.db.commit()
                self.update(channel.id, screen_count=4)
                outputs = self.persisted(channel.id).device_outputs
                for output in outputs[:3]:
                    self.assertEqual((output.id, output.rotation, output.display_uid), identities[output.display_order])
                self.assertEqual(outputs[3].rotation, 0)

    def test_metadata_edits_preserve_out_of_scope_modes_and_all_outputs(self):
        modes = [("expanded", "mirror"), ("expanded", "extended"), ("expanded", "independent"),
                 ("videowall", "single"), ("audio", "single"), ("touch", "single")]
        for mode, device_mode in modes:
            with self.subTest(mode=mode, device_mode=device_mode):
                created = self.create(mode=mode, device_mode=device_mode, screen_count=2)
                channel = self.persisted(created["id"])
                for index, output in enumerate(channel.device_outputs):
                    output.rotation = 90 if index == 0 else 270
                    output.name, output.display_uid, output.enabled = f"Custom {index}", f"uid-{index}", False
                    output.source_y, output.sync_group = 11, "historical"
                self.db.commit()
                before = self.output_state(channel.id)
                self.update(channel.id, name=f"Renamed {mode}/{device_mode}", notes="Metadata only")
                self.assertEqual(self.output_state(channel.id), before)
                self.assertEqual(self.persisted(channel.id).mode.value, mode)

    def test_geometry_change_preserves_existing_rotation_and_identity(self):
        created = self.create(rotation=270)
        output_id = created["device_outputs"][0]["id"]
        self.update(created["id"], resolution_width=1280, resolution_height=720)
        output = self.persisted(created["id"]).device_outputs[0]
        self.assertEqual((output.id, output.rotation, output.width, output.height), (output_id, 270, 1280, 720))

    def test_explicit_rotation_is_rejected_outside_normal_single(self):
        for mode, device_mode in [("expanded", "mirror"), ("expanded", "extended"), ("expanded", "independent"),
                                  ("videowall", "single"), ("audio", "single"), ("touch", "single"), ("normal", "mirror")]:
            with self.subTest(mode=mode, device_mode=device_mode):
                response = self.http.post("/api/channels/", json={"branch_id": self.branch.id, "name": "Out of scope",
                    "mode": mode, "device_mode": device_mode, "rotation": 90})
                self.assertEqual(response.status_code, 422, response.text)
        created = self.create(mode="expanded", device_mode="extended", screen_count=2)
        before = self.output_state(created["id"])
        response = self.http.patch(f"/api/channels/{created['id']}", json={"rotation": 180, "name": "Rejected"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.output_state(created["id"]), before)
        self.assertEqual(self.persisted(created["id"]).name, created["name"])

    def test_rotation_is_validated_against_requested_mode_transition(self):
        created = self.create(rotation=270)
        response = self.http.patch(f"/api/channels/{created['id']}", json={"mode": "expanded", "device_mode": "mirror", "rotation": 90})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.persisted(created["id"]).device_outputs[0].rotation, 270)
        expanded = self.create(mode="expanded", device_mode="mirror", screen_count=2)
        updated = self.update(expanded["id"], mode="normal", device_mode="single", rotation=180)
        self.assertEqual(updated["device_outputs"][0]["rotation"], 180)

    def test_missing_legacy_output_is_only_created_on_explicit_rotation(self):
        channel = Channel(client_id=self.tenant.id, branch_id=self.branch.id, name="Legacy no outputs")
        self.db.add(channel)
        self.db.commit()
        channel_id = channel.id
        self.update(channel_id, name="Legacy renamed")
        self.assertEqual(self.output_state(channel_id), [])
        self.update(channel_id, rotation=90)
        self.assertEqual(self.persisted(channel_id).device_outputs[0].rotation, 90)

    def test_missing_primary_identity_does_not_overwrite_another_output(self):
        created = self.create(rotation=270)
        channel = self.persisted(created["id"])
        channel.device_outputs[0].display_order = 2
        self.db.commit()
        before = self.output_state(channel.id)
        response = self.http.patch(f"/api/channels/{channel.id}", json={"rotation": 90, "name": "Rejected"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.output_state(channel.id), before)
        self.assertEqual(self.persisted(channel.id).name, created["name"])

    def test_client_and_branch_isolation_for_creation_and_updates(self):
        own = self.create(rotation=90)
        other = self.create(branch_id=self.other_branch.id, rotation=270)
        self.user = SimpleNamespace(role=UserRole.CLIENT_ADMIN, client_id=self.other_tenant.id, branch_id=None)
        response = self.http.patch(f"/api/channels/{own['id']}", json={"rotation": 180})
        self.assertEqual(response.status_code, 403)
        response = self.http.post("/api/channels/", json={"branch_id": self.branch.id, "name": "Cross tenant", "rotation": 180})
        self.assertEqual(response.status_code, 403)
        self.user = SimpleNamespace(role=UserRole.BRANCH_MANAGER, client_id=self.tenant.id, branch_id=self.branch.id)
        response = self.http.patch(f"/api/channels/{other['id']}", json={"rotation": 180})
        self.assertEqual(response.status_code, 403)
        response = self.http.post("/api/channels/", json={"branch_id": self.other_branch.id, "name": "Cross branch", "rotation": 180})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.persisted(other["id"]).device_outputs[0].rotation, 270)
        self.update(own["id"], rotation=180)
        self.assertEqual(self.persisted(own["id"]).device_outputs[0].rotation, 180)

    def test_read_only_role_cannot_write_rotation(self):
        created = self.create(rotation=90)
        self.user = SimpleNamespace(role=UserRole.OPERATOR, client_id=self.tenant.id, branch_id=self.branch.id)
        response = self.http.patch(f"/api/channels/{created['id']}", json={"rotation": 180})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.persisted(created["id"]).device_outputs[0].rotation, 90)

    def test_authenticated_player_runtime_delivers_persisted_angles_after_rename(self):
        created = self.create()
        token = "tpw_local_rotation_fixture"
        self.db.add(PlayerDevice(channel_id=created["id"], client_id=self.tenant.id, branch_id=self.branch.id,
            hardware_id="fixture", device_name="Fixture", player_token_hash=hashlib.sha256(token.encode()).hexdigest()))
        self.db.commit()
        for angle in ANGLES:
            with self.subTest(angle=angle):
                self.update(created["id"], rotation=angle)
                self.update(created["id"], name=f"Renamed at {angle}")
                response = self.http.get("/api/player/runtime-config", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 200, response.text)
                runtime = response.json()
                self.assertEqual(runtime["runtime_version"], 3)
                self.assertEqual(runtime["outputs"][0]["rotation"], angle)
                self.assertEqual(runtime["channel"]["name"], f"Renamed at {angle}")
                self.assertEqual(runtime["media_retention"]["schema_version"], 1)

    def test_rotation_does_not_change_campaign_selection_playlist_or_retention(self):
        created = self.create()
        current = Campaign(client_id=self.tenant.id, name="Current")
        future = Campaign(client_id=self.tenant.id, name="Future")
        fallback = Campaign(client_id=self.tenant.id, name="Fallback")
        self.db.add_all([current, future, fallback])
        self.db.flush()
        for index, content_type in enumerate((ContentType.IMAGE, ContentType.VIDEO), 1):
            content = ContentItem(client_id=self.tenant.id, name=f"Mixed {index}", type=content_type,
                file_path=f"/media/{self.tenant.id}/{index}.{'jpg' if index == 1 else 'mp4'}")
            self.db.add(content)
            self.db.flush()
            self.db.add(CampaignSequenceItem(campaign_id=current.id, item_type=CampaignSequenceItemType.CONTENT,
                content_id=content.id, sort_order=index, duration_seconds=15))
        self.db.add_all([
            Schedule(client_id=self.tenant.id, channel_id=created["id"], campaign_id=current.id, title="Now", timezone="UTC"),
            Schedule(client_id=self.tenant.id, branch_id=self.branch.id, campaign_id=future.id, title="Future",
                starts_on=date(2027, 1, 1), timezone="UTC"),
            CampaignChannelAssignment(channel_id=created["id"], campaign_id=fallback.id),
        ])
        self.db.commit()
        with patch("app.services.player_runtime.now_utc", return_value=NOW):
            before = build_player_runtime_config(self.persisted(created["id"]), self.db, "http://testserver/", snapshot_coherent=True)
            self.update(created["id"], rotation=270)
            after = build_player_runtime_config(self.persisted(created["id"]), self.db, "http://testserver/", snapshot_coherent=True)
        self.assertEqual(before["campaigns"][0]["campaign_id"], current.id)
        self.assertEqual([item["content"]["type"] for item in after["playlist"]], ["image", "video"])
        self.assertTrue(after["media_retention"]["complete"])
        self.assertEqual(set(after["media_retention"]["protected_campaign_ids"]), {current.id, future.id, fallback.id})
        self.assertEqual(after["media_retention"], before["media_retention"])
        expected = deepcopy(before)
        expected["outputs"][0]["rotation"] = 270
        self.assertEqual(after, expected)
