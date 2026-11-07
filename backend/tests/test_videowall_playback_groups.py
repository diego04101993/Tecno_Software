"""Flexible physical/playback scopes use isolated SQLite or explicit QA schemas."""
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Depends
from sqlalchemy import select, func

from app.api.deps import get_current_user, get_db
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignSequenceItem, Channel, ChannelMode, ContentItem, ContentType, DeviceOutput,
    PlayerDevice, VideowallNode, VideowallPlaybackGroup, VideowallPlaybackGroupMember,
    VideowallPublication, Videowall, User, UserRole,
)
from app.services import videowall_publications as publications
from app.services.player_runtime import build_player_runtime_config
from app.services.videowall_playback_groups import build_group_geometry_snapshot
from tests.test_videowall_publications import VideowallPublicationFixture, _png


class VideowallPlaybackGroupsTests(VideowallPublicationFixture):
    def setUp(self):
        super().setUp()
        self.devices = []
        for index, channel in enumerate(self.channels, 1):
            device = PlayerDevice(channel_id=channel.id, client_id=self.client.id, branch_id=self.branch.id,
                hardware_id=f"SYNTHETIC-FLEX-{index}", device_name=f"QA {index}",
                player_token_hash=hashlib.sha256(f"synthetic-token-{index}".encode()).hexdigest(),
                metadata_json={"issued_player_token": f"synthetic-token-{index}"})
            self.devices.append(device)
            self.db.add(DeviceOutput(root_channel_id=channel.id, channel_id=channel.id, name="Main",
                display_order=0, width=1920, height=1080, source_width=1920, source_height=1080))
        self.db.add_all(self.devices)
        self.db.commit()
        self.before_identity = self.identities()
        self.sync_campaign = self.program("Sync 1x2", 3840)
        self.individual1 = self.program("Individual 1", 1920)
        self.individual2 = self.program("Individual 2", 1920)

    def identities(self):
        self.db.expire_all()
        return [(c.id, c.channel_code, d.id, d.player_token_hash, d.hardware_id, d.metadata_json)
                for c, d in zip(self.channels, self.devices)]

    def program(self, name, width):
        relative = f"{self.client.id}/{uuid4().hex}.png"
        _png(self.root / relative, width, 1080)
        campaign = Campaign(client_id=self.client.id, name=name, playback_mode="sequential")
        content = ContentItem(client_id=self.client.id, name=name, type=ContentType.IMAGE,
            file_path="/media/" + relative, duration_seconds=2,
            metadata_json={"width": width, "height": 1080, "content_type": "image/png"})
        self.db.add_all([campaign, content]); self.db.flush()
        self.db.add(CampaignSequenceItem(campaign_id=campaign.id, content_id=content.id,
            sort_order=1, duration_seconds=2, is_enabled=True))
        self.db.commit()
        return campaign

    def layout(self, columns=2, node_indexes=(0, 1)):
        return self.http.put(f"/api/videowalls/{self.wall.id}/physical-layout", json=dict(
            rows=1, columns=columns, total_width=columns * 1920, total_height=1080,
            output_width=1920, output_height=1080,
            nodes=[dict(node_id=self.nodes[i].id, row_index=0, column_index=i) for i in node_indexes]))

    def group(self, mode="SYNCHRONIZED", nodes=(0, 1), campaign=None, identity=None):
        payload = dict(name="QA " + mode, mode=mode,
            campaign_id=(campaign or self.sync_campaign).id, node_ids=[self.nodes[i].id for i in nodes])
        if identity:
            payload["id"] = identity
        return payload

    def configure(self, groups):
        return self.http.put(f"/api/videowalls/{self.wall.id}/playback-groups", json={"groups": groups})

    def ready_group(self, identity):
        self.db.expire_all()
        group = self.db.get(VideowallPlaybackGroup, identity)
        publication = publications.create_publication(self.db, self.wall, group.campaign_id, self.actor, playback_group=group)
        work = publications.claim_publication(self.db, "SYNTHETIC-GROUP-WORKER")
        self.assertEqual(work.publication_id, publication.id)
        prepared = {}
        for item in work.program_snapshot_json["items"]:
            slices = []
            for node in work.geometry_snapshot_json["nodes"]:
                path = Path(self.directory.name) / f"{publication.id}-{node['position_index']}.png"
                _png(path, 1920, 1080)
                slices.append(SimpleNamespace(position_index=node["position_index"], path=path,
                    width=1920, height=1080, mime_type="image/png", size_bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(), duration_ms=2000,
                    fps_num=None, fps_den=None, frame_count=None, has_audio=False, keyframe_pts_ms=()))
            master = publications.resolve_publication_master(self.db, work, item)
            prepared[item["id"]] = SimpleNamespace(kind="image", duration_ms=2000,
                source_sha256=hashlib.sha256(master.read_bytes()).hexdigest(), slices=tuple(slices),
                fps_num=None, fps_den=None, frame_count=None)
        publications.complete_publication(self.db, publication.id, work.lease_token, prepared)
        self.db.refresh(publication)
        return publications.activate_publication(self.db, self.wall, publication.id, self.actor)

    def runtime(self, index):
        self.db.expire_all()
        return build_player_runtime_config(self.channels[index], self.db, "http://qa.local")

    def test_shrink_preserves_all_channel_device_code_and_token_identities(self):
        self.assertEqual(self.layout().status_code, 200)
        self.assertEqual(self.identities(), self.before_identity)
        self.assertFalse(self.db.get(VideowallNode, self.nodes[2].id).layout_enabled)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(PlayerDevice)), 3)
        self.db.refresh(self.wall)
        self.assertEqual((self.wall.rows, self.wall.columns, self.wall.total_width), (1, 2, 3840))

    def test_historical_three_node_publication_and_epoch_survive_layout_change(self):
        old = self.ready()
        publications.activate_publication(self.db, self.wall, old.id, self.actor)
        epoch = old.loop_start_epoch_ms
        self.assertEqual(self.layout().status_code, 200)
        self.db.expire_all()
        old = self.db.get(VideowallPublication, old.id)
        self.assertEqual(old.loop_start_epoch_ms, epoch)
        self.assertEqual(old.status, "ACTIVE")
        self.assertEqual(self.identities(), self.before_identity)
        self.assertIsNone(self.wall.active_publication_id)

    def test_sync_two_nodes_same_publication_epoch_and_local_crop(self):
        self.assertEqual(self.layout().status_code, 200)
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        group_id = next(g["id"] for g in response.json() if g["members"])
        pub = self.ready_group(group_id)
        left, right = self.runtime(0), self.runtime(1)
        self.assertIsNone(left["fallback"]); self.assertIsNone(right["fallback"])
        for i, runtime in enumerate((left, right), 1):
            wall = runtime["videowall"]
            self.assertEqual((wall["rows"], wall["columns"], wall["total_width"]), (1, 2, 3840))
            self.assertEqual(wall["publication_id"], pub.id)
            self.assertEqual(wall["sync"]["loop_start_epoch_ms"], pub.loop_start_epoch_ms)
            self.assertEqual(wall["node"]["position_index"], i)
            self.assertEqual(wall["node"]["crop_x"], (i - 1) * 1920)
        self.assertNotEqual(left["playlist"][0]["content_id"], right["playlist"][0]["content_id"])
        self.assertEqual(self.identities(), self.before_identity)

    def test_sync_individual_sync_reuses_group_publication_epoch_without_activation(self):
        self.assertEqual(self.layout().status_code, 200)
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        sync_id = response.json()[0]["id"]
        publication = self.ready_group(sync_id)
        epoch = publication.loop_start_epoch_ms
        response = self.configure([self.group("INDIVIDUAL", (0,), self.individual1),
                                   self.group("INDIVIDUAL", (1,), self.individual2)])
        self.assertEqual(response.status_code, 200, response.text)
        for index, campaign in enumerate((self.individual1, self.individual2)):
            runtime = self.runtime(index)
            self.assertEqual(runtime["mode"], "normal")
            self.assertEqual(runtime["channel"]["mode"], "normal")
            self.assertEqual(self.channels[index].mode.value, "videowall")
            self.assertIsNone(runtime["videowall"])
            self.assertEqual({x["campaign_id"] for x in runtime["playlist"]}, {campaign.id})
            self.assertEqual(runtime["playlist"][0]["content"]["metadata_json"]["width"], 1920)
            self.assertEqual(runtime["outputs"][0]["geometry"], dict(x=0, y=0, width=1920, height=1080))
        response = self.configure([self.group(identity=sync_id)])
        self.assertEqual(response.status_code, 200, response.text)
        for i in (0, 1):
            runtime = self.runtime(i)
            self.assertEqual(runtime["videowall"]["publication_id"], publication.id)
            self.assertEqual(runtime["videowall"]["sync"]["loop_start_epoch_ms"], epoch)
        self.assertEqual(self.identities(), self.before_identity)

    def test_mixed_physical_positions_two_three_become_group_local_one_two(self):
        response = self.configure([self.group("INDIVIDUAL", (0,), self.individual1), self.group(nodes=(1, 2))])
        self.assertEqual(response.status_code, 200, response.text)
        sync = next(g for g in response.json() if g["mode"] == "SYNCHRONIZED")
        publication = self.ready_group(sync["id"])
        individual = self.runtime(0)
        self.assertIsNone(self.runtime(0)["videowall"])
        self.assertEqual(individual["mode"], "normal")
        self.assertEqual(individual["channel"]["mode"], "normal")
        self.assertEqual(self.channels[0].mode.value, "videowall")
        self.assertEqual({item["campaign_id"] for item in individual["playlist"]}, {self.individual1.id})
        self.assertTrue(individual["playlist"])
        for index in (1, 2):
            runtime = self.runtime(index)
            wall = runtime["videowall"]
            self.assertEqual(wall["node"]["position_index"], index)
            self.assertEqual(wall["physical_node"]["position_index"], index + 1)
            self.assertEqual(wall["node"]["crop_x"], (index - 1) * 1920)
            self.assertEqual(wall["publication_id"], publication.id)
            self.assertEqual(wall["sync"]["loop_start_epoch_ms"], publication.loop_start_epoch_ms)
            self.assertEqual({item["campaign_id"] for item in runtime["playlist"]}, {self.sync_campaign.id})
            self.assertTrue(runtime["playlist"])

    def test_overlapping_group_membership_rejected_atomically(self):
        response = self.configure([self.group(), self.group("INDIVIDUAL", (0,), self.individual1)])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPlaybackGroupMember)), 0)

    def test_non_rectangular_non_contiguous_group_rejected(self):
        response = self.configure([self.group(nodes=(0, 2))])
        self.assertIn(response.status_code, (409, 422))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(VideowallPlaybackGroupMember)), 0)

    def test_missing_node_and_foreign_campaign_rejected(self):
        payload = self.group(); payload["node_ids"][0] = str(uuid4())
        self.assertEqual(self.configure([payload]).status_code, 422)
        foreign = Campaign(client_id=self.foreign.id, name="Synthetic foreign")
        self.db.add(foreign); self.db.commit()
        response = self.configure([self.group(campaign=foreign)])
        self.assertEqual(response.status_code, 409)

    def test_individual_count_and_synchronized_count_rejected(self):
        self.assertIn(self.configure([self.group("INDIVIDUAL")]).status_code, (409, 422))
        self.assertIn(self.configure([self.group(nodes=(0,))]).status_code, (409, 422))

    def test_partial_pending_publication_cannot_activate(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = response.json()[0]["id"]
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications",
            json=dict(campaign_id=self.sync_campaign.id, playback_group_id=identity))
        self.assertEqual(response.status_code, 201, response.text)
        publication = response.json()
        self.assertEqual(publication["expected_asset_count"], 2)
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications/{publication['id']}/activate")
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.db.get(VideowallPlaybackGroup, identity).active_publication_id)

    def test_legacy_videowall_paths_reject_grouped_topology_changes_but_allow_name(self):
        self.assertEqual(self.layout().status_code, 200)
        response = self.http.patch(f"/api/videowalls/{self.wall.id}", json={"name": "renamed"})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.http.patch(f"/api/videowalls/{self.wall.id}", json={"columns": 3, "total_width": 5760})
        self.assertEqual(response.status_code, 409)
        response = self.http.patch(f"/api/videowalls/{self.wall.id}/nodes/{self.nodes[0].id}", json={"position_index": 2})
        self.assertEqual(response.status_code, 409)

    def test_incompatible_physical_edit_rejected_when_group_has_current_members(self):
        response = self.configure([self.group(nodes=(1, 2))]); self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.layout().status_code, 409)
        self.db.expire_all()
        self.assertEqual(self.wall.columns, 3)
        self.assertTrue(self.nodes[2].layout_enabled)

    def test_group_reads_preserve_historical_group_ids_after_replacement(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        original_id = response.json()[0]["id"]
        response = self.configure([]); self.assertEqual(response.status_code, 200, response.text)
        history = next(g for g in response.json() if g["id"] == original_id)
        self.assertEqual(history["members"], [])
        self.assertEqual(history["columns"], 0)
        self.assertEqual(self.identities(), self.before_identity)

    def test_boolean_and_unknown_fields_rejected(self):
        response = self.http.put(f"/api/videowalls/{self.wall.id}/physical-layout", json=dict(
            rows=True, columns=2, total_width=3840, total_height=1080, output_width=1920, output_height=1080, nodes=[]))
        self.assertEqual(response.status_code, 422)
        group = self.group(); group["node_ids"] = [True]
        self.assertEqual(self.configure([group]).status_code, 422)

    def test_campaign_change_clears_current_pointer_preserves_old_publication_history(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = response.json()[0]["id"]
        old = self.ready_group(identity)
        epoch = old.loop_start_epoch_ms
        next_campaign = self.program("Next synchronized campaign", 3840)
        response = self.configure([self.group(campaign=next_campaign, identity=identity)])
        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()
        self.assertIsNone(self.db.get(VideowallPlaybackGroup, identity).active_publication_id)
        self.assertEqual(self.db.get(VideowallPublication, old.id).loop_start_epoch_ms, epoch)
        listed = self.http.get(f"/api/videowalls/{self.wall.id}/publications?playback_group_id={identity}")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertIn(old.id, {p["id"] for p in listed.json()})
        self.assertEqual(self.identities(), self.before_identity)

    def test_group_read_neutralizes_incompatible_publication_pointer(self):
        old = self.ready()
        publications.activate_publication(self.db, self.wall, old.id, self.actor)
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = response.json()[0]["id"]
        group = self.db.get(VideowallPlaybackGroup, identity)
        group.active_publication_id = old.id  # synthetic corrupt legacy pointer
        self.db.commit()
        response = self.http.get(f"/api/videowalls/{self.wall.id}/playback-groups")
        self.assertEqual(response.status_code, 200, response.text)
        returned = next(g for g in response.json() if g["id"] == identity)
        self.assertIsNone(returned["active_publication_id"])
        self.assertNotIn(old.id, response.text)
        group = self.group(); group["active_publication_id"] = str(uuid4())
        self.assertEqual(self.configure([group]).status_code, 422)

    def _business_state(self):
        """All rows, including identity and publication tables, inside this QA DB."""
        self.db.expire_all()
        return {table.name: [tuple(row) for row in self.db.execute(
            select(table).order_by(*table.primary_key.columns))]
            for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name)}

    @contextmanager
    def _authenticated_as(self, user):
        identity = user.id
        overrides = self.http.app.dependency_overrides
        original = overrides[get_current_user]

        def persisted_actor(db=Depends(get_db)):
            return db.get(User, identity)

        overrides[get_current_user] = persisted_actor
        try:
            yield
        finally:
            overrides[get_current_user] = original

    def _qa_actor(self, role, client_id=None, branch_id=None):
        user = User(email=uuid4().hex + "@example.com", full_name="Synthetic RBAC actor",
            password_hash="synthetic-not-a-login", role=role, client_id=client_id,
            branch_id=branch_id, status="active", is_active=True)
        self.db.add(user); self.db.commit()
        return user

    def _external_group(self, same_client=False):
        owner = self.client if same_client else self.foreign
        branch = self.branch if same_client else Branch(client_id=owner.id,
            name="Synthetic foreign branch", code=uuid4().hex[:12])
        self.db.add(branch); self.db.flush()
        wall = Videowall(client_id=owner.id, name="Synthetic other wall", render_mode="multi-node",
            playback_groups_enabled=True, rows=1, columns=1, total_width=1920, total_height=1080,
            output_width=1920, output_height=1080)
        channel = Channel(client_id=owner.id, branch_id=branch.id, name="Synthetic other node",
            mode=ChannelMode.VIDEOWALL, resolution_width=1920, resolution_height=1080,
            canvas_width=1920, canvas_height=1080)
        campaign = Campaign(client_id=owner.id, name="Synthetic other campaign", playback_mode="sequential")
        self.db.add_all([wall, channel, campaign]); self.db.flush()
        node = VideowallNode(videowall_id=wall.id, channel_id=channel.id, position_index=1,
            row_index=0, column_index=0, x=0, y=0, width=1920, height=1080)
        group = VideowallPlaybackGroup(client_id=owner.id, branch_id=branch.id, wall_id=wall.id,
            name="Synthetic other group", mode="INDIVIDUAL", campaign_id=campaign.id)
        self.db.add_all([node, group]); self.db.flush()
        self.db.add(VideowallPlaybackGroupMember(group_id=group.id, node_id=node.id,
            position_index=1, row_index=0, column_index=0))
        self.db.commit()
        return group, node

    def test_foreign_and_other_wall_nodes_cannot_enter_group_or_physical_layout(self):
        for same_client in (False, True):
            with self.subTest(same_client_other_wall=same_client):
                external, node = self._external_group(same_client)
                before = self._business_state()
                payload = self.group(); payload["node_ids"][1] = node.id
                response = self.configure([payload])
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn(node.id, response.text)
                self.assertNotIn(external.id, response.text)
                self.assertEqual(self._business_state(), before)
                response = self.http.put(f"/api/videowalls/{self.wall.id}/physical-layout", json=dict(
                    rows=1, columns=2, total_width=3840, total_height=1080,
                    output_width=1920, output_height=1080,
                    nodes=[dict(node_id=self.nodes[0].id, row_index=0, column_index=0),
                           dict(node_id=node.id, row_index=0, column_index=1)]))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn(node.id, response.text)
                self.assertEqual(self._business_state(), before)

    def test_foreign_and_other_wall_group_ids_cannot_be_reused_or_prepared(self):
        for same_client in (False, True):
            with self.subTest(same_client_other_wall=same_client):
                external, node = self._external_group(same_client)
                before = self._business_state()
                response = self.configure([self.group(identity=external.id)])
                self.assertEqual(response.status_code, 409, response.text)
                self.assertNotIn(external.id, response.text)
                self.assertEqual(self._business_state(), before)
                response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json=dict(
                    campaign_id=self.sync_campaign.id, playback_group_id=external.id))
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn(external.id, response.text)
                self.assertNotIn(node.channel_id, response.text)
                self.assertEqual(self._business_state(), before)

    def test_group_read_and_publication_filter_never_return_other_wall_identifiers(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        local = next(group for group in response.json() if group["members"])
        for same_client in (False, True):
            with self.subTest(same_client_other_wall=same_client):
                external, node = self._external_group(same_client)
                before = self._business_state()
                response = self.http.get(f"/api/videowalls/{self.wall.id}/playback-groups")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(local["id"], {group["id"] for group in response.json()})
                for identity in (external.id, external.wall_id, external.campaign_id, node.id, node.channel_id):
                    self.assertNotIn(identity, response.text)
                response = self.http.get(f"/api/videowalls/{self.wall.id}/publications?playback_group_id={external.id}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), [])
                self.assertEqual(self._business_state(), before)

    def test_mixed_branch_group_rejected_without_any_business_mutation(self):
        self.channels[1].branch_id = self.other_branch.id
        self.db.commit()
        before = self._business_state()
        response = self.configure([self.group()])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self._business_state(), before)

    def test_branch_corruption_after_group_creation_blocks_publication_without_effects(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = next(group["id"] for group in response.json() if group["members"])
        self.channels[1].branch_id = self.other_branch.id
        self.db.commit()
        before = self._business_state()
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json=dict(
            campaign_id=self.sync_campaign.id, playback_group_id=identity))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self._business_state(), before)

    def test_client_b_denied_all_group_layout_read_write_and_publication_paths(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = next(group["id"] for group in response.json() if group["members"])
        response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json=dict(
            campaign_id=self.sync_campaign.id, playback_group_id=identity))
        self.assertEqual(response.status_code, 201, response.text)
        publication_id = response.json()["id"]
        actor = self._qa_actor(UserRole.CLIENT_ADMIN, self.foreign.id)
        before = self._business_state()
        with self._authenticated_as(actor):
            requests = [
                ("GET", "playback-groups", None),
                ("GET", f"publications?playback_group_id={identity}", None),
                ("PUT", "playback-groups", {"groups": [self.group(identity=identity)]}),
                ("PUT", "physical-layout", dict(rows=1, columns=2, total_width=3840, total_height=1080,
                    output_width=1920, output_height=1080, nodes=[dict(node_id=self.nodes[i].id,
                        row_index=0, column_index=i) for i in (0, 1)])),
                ("POST", "publications", dict(campaign_id=self.sync_campaign.id, playback_group_id=identity)),
                ("POST", f"publications/{publication_id}/activate", None),
            ]
            for method, path, payload in requests:
                with self.subTest(method=method, path=path):
                    response = self.http.request(method, f"/api/videowalls/{self.wall.id}/{path}", json=payload)
                    self.assertEqual(response.status_code, 403, response.text)
                    for private_id in (identity, publication_id, self.sync_campaign.id, self.nodes[0].id):
                        self.assertNotIn(private_id, response.text)
                    self.assertEqual(self._business_state(), before)

    def test_branch_operator_reads_own_groups_but_cannot_configure_or_prepare(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        identity = next(group["id"] for group in response.json() if group["members"])
        actor = self._qa_actor(UserRole.OPERATOR, self.client.id, self.branch.id)
        before = self._business_state()
        with self._authenticated_as(actor):
            response = self.http.get(f"/api/videowalls/{self.wall.id}/playback-groups")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({group["id"] for group in response.json()}, {identity})
            for method, path, payload in [
                ("PUT", "playback-groups", {"groups": [self.group(identity=identity)]}),
                ("PUT", "physical-layout", dict(rows=1, columns=2, total_width=3840, total_height=1080,
                    output_width=1920, output_height=1080, nodes=[])),
                ("POST", "publications", dict(campaign_id=self.sync_campaign.id, playback_group_id=identity)),
            ]:
                response = self.http.request(method, f"/api/videowalls/{self.wall.id}/{path}", json=payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self._business_state(), before)

    def test_missing_or_different_branch_actor_cannot_read_or_configure_groups(self):
        response = self.configure([self.group()]); self.assertEqual(response.status_code, 200, response.text)
        for branch_id, expected in ((None, 403), (self.other_branch.id, 404)):
            actor = self._qa_actor(UserRole.OPERATOR, self.client.id, branch_id)
            before = self._business_state()
            with self._authenticated_as(actor):
                response = self.http.get(f"/api/videowalls/{self.wall.id}/playback-groups")
                self.assertEqual(response.status_code, expected, response.text)
                response = self.http.put(f"/api/videowalls/{self.wall.id}/playback-groups", json={"groups": [self.group()]})
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(self._business_state(), before)

    def test_staff_workspace_roles_can_read_configure_and_prepare_groups(self):
        identity = None
        for role in (UserRole.STAFF_ADMIN, UserRole.STAFF_OPERATOR):
            with self.subTest(role=role.value):
                actor = self._qa_actor(role)
                with self._authenticated_as(actor):
                    self.assertEqual(self.layout().status_code, 200)
                    response = self.configure([self.group(identity=identity)])
                    self.assertEqual(response.status_code, 200, response.text)
                    identity = next(group["id"] for group in response.json() if group["members"])
                    response = self.http.get(f"/api/videowalls/{self.wall.id}/playback-groups")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn(identity, {group["id"] for group in response.json()})
                    response = self.http.post(f"/api/videowalls/{self.wall.id}/publications", json=dict(
                        campaign_id=self.sync_campaign.id, playback_group_id=identity))
                    self.assertEqual(response.status_code, 201, response.text)
                    publication_id = response.json()["id"]
                    response = self.http.get(f"/api/videowalls/{self.wall.id}/publications?playback_group_id={identity}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn(publication_id, {publication["id"] for publication in response.json()})
        self.assertEqual(self.identities(), self.before_identity)
