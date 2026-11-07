"""Domain/API tests use only an empty SQLite DB or an explicitly isolated QA schema."""
from copy import copy, deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock
from uuid import uuid4
import zlib

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.routes import videowalls
from app.core.config import get_settings
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignSequenceItem, Channel, ChannelMode, Client,
    ContentItem, ContentType, User, UserRole, Videowall, VideowallNode,
    VideowallPublication, VideowallPublicationAsset,
    VideowallPlaybackGroup, VideowallPlaybackGroupMember,
)
from app.schemas.domain import VideowallCreate, VideowallPublicationCreate, VideowallUpdate
from app.services import videowall_publications as publications


def _png(path, width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = (b"\0" + b"\0\0\0" * width) * height
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class VideowallPublicationFixture(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="vw1-domain-qa-")
        self.root = Path(self.directory.name) / "media"
        self.root.mkdir()
        self.settings = copy(get_settings())
        self.settings.MEDIA_ROOT = self.root
        self.patches = [mock.patch(path, return_value=self.settings) for path in (
            "app.core.config.get_settings", "app.services.media_references.get_settings",
            "app.services.videowall_publications.get_settings")]
        for patch in self.patches:
            patch.start()
        url = os.environ.get("VW1_TEST_DATABASE_URL")
        self.pg_admin = None
        self.schema = None
        if url:
            parsed = make_url(url)
            self.assertEqual(parsed.get_backend_name(), "postgresql")
            self.assertIn(parsed.host, {"127.0.0.1", "localhost"})
            self.assertEqual(parsed.database, "vw1_qa")
            self.pg_admin = create_engine(url)
            self.schema = "vw1_domain_" + uuid4().hex
            with self.pg_admin.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
            self.engine = create_engine(url, connect_args={"options": "-csearch_path=" + self.schema})
        else:
            self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                        connect_args={"check_same_thread": False})

            @event.listens_for(self.engine, "connect")
            def foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

            with self.engine.connect() as connection:
                self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.client = Client(name="VW1 QA A", slug="vw1-a")
        self.foreign = Client(name="VW1 QA B", slug="vw1-b")
        self.db.add_all([self.client, self.foreign]); self.db.flush()
        self.branch = Branch(client_id=self.client.id, name="A1", code="A1")
        self.other_branch = Branch(client_id=self.client.id, name="A2", code="A2")
        self.db.add_all([self.branch, self.other_branch]); self.db.flush()
        self.actor = User(email="vw1-qa@example.com", full_name="Synthetic VW1 QA",
                          password_hash="synthetic-not-a-login", role=UserRole.CLIENT_ADMIN,
                          client_id=self.client.id, status="active", is_active=True)
        self.wall = Videowall(client_id=self.client.id, name="QA wall", render_mode="multi-node",
            media_delivery="node_slice", rows=1, columns=3, total_width=5760, total_height=1080,
            output_width=1920, output_height=1080, start_tolerance_ms=250)
        self.campaign = Campaign(client_id=self.client.id, name="QA program", playback_mode="sequential")
        self.db.add_all([self.actor, self.wall, self.campaign]); self.db.flush()
        self.channels = [Channel(client_id=self.client.id, branch_id=self.branch.id,
            name=f"QA node{i}", mode=ChannelMode.VIDEOWALL, resolution_width=1920,
            resolution_height=1080, canvas_width=1920, canvas_height=1080) for i in (1, 2, 3)]
        self.db.add_all(self.channels); self.db.flush()
        self.nodes = [VideowallNode(videowall_id=self.wall.id, channel_id=c.id, position_index=i,
            row_index=0, column_index=i-1, x=(i-1)*1920, y=0, width=1920, height=1080)
            for i, c in enumerate(self.channels, 1)]
        master_path = self.root / self.client.id / "master.png"
        _png(master_path, 5760, 1080)
        self.master = ContentItem(client_id=self.client.id, name="QA master", type=ContentType.IMAGE,
                                  file_path=f"/media/{self.client.id}/master.png", duration_seconds=2)
        self.db.add_all([self.master, *self.nodes]); self.db.flush()
        self.item = CampaignSequenceItem(campaign_id=self.campaign.id, content_id=self.master.id,
                                         sort_order=1, duration_seconds=2, is_enabled=True)
        self.db.add(self.item); self.db.commit()
        app = FastAPI()  # No app.main, startup, application DB or seeds.
        app.include_router(videowalls.router, prefix="/api/videowalls")
        actor_id = self.actor.id

        def isolated_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        def isolated_actor(db=Depends(get_db)):
            return db.get(User, actor_id)

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = isolated_actor
        self.http = TestClient(app)

    def tearDown(self):
        self.http.close(); self.db.close(); self.engine.dispose()
        if self.pg_admin is not None:
            with self.pg_admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            self.pg_admin.dispose()
        for patch in reversed(self.patches):
            patch.stop()
        self.directory.cleanup()

    def configure_group(self, indices=(0, 1)):
        self.wall.playback_groups_enabled = True
        group = VideowallPlaybackGroup(client_id=self.client.id, branch_id=self.branch.id,
            wall_id=self.wall.id, name="Synthetic rectangular playback group", mode="SYNCHRONIZED",
            campaign_id=self.campaign.id)
        self.db.add(group); self.db.flush()
        for position, index in enumerate(indices, 1):
            self.db.add(VideowallPlaybackGroupMember(group_id=group.id, node_id=self.nodes[index].id,
                position_index=position, row_index=0, column_index=position - 1))
        self.db.commit()
        _png(self.root / self.client.id / "master.png", len(indices) * 1920, 1080)
        return group

    def prepare(self, playback_group=None):
        return publications.create_publication(self.db, self.wall, self.campaign.id, self.actor,
                                                playback_group=playback_group)

    def result(self, work):
        program = work.program_snapshot_json["items"]
        results = {}
        for item in program:
            slices = []
            for node in work.geometry_snapshot_json["nodes"]:
                position = node["position_index"]
                path = Path(self.directory.name) / (work.publication_id + f"-{position}.png")
                _png(path, node["crop_width"], node["crop_height"])
                slices.append(SimpleNamespace(position_index=position, path=path,
                    width=node["crop_width"], height=node["crop_height"], mime_type="image/png", size_bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(), duration_ms=2000,
                    fps_num=None, fps_den=None, frame_count=None, has_audio=False, keyframe_pts_ms=()))
            path = publications.resolve_publication_master(self.db, work, item)
            results[item["id"]] = SimpleNamespace(kind="image", duration_ms=2000,
                source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), slices=tuple(slices),
                fps_num=None, fps_den=None, frame_count=None)
        return results

    def ready(self, playback_group=None):
        publication = self.prepare(playback_group)
        work = publications.claim_publication(self.db, "synthetic-qa-worker")
        self.assertEqual(work.publication_id, publication.id)
        publications.complete_publication(self.db, publication.id, work.lease_token, self.result(work))
        self.db.refresh(publication)
        self.assertEqual(publication.status, "READY")
        return publication


class VideowallPublicationTests(VideowallPublicationFixture):
    def test_schema_defaults_and_no_request_control_of_publication_authority(self):
        self.assertEqual(VideowallCreate(name="legacy").media_delivery, "legacy")
        self.assertIsNone(VideowallUpdate(name="rename").media_delivery)
        for field in ("loop_start_epoch_ms", "status", "file_path", "client_id"):
            with self.subTest(field=field):
                response = self.http.post(f"/api/videowalls/{self.wall.id}/publications",
                    json={"campaign_id": self.campaign.id, field: "synthetic"})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPublication)), 0)

    def test_prepare_persists_owned_three_asset_plan_without_epoch(self):
        publication = self.prepare()
        self.assertEqual(publication.status, "PENDING")
        self.assertIsNone(publication.loop_start_epoch_ms)
        self.assertIsNone(publication.loop_duration_ms)
        self.assertEqual(publication.program_snapshot_json["items"][0]["id"], self.item.id)
        self.assertEqual(publication.program_snapshot_json["items"][0]["duration_ms"], 2000)
        assets = list(self.db.scalars(select(VideowallPublicationAsset)))
        self.assertEqual({a.position_index for a in assets}, {1, 2, 3})
        self.assertEqual({a.master_content_id for a in assets}, {self.master.id})
        self.assertTrue(all(not a.validated and a.derived_content_id is None for a in assets))

    def test_four_k_geometry_preserves_one_based_node_positions(self):
        self.wall.total_width, self.wall.total_height = 11520, 2160
        self.wall.output_width, self.wall.output_height = 3840, 2160
        for c in self.channels:
            c.resolution_width = c.canvas_width = 3840
            c.resolution_height = c.canvas_height = 2160
        self.db.commit()
        geometry = publications.build_geometry_snapshot(self.db, self.wall)
        self.assertEqual([n["position_index"] for n in geometry["nodes"]], [1, 2, 3])
        self.assertEqual([n["crop_x"] for n in geometry["nodes"]], [0, 3840, 7680])
        self.assertEqual((geometry["panel_width"], geometry["panel_height"]), (3840, 2160))

    def test_missing_node_or_mixed_branch_rejected_before_publication(self):
        self.channels[2].branch_id = self.other_branch.id
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.prepare()
        self.db.rollback()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPublication)), 0)

    def test_foreign_campaign_rejected_without_foreign_response(self):
        other = Campaign(client_id=self.foreign.id, name="synthetic foreign")
        self.db.add(other); self.db.commit()
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json={"campaign_id": other.id})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(other.id, response.text)

    def test_random_program_rejected(self):
        self.campaign.playback_mode = "random"; self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self.prepare()
        self.assertEqual(caught.exception.status_code, 422)

    def test_video_duration_waits_for_probe_instead_of_using_default(self):
        self.master.type = ContentType.VIDEO; self.db.commit()
        publication = self.prepare()
        self.assertIsNone(publication.program_snapshot_json["items"][0]["duration_ms"])

    def test_complete_atomically_registers_three_real_derived_content_items(self):
        publication = self.ready()
        assets = list(self.db.scalars(select(VideowallPublicationAsset)))
        self.assertEqual(len({a.derived_content_id for a in assets}), 3)
        self.assertTrue(all(a.validated for a in assets))
        self.assertNotIn(self.master.id, {a.derived_content_id for a in assets})
        for asset in assets:
            content = self.db.get(ContentItem, asset.derived_content_id)
            self.assertEqual(content.client_id, self.client.id)
            _, path = publications._current_master(self.db, self.client.id, content.id)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset.checksum)
        self.assertEqual(publication.loop_duration_ms, 2000)
        self.assertIsNone(publication.loop_start_epoch_ms)

    def test_activation_persists_epoch_once_and_current_activation_is_idempotent(self):
        publication = self.ready()
        now = datetime(2026, 11, 4, 12, tzinfo=UTC)
        with mock.patch.object(publications, "now_utc", return_value=now):
            publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.assertEqual(publication.loop_start_epoch_ms, int(now.timestamp() * 1000) + 10_000)
        previous = publication.loop_start_epoch_ms
        with mock.patch.object(publications, "now_utc", return_value=now + timedelta(days=1)):
            publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.assertEqual(publication.loop_start_epoch_ms, previous)
        self.assertEqual(self.wall.active_publication_id, publication.id)

    def test_failed_candidate_preserves_previous_active_pointer_and_epoch(self):
        active = self.ready()
        publications.activate_publication(self.db, self.wall, active.id, self.actor)
        epoch = active.loop_start_epoch_ms
        candidate = self.prepare()
        work = publications.claim_publication(self.db, "worker-failure")
        self.assertEqual(candidate.id, work.publication_id)
        self.assertTrue(publications.fail_publication(self.db, candidate.id, work.lease_token,
                                                     "VW1_SLICE_FAILED", "/private/synthetic-path"))
        self.db.refresh(candidate); self.db.refresh(active); self.db.refresh(self.wall)
        self.assertEqual(candidate.status, "FAILED")
        self.assertNotIn("private", candidate.error_message)
        self.assertEqual(self.wall.active_publication_id, active.id)
        self.assertEqual(active.status, "ACTIVE")
        self.assertEqual(active.loop_start_epoch_ms, epoch)
        with self.assertRaises(HTTPException):
            publications.activate_publication(self.db, self.wall, candidate.id, self.actor)

    def test_incomplete_slice_set_cannot_be_ready(self):
        publication = self.prepare()
        work = publications.claim_publication(self.db, "worker-incomplete")
        result = self.result(work)
        result[self.item.id].slices = result[self.item.id].slices[:2]
        with self.assertRaises(HTTPException):
            publications.complete_publication(self.db, publication.id, work.lease_token, result)
        self.db.rollback(); self.db.refresh(publication)
        self.assertEqual(publication.status, "PROCESSING")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 1)

    def test_expired_lease_recovery_fences_old_worker(self):
        publication = self.prepare()
        first = publications.claim_publication(self.db, "worker-old")
        publication.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()
        second = publications.claim_publication(self.db, "worker-new")
        self.assertEqual(first.publication_id, second.publication_id)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertFalse(publications.renew_publication_lease(self.db, publication.id, first.lease_token))
        self.assertFalse(publications.fail_publication(self.db, publication.id, first.lease_token, "VW1_OLD_WORKER"))
        self.assertTrue(publications.renew_publication_lease(self.db, publication.id, second.lease_token))

    def test_deactivated_requester_cannot_claim_job(self):
        publication = self.prepare()
        self.actor.status = "suspended"; self.actor.is_active = False; self.db.commit()
        self.assertIsNone(publications.claim_publication(self.db, "worker-revoked"))
        self.db.refresh(publication)
        self.assertEqual(publication.status, "PENDING")

    def test_http_prepare_list_and_explicit_legacy_exit(self):
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json={"campaign_id": self.campaign.id})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "PENDING")
        listing = self.http.get(f"/api/videowalls/{self.wall.id}/publications")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([r["id"] for r in listing.json()], [response.json()["id"]])
        self.assertEqual(listing.json()[0]["validated_asset_count"], 0)
        exit_response = self.http.patch(f"/api/videowalls/{self.wall.id}", json={"media_delivery": "legacy"})
        self.assertEqual(exit_response.status_code, 200, exit_response.text)
        self.assertEqual(exit_response.json()["media_delivery"], "legacy")
        self.assertIsNone(exit_response.json()["active_publication_id"])

    def test_successful_replacement_keeps_old_active_epoch_and_rejects_reactivation(self):
        old = self.ready()
        publications.activate_publication(self.db, self.wall, old.id, self.actor)
        epoch = old.loop_start_epoch_ms
        new = self.ready()
        publications.activate_publication(self.db, self.wall, new.id, self.actor)
        self.db.refresh(old); self.db.refresh(self.wall)
        self.assertEqual(old.status, "ACTIVE")
        self.assertEqual(old.loop_start_epoch_ms, epoch)
        self.assertEqual(self.wall.active_publication_id, new.id)
        with self.assertRaises(HTTPException) as caught:
            publications.activate_publication(self.db, self.wall, old.id, self.actor)
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback(); self.db.refresh(self.wall)
        self.assertEqual(self.wall.active_publication_id, new.id)

    def test_explicit_legacy_exit_clears_pointer_without_rewriting_active_history(self):
        active = self.ready()
        publications.activate_publication(self.db, self.wall, active.id, self.actor)
        epoch = active.loop_start_epoch_ms
        response = self.http.patch(f"/api/videowalls/{self.wall.id}", json={"media_delivery": "legacy"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["active_publication_id"])
        self.db.refresh(active)
        self.assertEqual(active.status, "ACTIVE")
        self.assertEqual(active.loop_start_epoch_ms, epoch)
        response = self.http.patch(f"/api/videowalls/{self.wall.id}", json={"media_delivery": "node_slice"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["active_publication_id"])

    def test_live_playlist_edit_does_not_rewrite_frozen_program(self):
        publication = self.prepare()
        self.item.duration_seconds = 9
        self.db.commit()
        work = publications.claim_publication(self.db, "worker-snapshot")
        self.assertEqual(work.program_snapshot_json["items"][0]["duration_ms"], 2000)
        publications.complete_publication(self.db, publication.id, work.lease_token, self.result(work))
        self.db.refresh(publication)
        self.assertEqual(publication.program_snapshot_json["items"][0]["duration_ms"], 2000)
        self.assertEqual(publication.loop_duration_ms, 2000)

    def test_revoked_requesters_cannot_starve_valid_job_after_first_page(self):
        valid = self.prepare()
        revoked = User(email="revoked-vw1@example.com", full_name="Revoked QA",
            password_hash="synthetic-not-a-login", role=UserRole.CLIENT_ADMIN,
            client_id=self.client.id, is_active=False, status="suspended")
        self.db.add(revoked); self.db.flush()
        before = datetime.now(UTC) - timedelta(days=1)
        for index in range(50):
            self.db.add(VideowallPublication(id=str(uuid4()), client_id=self.client.id,
                branch_id=self.branch.id, wall_id=self.wall.id, campaign_id=self.campaign.id,
                requested_by_user_id=revoked.id, status="PENDING",
                program_snapshot_json=valid.program_snapshot_json,
                geometry_snapshot_json=valid.geometry_snapshot_json,
                geometry_signature=valid.geometry_signature, expected_asset_count=3,
                created_at=before + timedelta(seconds=index)))
        self.db.commit()
        claimed = publications.claim_publication(self.db, "worker-after-revoked-page")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.publication_id, valid.id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPublication).where(
            VideowallPublication.requested_by_user_id == revoked.id,
            VideowallPublication.status == "PROCESSING")), 0)

    def test_activation_lead_is_measured_after_tenant_gate_wait(self):
        from app.services import tenant_mutations
        publication = self.ready()
        before = datetime(2026, 11, 4, 12, tzinfo=UTC)
        after = before + timedelta(minutes=1)
        entered = [False]
        original = tenant_mutations.MutationGate.__enter__

        def traced_enter(gate):
            result = original(gate)
            entered[0] = True
            return result

        with mock.patch.object(tenant_mutations.MutationGate, "__enter__", traced_enter), \
                mock.patch.object(publications, "now_utc", side_effect=lambda: after if entered[0] else before):
            publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.assertTrue(entered[0])
        self.assertEqual(publication.loop_start_epoch_ms, int(after.timestamp() * 1000) + 10_000)


class VideowallGroupPublicationTests(VideowallPublicationFixture):
    def members(self, group):
        return list(self.db.scalars(select(VideowallPlaybackGroupMember).where(
            VideowallPlaybackGroupMember.group_id == group.id).order_by(
                VideowallPlaybackGroupMember.position_index)))

    def test_legacy_geometry_signature_has_identical_keys_and_positions(self):
        expected = dict(rows=1, columns=3, total_width=5760, total_height=1080,
            panel_width=1920, panel_height=1080, tolerance_ms=250, nodes=[
                dict(id=node.id, channel_id=node.channel_id, position_index=index,
                     row_index=0, column_index=index - 1, crop_x=(index - 1) * 1920,
                     crop_y=0, crop_width=1920, crop_height=1080)
                for index, node in enumerate(self.nodes, 1)])
        actual = publications.build_geometry_snapshot(self.db, self.wall)
        self.assertEqual(actual, expected)
        self.assertEqual(publications.geometry_signature(actual), publications.geometry_signature(expected))
        self.assertNotIn("playback_group_id", actual)

    def test_two_node_publication_uses_group_local_positions_and_exact_asset_count(self):
        group = self.configure_group((1, 2))
        physical_positions = [node.position_index for node in self.nodes]
        publication = self.prepare(group)
        self.assertEqual(publication.playback_group_id, group.id)
        self.assertEqual(publication.expected_asset_count, 2)
        geometry = publication.geometry_snapshot_json
        self.assertEqual((geometry["rows"], geometry["columns"], geometry["total_width"], geometry["total_height"]),
                         (1, 2, 3840, 1080))
        self.assertEqual([(n["id"], n["position_index"], n["crop_x"]) for n in geometry["nodes"]],
                         [(self.nodes[1].id, 1, 0), (self.nodes[2].id, 2, 1920)])
        self.assertEqual([node.position_index for node in self.nodes], physical_positions)
        self.assertIsNone(group.active_publication_id)
        self.assertIsNone(self.wall.active_publication_id)

    def test_wrong_campaign_or_individual_group_cannot_prepare_a_publication(self):
        group = self.configure_group()
        other = Campaign(client_id=self.client.id, name="Other owned campaign", playback_mode="sequential")
        self.db.add(other); self.db.commit()
        with self.assertRaises(HTTPException):
            publications.create_publication(self.db, self.wall, other.id, self.actor, playback_group=group)
        self.db.rollback()
        group.mode = "INDIVIDUAL"; self.db.commit()
        with self.assertRaises(HTTPException):
            self.prepare(group)
        self.db.rollback()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPublication)), 0)

    def test_two_node_partial_result_cannot_register_any_derived_content(self):
        group = self.configure_group()
        publication = self.prepare(group)
        work = publications.claim_publication(self.db, "group-partial-worker")
        result = self.result(work)
        result[self.item.id].slices = result[self.item.id].slices[:1]
        with self.assertRaises(HTTPException):
            publications.complete_publication(self.db, publication.id, work.lease_token, result)
        self.db.rollback(); self.db.refresh(publication)
        self.assertEqual(publication.status, "PROCESSING")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 1)
        self.assertTrue(all(asset.derived_content_id is None for asset in self.db.scalars(
            select(VideowallPublicationAsset).where(VideowallPublicationAsset.publication_id == publication.id))))

    def test_group_geometry_change_after_claim_cannot_read_or_complete_work(self):
        group = self.configure_group()
        publication = self.prepare(group)
        work = publications.claim_publication(self.db, "group-geometry-worker")
        result = self.result(work)
        self.members(group)[1].position_index = 3
        self.db.commit()
        self.assertFalse(publications.renew_publication_lease(self.db, publication.id, work.lease_token))
        with self.assertRaises(HTTPException):
            publications.resolve_publication_master(self.db, work, work.program_snapshot_json["items"][0])
        self.db.rollback()
        with self.assertRaises(HTTPException):
            publications.complete_publication(self.db, publication.id, work.lease_token, result)
        self.db.rollback()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 1)

    def test_empty_group_is_not_claimed_and_has_no_new_processing_attempt(self):
        group = self.configure_group()
        publication = self.prepare(group)
        self.db.execute(delete(VideowallPlaybackGroupMember).where(VideowallPlaybackGroupMember.group_id == group.id))
        self.db.commit()
        self.assertIsNone(publications.claim_publication(self.db, "empty-group-worker"))
        self.db.refresh(publication)
        self.assertEqual((publication.status, publication.attempt_count), ("PENDING", 0))

    def test_group_pointer_and_historical_epoch_survive_empty_then_reenabled_group(self):
        from app.services.videowall_integrity import publication_read_safe
        group = self.configure_group((1, 2))
        publication = self.ready(group)
        publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.db.refresh(group)
        epoch = publication.loop_start_epoch_ms
        self.assertEqual(group.active_publication_id, publication.id)
        self.assertIsNone(self.wall.active_publication_id)
        self.db.execute(delete(VideowallPlaybackGroupMember).where(VideowallPlaybackGroupMember.group_id == group.id))
        self.db.commit()
        self.assertTrue(publication_read_safe(self.db, publication.id))
        self.assertEqual(publication.status, "ACTIVE")
        for position, node in enumerate(self.nodes[1:], 1):
            self.db.add(VideowallPlaybackGroupMember(group_id=group.id, node_id=node.id,
                position_index=position, row_index=0, column_index=position - 1))
        self.db.commit()
        publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.db.refresh(publication); self.db.refresh(group)
        self.assertEqual((group.active_publication_id, publication.loop_start_epoch_ms), (publication.id, epoch))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), 3)

    def test_historical_publication_does_not_follow_empty_groups_new_campaign_or_mode(self):
        from app.services.videowall_integrity import publication_read_safe
        group = self.configure_group()
        publication = self.ready(group)
        publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        original_program = deepcopy(publication.program_snapshot_json)
        other = Campaign(client_id=self.client.id, name="New owned campaign", playback_mode="sequential")
        self.db.add(other); self.db.flush()
        self.db.execute(delete(VideowallPlaybackGroupMember).where(VideowallPlaybackGroupMember.group_id == group.id))
        group.mode = "INDIVIDUAL"; group.campaign_id = other.id
        group.active_publication_id = None  # The authorized API clears incompatible current playback.
        self.db.commit()
        self.assertTrue(publication_read_safe(self.db, publication.id))
        self.assertEqual(publication.campaign_id, self.campaign.id)
        self.assertEqual(publication.program_snapshot_json, original_program)

    def test_foreign_group_reference_is_not_readable_even_for_owned_publication(self):
        from app.services.videowall_integrity import publication_read_safe
        group = self.configure_group()
        publication = self.ready(group)
        foreign_branch = Branch(client_id=self.foreign.id, name="B", code="B")
        foreign_wall = Videowall(client_id=self.foreign.id, name="B wall", playback_groups_enabled=True)
        foreign_campaign = Campaign(client_id=self.foreign.id, name="B campaign")
        self.db.add_all([foreign_branch, foreign_wall, foreign_campaign]); self.db.flush()
        foreign_group = VideowallPlaybackGroup(client_id=self.foreign.id, branch_id=foreign_branch.id,
            wall_id=foreign_wall.id, name="B group", mode="SYNCHRONIZED", campaign_id=foreign_campaign.id)
        self.db.add(foreign_group); self.db.flush()
        publication.playback_group_id = foreign_group.id
        self.db.commit()
        self.assertFalse(publication_read_safe(self.db, publication.id))

    def test_ready_group_publication_cannot_activate_for_a_different_current_campaign(self):
        group = self.configure_group()
        publication = self.ready(group)
        other = Campaign(client_id=self.client.id, name="New selected campaign", playback_mode="sequential")
        self.db.add(other); self.db.flush()
        group.campaign_id = other.id
        self.db.commit()
        with self.assertRaises(HTTPException):
            publications.activate_publication(self.db, self.wall, publication.id, self.actor)
        self.db.rollback(); self.db.refresh(publication); self.db.refresh(group)
        self.assertEqual(publication.status, "READY")
        self.assertIsNone(publication.loop_start_epoch_ms)
        self.assertIsNone(group.active_publication_id)
