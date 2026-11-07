"""A7 registry matrix: persisted synthetic fixtures and measured effects only."""
import io
import os
import uuid
import unittest
from contextlib import contextmanager, ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import MetaData, create_engine, event, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tenant_graph_cases import GraphFixture
from app.models.base import Base
from app.models import entities as E
from app.services import tenant_mutations as mutations
from app.services.tenant_ownership import OwnerState, RowRef, TenantGraph
from app.services.tenant_relations import MODEL_SPECS, RELATION_SPECS, model_class, model_spec

COVERAGE = []
ROLES = ("client_admin", "staff_operator", "staff_admin", "super_admin")
VW1_RELATION_KEYS = frozenset((
    "Videowall.active_publication_id", "VideowallPublication.client_id",
    "VideowallPublication.branch_id", "VideowallPublication.wall_id",
    "VideowallPublication.campaign_id", "VideowallPublication.requested_by_user_id",
    "VideowallPublicationAsset.publication_id", "VideowallPublicationAsset.node_id",
    "VideowallPublicationAsset.master_content_id", "VideowallPublicationAsset.derived_content_id",
))
GROUP_RELATION_KEYS = frozenset((
    "VideowallPlaybackGroup.client_id", "VideowallPlaybackGroup.branch_id",
    "VideowallPlaybackGroup.wall_id", "VideowallPlaybackGroup.campaign_id",
    "VideowallPlaybackGroup.active_publication_id", "VideowallPlaybackGroupMember.group_id",
    "VideowallPlaybackGroupMember.node_id", "VideowallPublication.playback_group_id",
))
PROTECTED_GROUP_RELATION_KEYS = frozenset((
    "VideowallPlaybackGroup.active_publication_id", "VideowallPublication.playback_group_id",
))


class DeleteMatrixFixture(GraphFixture):
    """Small explicit relationship scenarios; metadata supplies only QA scalar defaults."""
    def setUp(self):
        self.engine = self.make_engine()
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
            # PostgreSQL's AddConstraint DDL marks cyclic constraints as external
            # to CREATE TABLE. Never reuse that compiler state for a SQLite DB.
            metadata = MetaData()
            for table in Base.metadata.tables.values():
                table.to_metadata(metadata)
            metadata.create_all(self.engine)
            with self.engine.connect() as connection:
                actual = {(spec.name, row[3], row[2], row[6]) for spec in MODEL_SPECS
                          for row in connection.exec_driver_sql("PRAGMA foreign_key_list(" + spec.table + ")")}
            expected = {(edge.model, edge.field, model_spec(edge.target).table, edge.on_delete or "NO ACTION")
                        for edge in RELATION_SPECS}
            self.assertEqual(actual, expected, "QA schema must contain all 97 real FK effects")
        else:
            Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.graph = TenantGraph(self.db)
        self.seeded = {}
        self.building = set()
        self.actors = {}
        self.vw1_rows = {}
        self.playback_group_rows = {}
        self.playback_group_publication_rows = {}
        for side in ("A", "B"):
            self.ensure("Client", side)
            for role in ROLES:
                if role != "client_admin" and side == "B":
                    continue
                self.actors[side, role] = self.add(E.User, email=uuid.uuid4().hex + "@example.com",
                    full_name="Synthetic QA actor", password_hash="synthetic", role=role,
                    client_id=self.ensure("Client", side).id if role == "client_admin" else None)
        self.db.commit()

    def ensure(self, name, side):
        if name in {"VideowallPlaybackGroup", "VideowallPlaybackGroupMember"}:
            return self._ensure_playback_group(side, with_member=name == "VideowallPlaybackGroupMember")[name]
        if name in {"VideowallPublication", "VideowallPublicationAsset"}:
            return self._ensure_vw1(side)[name]
        key = (name, side)
        if key in self.seeded:
            return self.seeded[key]
        if key in self.building:
            raise AssertionError("Required FK cycle in QA factory: " + name)
        self.building.add(key)
        cls = model_class(name)
        values = {}
        for column in inspect(cls).columns:
            if column.primary_key or column.name in {"created_at", "updated_at"}:
                continue
            if column.nullable:
                continue
            if column.foreign_keys:
                edge = next(e for e in RELATION_SPECS if e.model == name and e.field == column.name)
                values[column.name] = self.ensure(edge.target, side).id
            elif column.default is None and column.server_default is None:
                if getattr(column.type, "enum_class", None):
                    values[column.name] = list(column.type.enum_class)[0]
                elif str(column.type).startswith("DATETIME") or str(column.type).startswith("TIMESTAMP"):
                    values[column.name] = datetime.now(UTC)
                else:
                    values[column.name] = "qa-" + uuid.uuid4().hex[:16]
        if name == "User":
            values.update(role=E.UserRole.CLIENT_ADMIN, client_id=self.ensure("Client", side).id,
                          email=uuid.uuid4().hex + "@example.com")
        if name == "CampaignSequenceItem":
            values.update(item_type="content", content_id=self.ensure("ContentItem", side).id)
        if name == "DatasetImport":
            values["storage_path"] = ""
        obj = self.add(cls, **values)
        self.seeded[key] = obj
        self.building.remove(key)
        return obj

    def _ensure_playback_group(self, side, *, with_member=False):
        """Empty retained group or independent valid member scope, never fabricated IDs.

        An empty foreign target permits persisting a corrupt group_id without
        first colliding with the independent UNIQUE(group_id, position_index).
        The member's intact positive control still has its own complete scope.
        """
        key = side, with_member
        if key not in self.playback_group_rows:
            client = self.ensure("Client", side)
            branch = self.ensure("Branch", side)
            campaign = self.ensure("Campaign", side)
            wall = self.add(E.Videowall, client_id=client.id, name="Group QA " + side,
                rows=1, columns=1, total_width=1920, total_height=1080,
                output_width=1920, output_height=1080, playback_groups_enabled=True)
            channel = self.add(E.Channel, client_id=client.id, branch_id=branch.id,
                name="Group QA panel " + side + (" member" if with_member else " empty"), mode=E.ChannelMode.VIDEOWALL,
                resolution_width=1920, resolution_height=1080, canvas_width=1920, canvas_height=1080)
            node = self.add(E.VideowallNode, videowall_id=wall.id, channel_id=channel.id,
                position_index=1, row_index=0, column_index=0, x=0, y=0, width=1920, height=1080,
                layout_enabled=True)
            group = self.add(E.VideowallPlaybackGroup, client_id=client.id, branch_id=branch.id,
                wall_id=wall.id, name="Group QA " + side, mode="INDIVIDUAL", campaign_id=campaign.id)
            member = (self.add(E.VideowallPlaybackGroupMember, group_id=group.id, node_id=node.id,
                position_index=1, row_index=0, column_index=0) if with_member else None)
            self.playback_group_rows[key] = dict(VideowallPlaybackGroup=group,
                VideowallPlaybackGroupMember=member, client=client, branch=branch, wall=wall,
                campaign=campaign, node=node, channel=channel)
            self.db.commit()
        return self.playback_group_rows[key]

    def _ensure_playback_group_publication(self, side):
        """Real owned, complete ACTIVE scope for the two publication-context edges."""
        if side not in self.playback_group_publication_rows:
            owned = self._ensure_vw1(side, active=True)
            wall, publication = owned["wall"], owned["VideowallPublication"]
            wall.playback_groups_enabled = True
            wall.active_publication_id = None
            group = self.add(E.VideowallPlaybackGroup, client_id=owned["client"].id,
                branch_id=owned["branch"].id, wall_id=wall.id, name="Published group QA " + side,
                mode="SYNCHRONIZED", campaign_id=owned["campaign"].id)
            members = [self.add(E.VideowallPlaybackGroupMember, group_id=group.id, node_id=node.id,
                position_index=node.position_index, row_index=0, column_index=node.position_index - 1)
                for node in owned["nodes"]]
            publication.playback_group_id = group.id
            group.active_publication_id = publication.id
            self.db.commit()
            self.playback_group_publication_rows[side] = dict(owned,
                VideowallPlaybackGroup=group, VideowallPlaybackGroupMember=members[0], members=members)
        return self.playback_group_publication_rows[side]

    def _ensure_vw1(self, side, *, ready=False, active=False):
        """Owned DB-scope fixtures, not codec/media quality test evidence."""
        import hashlib
        import json
        from tempfile import TemporaryDirectory
        from app.core.config import get_settings
        group = self.vw1_rows.get(side)
        if group is None:
            client, branch, requestor = (self.ensure(name, side) for name in ("Client", "Branch", "User"))
            wall = self.add(E.Videowall, client_id=client.id, name="VW1 graph " + side,
                            rows=1, columns=3, total_width=5760, total_height=1080,
                            output_width=1920, output_height=1080)
            node_rows = []
            for position in (1, 2, 3):
                channel = self.add(E.Channel, client_id=client.id, branch_id=branch.id,
                                   name="VW1 graph " + side + " " + str(position), mode=E.ChannelMode.VIDEOWALL,
                                   resolution_width=1920, resolution_height=1080, canvas_width=1920, canvas_height=1080)
                node_rows.append(self.add(E.VideowallNode, videowall_id=wall.id, channel_id=channel.id,
                    position_index=position, row_index=0, column_index=position-1,
                    x=(position-1)*1920, y=0, width=1920, height=1080))
            root = Path(get_settings().MEDIA_ROOT).resolve()
            if root.is_relative_to(Path(__file__).resolve().parents[2]):
                raise RuntimeError("VW1 graph media must stay outside the checkout")
            root.mkdir(parents=True, exist_ok=True)
            directory = TemporaryDirectory(prefix="vw1-graph-", dir=root)
            self.addCleanup(directory.cleanup)
            def owned_file(label):
                # Deliberately synthetic bytes: these tests measure ownership,
                # FK integrity and mutation effects, never FFmpeg/codec validity.
                data = ("synthetic-vw1-graph-" + side + "-" + label).encode()
                path = Path(directory.name) / (label + ".mp4")
                path.write_bytes(data)
                checksum = hashlib.sha256(data).hexdigest()
                content = self.add(E.ContentItem, client_id=client.id, name="VW1 " + label,
                    type=E.ContentType.VIDEO, file_path="/media/" + path.relative_to(root).as_posix(),
                    metadata_json={"content_type": "video/mp4", "checksum": checksum, "size": len(data)})
                return content, checksum, len(data)
            master, source_sha, _ = owned_file("master")
            campaign = self.add(E.Campaign, client_id=client.id, name="VW1 graph " + side)
            item = self.add(E.CampaignSequenceItem, campaign_id=campaign.id, content_id=master.id,
                            item_type="content", sort_order=1, duration_seconds=1)
            geometry = dict(rows=1, columns=3, total_width=5760, total_height=1080,
                            panel_width=1920, panel_height=1080, tolerance_ms=250,
                            nodes=[dict(id=node.id, channel_id=node.channel_id, position_index=node.position_index,
                                row_index=0, column_index=node.position_index-1, crop_x=(node.position_index-1)*1920,
                                crop_y=0, crop_width=1920, crop_height=1080) for node in node_rows])
            program = dict(campaign_id=campaign.id, items=[dict(id=item.id, master_content_id=master.id,
                           type="video", sort_order=1, duration_ms=None)])
            publication = self.add(E.VideowallPublication, client_id=client.id, branch_id=branch.id,
                wall_id=wall.id, campaign_id=campaign.id, requested_by_user_id=requestor.id,
                program_snapshot_json=program, geometry_snapshot_json=geometry,
                geometry_signature=hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                expected_asset_count=3)
            assets = [self.add(E.VideowallPublicationAsset, publication_id=publication.id, node_id=node.id,
                      master_content_id=master.id, logical_item_id=item.id, position_index=node.position_index,
                      sort_order=1, width=1920, height=1080) for node in node_rows]
            group = dict(VideowallPublication=publication, VideowallPublicationAsset=assets[0], client=client,
                         wall=wall, branch=branch, requestor=requestor, master=master, campaign=campaign,
                         nodes=node_rows, assets=assets, derived=[], owned_file=owned_file, source_sha=source_sha)
            self.vw1_rows[side] = group
        publication = group["VideowallPublication"]
        if (ready or active) and publication.status == "PENDING":
            program = dict(publication.program_snapshot_json)
            program["items"] = [{**program["items"][0], "duration_ms": 1000}]
            publication.program_snapshot_json = program
            publication.status = "READY"
            publication.loop_duration_ms = 1000
            for asset in group["assets"]:
                content, checksum, size = group["owned_file"]("derived-" + str(asset.position_index))
                group["derived"].append(content)
                asset.derived_content_id = content.id
                asset.duration_ms, asset.mime_type, asset.checksum, asset.size_bytes = 1000, "video/mp4", checksum, size
                asset.probe_json = dict(fps_num=25, fps_den=1, frame_count=25, has_audio=False,
                                        keyframe_pts_ms=[0], source_sha256=group["source_sha"])
                asset.validated = True
        if active:
            publication.status = "ACTIVE"
            publication.loop_start_epoch_ms = 10000
            publication.activated_at = datetime.now(UTC)
            group["wall"].media_delivery = "node_slice"
            group["wall"].active_publication_id = publication.id
        self.db.commit()
        return group

    def actor(self, side, role):
        return self.actors[side if role == "client_admin" else "A", role]

    def scenario(self, edge, source_side, *, foreign):
        target_side = ("B" if source_side == "A" else "A") if foreign else source_side
        if edge.key in GROUP_RELATION_KEYS:
            published = edge.key in {"VideowallPlaybackGroup.active_publication_id", "VideowallPublication.playback_group_id"}
            group = (self._ensure_playback_group_publication(source_side) if published
                     else self._ensure_playback_group(source_side, with_member=edge.model == "VideowallPlaybackGroupMember"))
            source = group[edge.model]
            if foreign:
                target = (self._ensure_playback_group_publication(target_side)["VideowallPublication"]
                          if edge.target == "VideowallPublication" else self.ensure(edge.target, target_side))
            else:
                target = {"client_id": group["client"], "branch_id": group["branch"], "wall_id": group["wall"],
                          "campaign_id": group["campaign"], "node_id": group.get("node"),
                          "group_id": group["VideowallPlaybackGroup"],
                          "playback_group_id": group["VideowallPlaybackGroup"],
                          "active_publication_id": group.get("VideowallPublication")}[edge.field]
            setattr(source, edge.field, target.id)
            self.db.commit()
            self.db.expire_all()
            return source, target, target_side
        if edge.key in VW1_RELATION_KEYS:
            group = self._ensure_vw1(source_side, ready=edge.field == "derived_content_id",
                                     active=edge.key == "Videowall.active_publication_id")
            source = group["wall"] if edge.model == "Videowall" else group[edge.model]
            if edge.key == "Videowall.active_publication_id":
                target = self._ensure_vw1(target_side, active=True)["VideowallPublication"]
            elif not foreign:
                target = {"client_id": self.ensure("Client", source_side), "branch_id": group["branch"],
                          "wall_id": group["wall"], "campaign_id": group["campaign"],
                          "requested_by_user_id": group["requestor"], "publication_id": group["VideowallPublication"],
                          "node_id": group["nodes"][0], "master_content_id": group["master"],
                          "derived_content_id": group["derived"][0] if group["derived"] else None}[edge.field]
            else:
                target = self.ensure(edge.target, target_side)
            setattr(source, edge.field, target.id)
            self.db.commit()
            self.db.expire_all()
            return source, target, target_side
        source = self.ensure(edge.model, source_side)
        target = self.ensure(edge.target, target_side)
        if source is target:
            # Folder.parent is contextual, not a self-cycle.
            target = self.add(E.ContentFolder, client_id=self.ensure("Client", target_side).id, name="QA parent")
        if not foreign and edge.key == "KioskButton.target_screen_id":
            target = self.add(E.KioskScreen, client_id=self.ensure("Client", target_side).id,
                              name="QA target", slug="qa-target")
        if not foreign and edge.key == "DeviceOutput.channel_id":
            target = self.add(E.Channel, client_id=self.ensure("Client", target_side).id,
                              branch_id=self.ensure("Branch", target_side).id, name="QA secondary")
        if edge.key == "AudioAssignment.spot_playlist_id":
            target.kind = E.AudioPlaylistKind.SPOT
        if edge.key == "AudioAssignment.music_playlist_id":
            target.kind = E.AudioPlaylistKind.MUSIC
        if edge.key in {"TouchExperience.home_screen_id", "TouchExperience.attract_screen_id"} and not foreign:
            target.experience_id = source.id
        if edge.key == "KioskButton.target_screen_id" and not foreign:
            target.experience_id = self.ensure("KioskScreen", source_side).experience_id
        setattr(source, edge.field, target.id)
        self.db.commit()
        self.db.expire_all()
        return source, target, target_side

    def rejection(self, roots, actor=None, *, operation="delete_client"):
        with self.no_effects(), patch.object(Path, "unlink", side_effect=AssertionError("premature unlink")):
            with self.assertRaises(HTTPException) as caught:
                with mutations.delete_guard(self.db, roots, actor=actor, operation=operation):
                    self.fail("Corrupt delete approved")
            self.assertEqual((caught.exception.status_code, caught.exception.detail),
                             (409, "TENANT_RELATION_CONFLICT"))

    def check_edge(self, edge):
        if edge.key in VW1_RELATION_KEYS or edge.key in PROTECTED_GROUP_RELATION_KEYS:
            # VW1 has no release/GC operation. Test both intact and corrupt
            # graphs as protected; the original 87 policies below are unchanged.
            for side in ("A", "B"):
                for foreign in (False, True):
                    source, target, target_side = self.scenario(edge, side, foreign=foreign)
                    for role in ROLES:
                        with self.subTest(edge=edge.key, side=side, role=role, foreign=foreign):
                            self.rejection((target,), self.actor(target_side, role))
                            COVERAGE.append(dict(EDGE=edge.key, CASE=side + ("_foreign" if foreign else "_protected"),
                                ENGINE=self.engine.dialect.name, ROLE=role, RESULT="PASS", ZERO_MUTATION=True,
                                CLASSIFICATION=edge.delete_policy, DATABASE_EFFECT=edge.on_delete or "RESTRICT"))
                    self.doCleanups()
                    self.setUp()
            return
        for side in ("A", "B"):
            foreign = edge.delete_policy == "NEEDS_PREFLIGHT"
            source, target, target_side = self.scenario(edge, side, foreign=foreign)
            for role in ROLES:
                with self.subTest(edge=edge.key, side=side, role=role):
                    if foreign:
                        self.rejection((target,), self.actor(target_side, role))
                    else:
                        source_owner, target_owner = self.graph.resolve_owner(source), self.graph.resolve_owner(target)
                        self.assertEqual(source_owner.state, OwnerState.OWNED)
                        self.assertEqual(source_owner.client_id, target_owner.client_id)
                        with self.no_effects():
                            with mutations.delete_guard(self.db, (target,), actor=self.actor(target_side, role)) as approval:
                                self.assertIn(self.ref(source), approval.covered)
                                self.assertTrue(any(d.relation_key == edge.key and d.reference == self.ref(source)
                                                    for p in approval.plans for d in p.dependencies))
                    COVERAGE.append(dict(EDGE=edge.key, CASE=side + ("_foreign" if foreign else "_derived"),
                        ENGINE=self.engine.dialect.name, ROLE=role, RESULT="PASS", ZERO_MUTATION=True,
                        CLASSIFICATION=edge.delete_policy, DATABASE_EFFECT=edge.on_delete or "RESTRICT"))
                self.db.rollback()
            # Rebuild for the opposite direction; do not retain a corrupt first case.
            self.doCleanups()
            self.setUp()


class DeleteEdgeSQLiteTests(DeleteMatrixFixture):
    pass


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteEdgePostgresTests(DeleteMatrixFixture):
    def make_engine(self):
        url = make_url(os.environ["A7_TEST_DATABASE_URL"])
        if url.host not in {"localhost", "127.0.0.1"} or url.database != "tecno_control_retention_test":
            raise RuntimeError("Refusing nonlocal/non-QA PostgreSQL")
        schema = "a7_" + uuid.uuid4().hex
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
        with engine.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            with engine.begin() as db:
                db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            engine.dispose()
        self.addCleanup(cleanup)
        return engine


for _edge in RELATION_SPECS:
    def _test(self, edge=_edge):
        self.check_edge(edge)
    for _cls in (DeleteEdgeSQLiteTests, DeleteEdgePostgresTests):
        setattr(_cls, "test_edge_" + _edge.key.replace(".", "_"), _test)
del _cls


class DeleteExecutionTests(DeleteMatrixFixture):
    def test_indirect_cleanup_requires_approved_parent_plan(self):
        from app.api.routes import branches, channels
        channel = self.ensure("Channel", "A")
        branch = self.ensure("Branch", "A")
        self.db.commit()
        for function, row in ((branches.cleanup_branch_delete_dependencies, branch),
                              (channels.cleanup_channel_delete_dependencies, channel)):
            with self.no_effects(), self.assertRaises(HTTPException) as caught:
                function(self.db, row)
            self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_client_cleanup_proves_files_before_commit_and_preserves_unknown(self):
        from tempfile import TemporaryDirectory
        from app.core.config import get_settings
        from app.services.client_cleanup import delete_client_cascade
        with TemporaryDirectory(prefix="a7-media-proof-") as directory, \
                patch.object(get_settings(), "MEDIA_ROOT", Path(directory)):
            root = Path(directory)
            client = self.ensure("Client", "A")
            parent = root / client.id
            parent.mkdir()
            owned, unknown, ambiguous = (parent / name for name in ("owned.mp4", "unknown.mp4", "shared.mp4"))
            for path in (owned, unknown, ambiguous):
                path.write_bytes(b"synthetic")
            for side, path in (("A", owned), ("A", ambiguous), ("B", ambiguous)):
                self.add(E.ContentItem, client_id=self.ensure("Client", side).id, name="QA",
                         type=E.ContentType.VIDEO, file_path="/media/" + path.relative_to(root).as_posix())
            imp = self.ensure("DatasetImport", "A")
            import_path = root / "datasets" / client.id / "fixture.csv"
            import_path.parent.mkdir(parents=True)
            import_path.write_bytes(b"QA\n")
            imp.storage_path = import_path.relative_to(root).as_posix()
            self.db.commit()
            committed = []
            event.listen(self.db, "after_commit", lambda *_: committed.append(True))
            real_unlink = Path.unlink
            def checked_unlink(path, *args, **kwargs):
                self.assertTrue(committed, "Unlink before commit")
                return real_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", checked_unlink):
                delete_client_cascade(client, self.db)
            self.assertFalse(owned.exists())
            self.assertFalse(import_path.exists())
            self.assertEqual(unknown.read_bytes(), b"synthetic")
            self.assertEqual(ambiguous.read_bytes(), b"synthetic")

    def test_branch_handler_rejects_foreign_user_before_mutation(self):
        from app.api.routes.branches import delete_branch
        branch = self.ensure("Branch", "A")
        user = self.ensure("User", "B")
        user.branch_id = branch.id
        self.db.commit()
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            delete_branch(branch.id, db=self.db, current_user=self.actor("A", "super_admin"))
        self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_no_physical_cleanup_before_reject(self):
        from app.services.client_cleanup import delete_client_cascade
        branch = self.ensure("Branch", "A")
        user = self.ensure("User", "B")
        user.branch_id = branch.id
        self.db.commit()
        with self.no_effects(), patch.object(Path, "unlink", side_effect=AssertionError("Unlink before approval")), \
                self.assertRaises(HTTPException):
            delete_client_cascade(self.ensure("Client", "A"), self.db)

    def test_client_whole_graph_legitimate_delete(self):
        from app.services.client_cleanup import delete_client_cascade
        from app.models.base import Base
        for spec in (spec for spec in MODEL_SPECS if spec.name not in {"VideowallPublication", "VideowallPublicationAsset"}):
            self.ensure(spec.name, "A")
        self.db.commit()
        client = self.ensure("Client", "A")
        client_id = client.id
        delete_client_cascade(client, self.db)
        self.assertIsNone(self.db.get(E.Client, client_id))
        self.assertIsNotNone(self.db.get(E.Client, self.ensure("Client", "B").id))

    def test_client_foreign_set_null_is_rejected_before_bulk_delete(self):
        from app.services.client_cleanup import delete_client_cascade
        branch = self.ensure("Branch", "A")
        foreign_user = self.ensure("User", "B")
        foreign_user.branch_id = branch.id
        self.db.commit()
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            delete_client_cascade(self.ensure("Client", "A"), self.db)
        self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_global_principal_with_tenant_revision_cannot_be_deleted(self):
        user = self.actors["A", "super_admin"]
        revision = self.ensure("LayoutRevision", "B")
        revision.created_by_user_id = user.id
        self.db.commit()
        self.rejection((user,), self.actors["A", "super_admin"], operation="delete_team_user")

    def test_cli_late_corruption_rejects_entire_batch_before_delete_or_flush(self):
        from app.scripts import cleanup_unused_assets as cli
        from contextlib import nullcontext
        from types import SimpleNamespace
        from datetime import timedelta
        old = datetime.now(UTC) - timedelta(days=40)
        for index in range(3):
            self.add(E.Campaign, client_id=self.ensure("Client", "A").id, name="QA batch",
                     created_at=old + timedelta(seconds=index),
                     layout_id=self.ensure("Layout", "B").id if index == 2 else None)
        self.db.commit()
        with patch.object(cli, "SessionLocal", lambda: nullcontext(self.db)), \
                patch.object(cli, "parse_args", lambda: SimpleNamespace(apply=True, days=5)), \
                patch("sys.stdout", new=io.StringIO()), self.no_effects(), \
                patch.object(Path, "unlink", side_effect=AssertionError("early unlink")), \
                self.assertRaises(HTTPException) as caught:
            cli.main()
        self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")
        self.assertEqual(len(list(self.db.scalars(select(E.Campaign)))), 3)

    def test_cli_discovers_contents_after_campaigns_without_provisional_delete(self):
        from app.scripts import cleanup_unused_assets as cli
        from contextlib import nullcontext
        from types import SimpleNamespace
        from datetime import timedelta
        item = self.ensure("CampaignPlaylistItem", "A")
        campaign = self.ensure("Campaign", "A")
        content = self.ensure("ContentItem", "A")
        campaign.created_at = content.created_at = datetime.now(UTC) - timedelta(days=40)
        ids = campaign.id, content.id
        self.db.commit()
        with patch.object(cli, "SessionLocal", lambda: nullcontext(self.db)), \
                patch.object(cli, "parse_args", lambda: SimpleNamespace(apply=True, days=5)), \
                patch("sys.stdout", new=io.StringIO()):
            cli.main()
        self.assertIsNone(self.db.get(E.Campaign, ids[0]))
        self.assertIsNone(self.db.get(E.ContentItem, ids[1]))

    def test_retention_late_corruption_rejects_whole_batch(self):
        from app.services.cleanup_retention import run_retention_cleanup
        from datetime import timedelta
        for index in range(3):
            self.add(E.Campaign, client_id=self.ensure("Client", "A").id, name="QA batch",
                     created_at=datetime.now(UTC) - timedelta(days=40-index),
                     layout_id=self.ensure("Layout", "B").id if index == 2 else None)
        self.db.commit()
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            run_retention_cleanup(self.db, actor=self.actors["A", "super_admin"])
        self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")
        self.assertEqual(len(list(self.db.scalars(select(E.Campaign)))), 3)

    def test_delete_orphan_foreign_child_rejected_before_orm_delete(self):
        campaign = self.ensure("Campaign", "A")
        item = self.ensure("CampaignPlaylistItem", "A")
        item.content_id = self.ensure("ContentItem", "B").id
        self.db.commit()
        # Materialize the actual ORM delete-orphan collection before the guard.
        self.assertIn(item, campaign.playlist_items)
        self.rejection((campaign,), self.actor("A", "super_admin"), operation="delete_campaign")
        self.assertFalse(self.db.deleted)

    def test_non_fk_legacy_screen_reference_is_checked(self):
        button = self.ensure("KioskButton", "A")
        foreign = self.ensure("KioskScreen", "B")
        button.action_type = E.KioskActionType.SWITCH_SCREEN
        button.action_value = foreign.id
        self.db.commit()
        self.rejection((self.ensure("KioskScreen", "A"),), self.actor("A", "super_admin"),
                       operation="delete_touch_screen")

    def test_revalidation_detects_dependency_change(self):
        parent = self.ensure("Branch", "A")
        self.db.commit()
        real = mutations.preflight_relationship_graph
        calls = []
        from dataclasses import replace
        def changed(*args, **kwargs):
            plan = real(*args, **kwargs)
            calls.append(plan)
            return replace(plan, dependencies=()) if len(calls) == 2 else plan
        self.ensure("Channel", "A")
        self.db.commit()
        with patch.object(mutations, "preflight_relationship_graph", changed):
            self.rejection((parent,), self.actor("A", "super_admin"), operation="delete_branch")
        self.assertEqual(len(calls), 2)

    def test_approved_plan_does_not_cover_unrelated_nested_delete(self):
        first = self.ensure("Campaign", "A")
        second = self.ensure("ContentItem", "B")
        self.db.commit()
        with self.no_effects(), mutations.delete_guard(self.db, (first,)):
            with self.assertRaises(HTTPException):
                with mutations.delete_guard(self.db, (second,)):
                    self.fail("Nested scope expanded")

    def test_context_only_owner_is_not_an_approved_nested_delete(self):
        campaign = self.ensure("Campaign", "A")
        client = self.ensure("Client", "A")
        self.db.commit()
        with self.no_effects(), mutations.delete_guard(self.db, (campaign,)) as approval:
            self.assertTrue(any(self.ref(client) in plan.checked for plan in approval.plans))
            with self.assertRaises(HTTPException):
                with mutations.delete_guard(self.db, (client,)):
                    self.fail("Context-only ancestor became a delete target")


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteConcurrencyTests(DeleteExecutionTests):
    make_engine = DeleteEdgePostgresTests.make_engine

    def blocked_writer(self, action):
        from concurrent.futures import ThreadPoolExecutor
        from sqlalchemy.exc import OperationalError
        def writer():
            with Session(self.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    action(db)
                    db.commit()
                except OperationalError as exc:
                    self.assertEqual(exc.orig.sqlstate, "55P03")
                    db.rollback()
                    return "BLOCKED"
                self.fail("Concurrent writer bypassed the delete plan")
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(writer).result(timeout=8), "BLOCKED")

    def test_new_foreign_fk_after_preflight_is_blocked(self):
        parent = self.ensure("Branch", "A")
        foreign = self.ensure("Client", "B")
        self.db.commit()
        parent_id, foreign_id = parent.id, foreign.id
        with mutations.delete_guard(self.db, (parent,)):
            def insert(db):
                db.add(E.User(client_id=foreign_id, branch_id=parent_id, role="client_admin",
                              email="foreign@example.com", full_name="QA", password_hash="synthetic"))
                db.flush()
            self.blocked_writer(insert)
        self.db.rollback()

    def test_dependent_owner_change_after_preflight_is_blocked(self):
        child = self.ensure("Channel", "A")
        parent = self.ensure("Branch", "A")
        other_id = self.ensure("Client", "B").id
        self.db.commit()
        identity = child.id
        with mutations.delete_guard(self.db, (parent,)):
            self.blocked_writer(lambda db: db.execute(update(E.Channel).where(E.Channel.id == identity)
                                                       .values(client_id=other_id)))
        self.db.rollback()

    def test_row_cannot_disappear_between_preflight_and_execution(self):
        from sqlalchemy import delete
        parent = self.ensure("Campaign", "A")
        self.db.commit()
        identity = parent.id
        with mutations.delete_guard(self.db, (parent,)):
            self.blocked_writer(lambda db: db.execute(delete(E.Campaign).where(E.Campaign.id == identity)))
        self.db.rollback()

    def test_rollback_preserves_rows_and_releases_locks(self):
        parent = self.ensure("Campaign", "A")
        self.db.commit()
        identity = parent.id
        with mutations.delete_guard(self.db, (parent,)):
            self.db.delete(parent)
            self.db.flush()
            self.db.rollback()
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(E.Campaign, identity))
            db.execute(text("SET LOCAL lock_timeout = '250ms'"))
            db.execute(update(E.Campaign).where(E.Campaign.id == identity).values(name="after rollback"))
            db.commit()


class DeletePlanCoverageTests(DeleteMatrixFixture):
    def check_plan_edge(self, edge):
        source, target, side = self.scenario(edge, "A", foreign=False)
        if edge.key in VW1_RELATION_KEYS or edge.key in PROTECTED_GROUP_RELATION_KEYS:
            self.rejection((target,), self.actor(side, "super_admin"))
            return
        with self.no_effects(), mutations.delete_guard(self.db, (target,), actor=self.actor(side, "super_admin")) as plan:
            self.assertTrue(any(d.relation_key == edge.key and d.reference == self.ref(source)
                                for p in plan.plans for d in p.dependencies), edge.key)

    def check_database_effect(self, edge):
        from sqlalchemy import delete
        from sqlalchemy.exc import IntegrityError
        source, target, side = self.scenario(edge, "A", foreign=False)
        if edge.key in VW1_RELATION_KEYS or edge.key in PROTECTED_GROUP_RELATION_KEYS:
            # Real FK behavior is verified by migration 016 tests; no public
            # service may release a publication or its protected dependencies.
            self.rejection((target,), self.actor(side, "super_admin"))
            return
        source_id = source.id
        target_id = target.id
        with mutations.delete_guard(self.db, (target,), actor=self.actor(side, "super_admin")):
            if not edge.on_delete:
                with self.assertRaises(IntegrityError):
                    self.db.execute(delete(type(target)).where(type(target).id == target.id))
                self.db.rollback()
                return
            if edge.on_delete == "RESTRICT":
                with self.assertRaises(IntegrityError):
                    self.db.execute(delete(type(target)).where(type(target).id == target_id))
                self.db.rollback()
                surviving = self.db.get(type(source), source_id)
                self.assertIsNotNone(surviving, edge.key)
                self.assertIsNotNone(self.db.get(type(target), target_id), edge.key)
                self.assertEqual(getattr(surviving, edge.field), target_id, edge.key)
                return
            self.db.execute(delete(type(target)).where(type(target).id == target.id))
            self.db.flush()
            self.db.expire_all()
            surviving = self.db.get(type(source), source_id)
            if edge.on_delete == "CASCADE":
                self.assertIsNone(surviving, edge.key)
            else:
                self.assertIsNotNone(surviving, edge.key)
                self.assertIsNone(getattr(surviving, edge.field), edge.key)
            self.db.rollback()


for _edge in RELATION_SPECS:
    def _test_plan(self, edge=_edge):
        self.check_plan_edge(edge)
    setattr(DeletePlanCoverageTests, "test_effect_" + _edge.key.replace(".", "_"), _test_plan)


class DeleteDatabaseSQLiteTests(DeleteMatrixFixture):
    check_database_effect = DeletePlanCoverageTests.check_database_effect


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteDatabasePostgresTests(DeleteDatabaseSQLiteTests):
    make_engine = DeleteEdgePostgresTests.make_engine


for _edge in RELATION_SPECS:
    def _test_database(self, edge=_edge):
        self.check_database_effect(edge)
    setattr(DeleteDatabaseSQLiteTests, "test_database_" + _edge.key.replace(".", "_"), _test_database)


def _recompile_delete_plan(replacements=()):
    """Keep live module globals; only the new function name lives in locals."""
    import inspect as source_inspect
    source = source_inspect.getsource(mutations._locked_delete_plan)
    for old, new in replacements:
        if source.count(old) != 1:
            raise AssertionError("Mutation source anchor must occur exactly once: " + old)
        source = source.replace(old, new)
    namespace = {}
    exec(compile(source, "<A7 in-memory plan>", "exec"), vars(mutations), namespace)
    return namespace["_locked_delete_plan"]


def _assertion_sites(function, *anchors):
    """Identify the intended assertion, not any failure in the same test."""
    import inspect as source_inspect
    function = source_inspect.unwrap(function)
    lines, first = source_inspect.getsourcelines(function)
    sites = set()
    for anchor in anchors:
        matches = [first + index for index, line in enumerate(lines) if anchor in line]
        if len(matches) != 1:
            raise AssertionError("Defense assertion anchor must occur once: " + anchor)
        sites.add((function.__code__.co_filename, matches[0]))
    return frozenset(sites)


class VW1MutationDefenseTests(DeleteMatrixFixture):
    """Specific scope/state defenses, independent of media codec QA."""

    def _typed_rejection(self, row, marker):
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            intent = mutations.MutationIntent("UPDATE", self.ref(row), ())
            with mutations.write_guard(self.db, actor=self.actor("A", "super_admin"),
                    client_id=self.ensure("Client", "A").id,
                    operation="prepare_videowall_publication", intents=(intent,)):
                self.fail(marker)
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (409, "TENANT_RELATION_CONFLICT"))

    def _typed_row_rejection(self, row, changes, marker):
        from app.services import videowall_integrity as integrity
        from app.services.tenant_ownership import context_for_actor
        graph = mutations._WriteGraph(self.db)
        graph.media_validation_locked = True
        context = context_for_actor(self.db, self.actor("A", "super_admin"),
                                    client_id=self.ensure("Client", "A").id)
        ref = self.ref(row)
        original = dict(graph.snapshot(ref))
        changed = {**original, **changes}
        validator = (integrity.publication_semantics if ref.model == "VideowallPublication"
                     else integrity.asset_semantics)
        with self.no_effects():
            self.assertTrue(validator(graph, ref, context, original), "VW1 intact typed fixture must remain valid")
            self.assertFalse(validator(graph, ref, context, changed), marker)

    def test_protected_publication_delete(self):
        client = self._ensure_vw1("A")["client"]
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            with mutations.delete_guard(self.db, (client,), actor=self.actor("A", "super_admin")):
                self.fail("VW1 protected publication delete was approved")
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (409, "TENANT_RELATION_CONFLICT"))

    def test_geometry_signature_is_authoritative(self):
        publication = self._ensure_vw1("A")["VideowallPublication"]
        self.corrupt(publication, geometry_signature="f" * 64)
        self._typed_rejection(publication, "VW1 mismatched geometry signature was accepted")

    def test_expected_asset_count_matches_program(self):
        publication = self._ensure_vw1("A")["VideowallPublication"]
        self._typed_row_rejection(publication, {"expected_asset_count": 6}, "VW1 incomplete asset cardinality was accepted")

    def test_asset_position_matches_frozen_node(self):
        asset = self._ensure_vw1("A")["assets"][0]
        self._typed_row_rejection(asset, {"position_index": 2}, "VW1 wrong frozen node position was accepted")

    def test_derived_content_cannot_be_master(self):
        group = self._ensure_vw1("A", ready=True)
        asset = group["assets"][0]
        self._typed_row_rejection(asset, {"derived_content_id": group["master"].id},
                                  "VW1 master was accepted as a playable derived slice")

    def test_secondary_node_cannot_retain_audio(self):
        asset = self._ensure_vw1("A", ready=True)["assets"][1]
        self._typed_row_rejection(asset, {"probe_json": {**asset.probe_json, "has_audio": True}},
                                  "VW1 secondary node audio was accepted")


def run_vw1_mutations():
    """Six source-bound VW1 defects, each with passing intact controls."""
    import inspect as source_inspect
    from types import CodeType, FunctionType
    from app.services import videowall_integrity as integrity

    cases = (
        ("vw1_delete_protection", mutations, "delete_guard",
         'ref.model in {"VideowallPublication", "VideowallPublicationAsset"}', "False",
         "test_protected_publication_delete"),
        ("vw1_geometry_signature", integrity, "publication_semantics",
         'geometry_signature(geometry) != row["geometry_signature"]', "False",
         "test_geometry_signature_is_authoritative"),
        ("vw1_asset_cardinality", integrity, "publication_semantics",
         'row["expected_asset_count"] != len(program["items"]) * len(geometry["nodes"])', "False",
         "test_expected_asset_count_matches_program"),
        ("vw1_frozen_node_position", integrity, "asset_semantics",
         'row["position_index"] != node["position_index"]', "False",
         "test_asset_position_matches_frozen_node"),
        ("vw1_master_as_derived", integrity, "asset_semantics",
         'row["derived_content_id"] == row["master_content_id"]', "False",
         "test_derived_content_cannot_be_master"),
        ("vw1_secondary_audio", integrity, "asset_semantics",
         '(row["position_index"] != 1 and probe["has_audio"])', "False",
         "test_secondary_node_cannot_retain_audio"),
    )
    results = []
    for name, owner, attr, before, after, method in cases:
        original = getattr(owner, attr)
        unwrapped = source_inspect.unwrap(original)
        source = source_inspect.getsource(original)
        edits = [(before, after)]
        if name == "vw1_asset_cardinality":
            # Both cardinality comparisons defend the same expected-count
            # invariant; omitting only one leaves a redundant defense intact.
            edits.append(('len(assets) != row["expected_asset_count"]', "False"))
        altered = source
        for old, new in edits:
            if altered.count(old) != 1:
                raise AssertionError("VW1 mutation target is not unique: " + name)
            altered = altered.replace(old, new)
        # Compile the complete lexical module without executing it. Compiling
        # an isolated function can change optimized constants/code structure,
        # which must never be mistaken for the intact source control.
        path = Path(unwrapped.__code__.co_filename)
        module_source = path.read_text(encoding="utf-8")
        if module_source.count(source) != 1:
            raise AssertionError("VW1 source function is not unique: " + name)
        module_code = compile(module_source.replace(source, altered), str(path), "exec", dont_inherit=True)
        def codes(code):
            yield code
            for constant in code.co_consts:
                if isinstance(constant, CodeType):
                    yield from codes(constant)
        targets = [code for code in codes(module_code)
                   if code.co_qualname == unwrapped.__code__.co_qualname
                   and code.co_firstlineno == unwrapped.__code__.co_firstlineno]
        if len(targets) != 1:
            raise AssertionError("VW1 compiled mutation target is not unique: " + name)
        changed = FunctionType(targets[0], unwrapped.__globals__, unwrapped.__name__,
                               unwrapped.__defaults__, unwrapped.__closure__)
        changed.__kwdefaults__ = unwrapped.__kwdefaults__
        if original is not unwrapped:
            if original.__wrapped__ is not unwrapped or attr != "delete_guard":
                raise AssertionError("Unreviewed VW1 mutation decorator: " + name)
            changed = contextmanager(changed)
        defense = (_assertion_sites(VW1MutationDefenseTests.test_protected_publication_delete,
                                   'self.fail("VW1 protected publication delete was approved")')
                   if name == "vw1_delete_protection" else
                   _assertion_sites(VW1MutationDefenseTests._typed_rejection, "self.fail(marker)")
                   if name == "vw1_geometry_signature" else
                   _assertion_sites(VW1MutationDefenseTests._typed_row_rejection, "self.assertFalse(validator("))
        results.append(_run_delete_mutation(name, owner, attr, changed,
            VW1MutationDefenseTests, method, defense, after_setup=True))
    return results


class _MutationProbe(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.assertion_sites = []

    def _record_assertion(self, err):
        # The deepest test frame identifies the actual failed check. An outer
        # test frame alone could also occur in setup/observer/fixture failures.
        tb, site = err[2], None
        test_root = Path(__file__).parent
        while tb:
            if Path(tb.tb_frame.f_code.co_filename).parent == test_root:
                site = (tb.tb_frame.f_code.co_filename, tb.tb_lineno)
            tb = tb.tb_next
        self.assertion_sites.append(site)

    def addFailure(self, test, err):
        self._record_assertion(err)
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None and issubclass(err[0], test.failureException):
            self._record_assertion(err)
        super().addSubTest(test, subtest, err)

    def evidence(self):
        passed = (self.testsRun == 1 and self.wasSuccessful()
                  and not self.skipped and not self.expectedFailures)
        return dict(status="PASS" if passed else "NOT_PASS", tests_run=self.testsRun,
                    failures=len(self.failures), errors=len(self.errors), skipped=len(self.skipped),
                    assertion_sites=self.assertion_sites,
                    failure_trace=[trace for _, trace in self.failures],
                    error_trace=[trace for _, trace in self.errors])


# A7-R3: evidence is gathered before C, independently of its assertion result.
_PLAN_ORIGINAL = mutations._locked_delete_plan
_PLAN_EDITS = {
    "stale_branch_plan_reused": (
        ("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),
        ("rediscovered = _effect_roots(graph, initial, operation)", "rediscovered = expanded"),
    ),
    "rediscovery_omitted_after_lock": (
        ("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),
    ),
    "revalidation_omitted": (
        ('confirmed = preflight_relationship_graph(gate, tuple(MutationIntent("DELETE", ref) for ref in rediscovered),\n'
         '                                                      discovery_spec, semantic_adapters=_delete_adapters())',
         "confirmed = plan"),
    ),
}


def _code_structure(code):
    from types import CodeType
    def constant(value):
        if isinstance(value, CodeType):
            return _code_structure(value)
        if isinstance(value, frozenset):
            # Compiler constant folding can create equal sets with different
            # iteration order. Preserve type and every member, not repr order.
            return ("frozenset", tuple(sorted((constant(v) for v in value), key=repr)))
        if isinstance(value, tuple):
            return ("tuple", tuple(constant(v) for v in value))
        return (type(value).__name__, repr(value))

    return (code.co_code.hex(), code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_flags, code.co_stacksize,
            code.co_exceptiontable.hex(),
            tuple(constant(c) for c in code.co_consts))


def _mutation_fingerprint(value):
    from hashlib import sha256
    from types import FunctionType
    if isinstance(value, FunctionType):
        payload = (_code_structure(value.__code__), value.__defaults__, value.__kwdefaults__,
                   tuple(_mutation_fingerprint(c.cell_contents) for c in (value.__closure__ or ())))
    else:
        payload = value
    return sha256(repr(payload).encode("utf-8")).hexdigest()


def _intact_control(value):
    from types import CodeType, FunctionType
    if not isinstance(value, FunctionType):
        return tuple(list(value)) if isinstance(value, tuple) else value
    # Compile the complete module without executing it: this retains lexical
    # context, decorators' wrapper code and closure layout, but cannot start
    # an application or create a second collaborator/observer namespace.
    source = Path(value.__code__.co_filename)
    module_code = compile(source.read_bytes(), str(source), "exec", dont_inherit=True)

    def codes(code):
        yield code
        for constant in code.co_consts:
            if isinstance(constant, CodeType):
                yield from codes(constant)

    candidates = [code for code in codes(module_code)
                  if code.co_qualname == value.__code__.co_qualname
                  and code.co_firstlineno == value.__code__.co_firstlineno]
    if len(candidates) != 1:
        raise ValueError("Intact source target must resolve exactly once")
    clone = FunctionType(candidates[0], value.__globals__, value.__name__,
                         value.__defaults__, value.__closure__)
    clone.__kwdefaults__ = value.__kwdefaults__
    clone.__dict__.update(value.__dict__)
    return clone


def _required_globals(function):
    import dis
    from types import CodeType
    def names(code):
        result = {i.argval for i in dis.get_instructions(code)
                  if i.opname in {"LOAD_GLOBAL", "LOAD_NAME"}}
        for constant in code.co_consts:
            if isinstance(constant, CodeType):
                result.update(names(constant))
        return result
    return names(function.__code__)


_PLAN_BINDINGS = {key: vars(mutations)[key] for key in _required_globals(_PLAN_ORIGINAL)
                  if key in vars(mutations)}
_HEALTH_MISSING = object()


def _harness_snapshot(original, intact, replacement, cls, method):
    import inspect as source_inspect
    import sys
    from types import FunctionType
    bindings, issues = [], []
    for label, value in (("original", original), ("intact", intact), ("replacement", replacement)):
        function = source_inspect.unwrap(value) if isinstance(value, FunctionType) else value
        if not isinstance(function, FunctionType):
            continue
        owner = sys.modules.get(function.__module__)
        if owner is None or function.__globals__ is not vars(owner):
            issues.append(label + ": disconnected module globals")
        builtins = function.__builtins__
        for key in sorted(_required_globals(function)):
            if key not in function.__globals__ and key in builtins:
                continue
            expected = function.__globals__.get(key, _HEALTH_MISSING)
            if expected is _HEALTH_MISSING:
                issues.append(label + ": missing global " + key)
            bindings.append((label + "." + key, function.__globals__, key, expected))
    # These are the actual instrumentation entry points; retain identities across
    # A/B so a leaked patch cannot become the expected baseline for C.
    hooks = ((cls, "setUp"), (cls, method), (_MutationProbe, "addFailure"),
             (_MutationProbe, "addSubTest"), (_MutationProbe, "evidence"), (patch, "object"))
    return bindings, [(owner, key, getattr(owner, key)) for owner, key in hooks], issues


def _harness_health(snapshot, original, intact, replacement, *, plan=False):
    bindings, hooks, initial_issues = snapshot
    issues = list(initial_issues)
    evidence = []
    for label, namespace, key, expected in bindings:
        actual = namespace.get(key, _HEALTH_MISSING)
        ok = actual is expected and actual is not _HEALTH_MISSING
        evidence.append(dict(binding=label, expected=id(expected), actual=id(actual), matches=ok))
        if not ok:
            issues.append(label + ": required binding changed")
    for owner, key, expected in hooks:
        if getattr(owner, key, None) is not expected:
            issues.append("instrumentation hook changed: " + key)
    sentinel_checks = []
    if plan:
        functions = (original, intact, replacement)
        for key, expected in _PLAN_BINDINGS.items():
            if vars(mutations).get(key, _HEALTH_MISSING) is not expected:
                issues.append("plan collaborator changed/missing: " + key)
        if any(getattr(fn, "__globals__", None) is not vars(mutations) for fn in functions):
            issues.append("plan observer globals disconnected")
        else:
            for key in ("_effect_roots", "_lock_delete_anchors", "preflight_relationship_graph"):
                sentinel = object()
                with patch.object(mutations, key, sentinel):
                    ok = all(fn.__globals__.get(key) is sentinel for fn in functions)
                sentinel_checks.append(dict(collaborator=key, sentinel_visible=ok))
                if not ok:
                    issues.append("patched collaborator not visible: " + key)
    return dict(status="PASS" if not issues else "FAIL", issues=issues,
                bindings=evidence, sentinel_checks=sentinel_checks,
                instrumentation_hooks=[key for _, key, _ in hooks])


def _mutation_provenance(name, target, attr, original, intact, replacement):
    import ast
    import inspect as source_inspect
    from hashlib import sha256
    before, after = _mutation_fingerprint(original), _mutation_fingerprint(replacement)
    evidence = dict(MUTATION_TARGET=getattr(target, "__name__", type(target).__name__) + "." + attr + ":" + name,
                    BEFORE_FINGERPRINT=before, AFTER_FINGERPRINT=after, MUTATION_APPLIED="NO",
                    checked_before_test=True)
    if _mutation_fingerprint(intact) != before:
        evidence["reason"] = "intact control code/data differs"
        return evidence
    if before == after:
        evidence["reason"] = "mutation target unchanged"
        return evidence
    if target is mutations and attr == "_locked_delete_plan":
        if name not in _PLAN_EDITS:
            evidence["reason"] = "mutation target not registered"
            return evidence
        source = source_inspect.getsource(_PLAN_ORIGINAL)
        altered = source
        for old, new in _PLAN_EDITS[name]:
            if altered.count(old) != 1:
                evidence["reason"] = "mutation target not found exactly once"
                return evidence
            altered = altered.replace(old, new)
        namespace = {}
        exec(compile(altered, "<independent registered mutation identity>", "exec"),
             vars(mutations), namespace)
        expected = _mutation_fingerprint(namespace["_locked_delete_plan"])
        evidence.update(
            BEFORE_AST=sha256(ast.dump(ast.parse(source)).encode()).hexdigest(),
            AFTER_AST=sha256(ast.dump(ast.parse(altered)).encode()).hexdigest(),
            EXPECTED_AFTER_FINGERPRINT=expected)
        if after != expected:
            evidence["reason"] = "replacement does not match registered target edit"
            return evidence
    evidence["MUTATION_APPLIED"] = "YES"
    return evidence

def _run_delete_mutation(name, target, attr, replacement, cls, method, defense_sites,
                         *, recompiled_control=None, code_target=False, extra_mutant_patches=(),
                         after_setup=False):
    @contextmanager
    def install_code(value):
        previous = target.__code__
        try:
            target.__code__ = value.__code__
            yield
        finally:
            target.__code__ = previous

    def probe(value, install, mutant_mode=False):
        result = _MutationProbe()
        instance = cls(method)
        original_method = getattr(instance, method)

        @contextmanager
        def installed():
            with ExitStack() as stack:
                stack.enter_context(install_code(value) if code_target else patch.object(target, attr, value))
                if mutant_mode:
                    for owner, key, changed in extra_mutant_patches:
                        stack.enter_context(patch.object(owner, key, changed))
                yield
        if install and after_setup:
            def instrumented_method():
                with installed():
                    original_method()
            setattr(instance, method, instrumented_method)
            instance.run(result)
        elif install:
            with installed():
                instance.run(result)
        else:
            instance.run(result)
        return result

    if not hasattr(target, attr) or replacement is None:
        return dict(mutation=name, detected=False, mutation_result="INVALID_MUTATION",
                    invalid_mutations=1, non_discriminating=1, harness_errors=0, errors=0, skipped=0,
                    mutation_provenance={"MUTATION_APPLIED": "NO", "reason": "target/replacement missing"})
    baseline = target if code_target else getattr(target, attr)
    control = recompiled_control if recompiled_control is not None else _intact_control(baseline)
    snapshot = _harness_snapshot(baseline, control, replacement, cls, method)
    is_plan = target is mutations and attr == "_locked_delete_plan"
    health_before = _harness_health(snapshot, baseline, control, replacement, plan=is_plan)
    provenance = _mutation_provenance(name, target, attr, baseline, control, replacement)
    companions = []
    for owner, key, changed in extra_mutant_patches:
        before = getattr(owner, key, _HEALTH_MISSING)
        companions.append(dict(target=getattr(owner, "__name__", type(owner).__name__) + "." + key,
                               before=_mutation_fingerprint(before), after=_mutation_fingerprint(changed)))
        if before is _HEALTH_MISSING or _mutation_fingerprint(before) == _mutation_fingerprint(changed):
            provenance.update(MUTATION_APPLIED="NO", reason="companion target missing/unchanged")
    provenance["companion_targets"] = companions
    original = probe(None, False)
    intact = probe(control, True)
    controls = [original, intact]
    controls_pass = all(r.evidence()["status"] == "PASS" for r in controls)
    health = _harness_health(snapshot, baseline, control, replacement, plan=is_plan)
    healthy = health_before["status"] == health["status"] == "PASS"
    applied = provenance["MUTATION_APPLIED"] == "YES"
    mutant = probe(replacement, True, True) if controls_pass and healthy and applied else None
    health_after = _harness_health(snapshot, baseline, control, replacement, plan=is_plan)
    healthy = healthy and health_after["status"] == "PASS"
    expected_failure = (mutant is not None and mutant.testsRun == 1 and bool(mutant.failures)
                        and not mutant.errors and not mutant.skipped and not mutant.expectedFailures
                        and not mutant.unexpectedSuccesses
                        and len(mutant.assertion_sites) == len(mutant.failures)
                        and all(site in defense_sites for site in mutant.assertion_sites))
    if not controls_pass or not healthy:
        status = "INVALID_HARNESS"
    elif not applied:
        status = "INVALID_MUTATION"
    elif expected_failure:
        status = "DETECTED"
    elif mutant.evidence()["status"] == "PASS":
        status = "SURVIVED"
    else:
        status = "INVALID_HARNESS"
    all_runs = controls + ([mutant] if mutant is not None else [])
    return dict(mutation=name, detected=status == "DETECTED", mutation_result=status,
                non_discriminating=int(status != "DETECTED"),
                harness_errors=int(status == "INVALID_HARNESS"),
                errors=sum(len(r.errors) for r in all_runs),
                skipped=sum(len(r.skipped) for r in all_runs),
                harness_health=health, harness_health_before_controls=health_before,
                harness_health_after_mutant=health_after,
                mutation_provenance=provenance,
                invalid_mutations=int(status == "INVALID_MUTATION"),
                control_original=original.evidence(),
                control_recompiled=intact.evidence() if intact is not None else None,
                mutant=mutant.evidence() if mutant is not None else None,
                expected_defense_sites=sorted(defense_sites),
                failure_trace=mutant.evidence()["failure_trace"] if mutant is not None else [],
                error_trace=[trace for r in all_runs for _, trace in r.errors])


def _a7_defense_sites(name, cls, method):
    if name.startswith("omit_delete_edge:"):
        return _assertion_sites(DeletePlanCoverageTests.check_plan_edge, "self.assertTrue(any(")
    if name in {"foreign_set_null", "foreign_cascade", "foreign_delete_orphan",
                "global_bypass", "revalidation_omitted"}:
        return _assertion_sites(DeleteMatrixFixture.rejection, 'self.fail("Corrupt delete approved")')
    if name in {"branch_without_preflight", "client_without_preflight", "cli_late_preflight",
                "cleanup_unapproved_target", "post_gate_execution_expansion"}:
        return _assertion_sites(GraphFixture.no_effects, "self.assertEqual(effects, [])")
    if name == "child_checked_after_delete":
        return _assertion_sites(GraphFixture.no_effects, "self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)")
    if name == "context_ancestor_as_delete_target":
        return _assertion_sites(getattr(cls, method), 'self.fail("Context-only ancestor became a delete target")')
    if name in {"stale_branch_plan_reused", "rediscovery_omitted_after_lock"}:
        return _assertion_sites(getattr(cls, method), 'self.assertEqual(phases, ["DISCOVER"')
    if name == "lock_anchor_removed":
        return _assertion_sites(DeleteConcurrencyTests.blocked_writer, 'self.fail("Concurrent writer bypassed the delete plan")')
    if name in {"last_admin_no_lock", "admin_count_before_lock", "admin_lock_released_before_decision"}:
        return _assertion_sites(DeleteR1ConcurrencyTests.concurrent_admin_deletes,
                                "self.assertEqual(count, 1,", "self.assertTrue(blocked,")
    if name in {"device_output_false_orphan", "button_target_false_orphan", "orm_mapped_to_wrong_fk"}:
        return _assertion_sites(getattr(cls, method), "self.assertEqual(mismatches, [])")
    if name == "orphan_inventory_incorrect_45":
        return _assertion_sites(getattr(cls, method), "self.assertEqual(registry.inspect_orm_delete_orphans()")
    raise AssertionError("No specific defense assertion registered for " + name)


def run_a7_mutations():
    """A kill requires a passing control and the specific defense assertion."""
    results = []
    for edge in RELATION_SPECS:
        if edge.key in VW1_RELATION_KEYS or edge.key in GROUP_RELATION_KEYS:
            # Keep the original 87 approval-plan mutants intact. VW1's
            # protected-until-release contract has its own targeted mutants.
            continue
        name, method = "omit_delete_edge:" + edge.key, "test_effect_" + edge.key.replace(".", "_")
        results.append(_run_delete_mutation(
            name, mutations, "RELATION_SPECS", tuple(e for e in RELATION_SPECS if e.key != edge.key),
            DeletePlanCoverageTests, method, _a7_defense_sites(name, DeletePlanCoverageTests, method)))

    @contextmanager
    def bypass(*args, **kwargs):
        yield None

    from app.services import client_cleanup
    from app.api.routes import branches, channels
    from dataclasses import replace
    import inspect as source_inspect
    source = source_inspect.getsource(mutations._locked_delete_plan)
    needle = "confirmed = preflight_relationship_graph(gate, tuple(MutationIntent(\"DELETE\", ref) for ref in rediscovered),\n                                                      discovery_spec, semantic_adapters=_delete_adapters())"
    assert source.count(needle) == 1
    stale_guard = _recompile_delete_plan(((needle, "confirmed = plan"),))
    intact_plan = _recompile_delete_plan()
    @contextmanager
    def branch_bypass(*args, **kwargs):
        with patch.object(branches, "require_delete_coverage", lambda *a, **k: None), \
                patch.object(channels, "require_delete_coverage", lambda *a, **k: None):
            yield None
    cases = [
        ("branch_without_preflight", branches, "delete_guard", branch_bypass,
         DeleteExecutionTests, "test_branch_handler_rejects_foreign_user_before_mutation"),
        ("client_without_preflight", client_cleanup, "delete_guard", bypass,
         DeleteExecutionTests, "test_client_foreign_set_null_is_rejected_before_bulk_delete"),
        ("foreign_set_null", mutations, "delete_guard", bypass,
         DeleteEdgeSQLiteTests, "test_edge_User_branch_id"),
        ("foreign_cascade", mutations, "delete_guard", bypass,
         DeleteEdgeSQLiteTests, "test_edge_AudioAssignment_branch_id"),
        ("foreign_delete_orphan", mutations, "delete_guard", bypass,
         DeleteExecutionTests, "test_delete_orphan_foreign_child_rejected_before_orm_delete"),
        ("global_bypass", mutations, "delete_guard", bypass,
         DeleteExecutionTests, "test_global_principal_with_tenant_revision_cannot_be_deleted"),
        ("revalidation_omitted", mutations, "_locked_delete_plan", stale_guard,
         DeleteExecutionTests, "test_revalidation_detects_dependency_change"),
    ]
    from app.scripts import cleanup_unused_assets as cli
    cases.append(("cli_late_preflight", cli, "delete_guard", bypass,
                  DeleteExecutionTests, "test_cli_late_corruption_rejects_entire_batch_before_delete_or_flush"))
    @contextmanager
    def early_unlink(*args, **kwargs):
        Path("synthetic-should-never-be-unlinked").unlink()
        yield None
    cases.append(("unlink_before_approval", client_cleanup, "delete_guard", early_unlink,
                  DeleteExecutionTests, "test_no_physical_cleanup_before_reject"))
    @contextmanager
    def early_delete(db, rows, **kwargs):
        db.delete(tuple(rows)[0])
        with mutations.delete_guard(db, rows, **kwargs) as plan:
            yield plan
    cases.append(("child_checked_after_delete", client_cleanup, "delete_guard", early_delete,
                  DeleteExecutionTests, "test_client_foreign_set_null_is_rejected_before_bulk_delete"))
    original_coverage = mutations.require_delete_coverage
    def context_as_effect(db, rows):
        approval = db.info[mutations._DELETE_STATE]
        checked = {ref for plan in approval.plans for ref in plan.checked}
        if all(TenantGraph(db).reference(row) in checked for row in rows):
            return approval
        return original_coverage(db, rows)
    cases.append(("context_ancestor_as_delete_target", mutations, "require_delete_coverage", context_as_effect,
                  DeleteExecutionTests, "test_context_only_owner_is_not_an_approved_nested_delete"))
    for name, target, attr, replacement, cls, test in cases:
        sites = (_assertion_sites(early_unlink, 'Path("synthetic-should-never-be-unlinked").unlink()')
                 if name == "unlink_before_approval" else _a7_defense_sites(name, cls, test))
        results.append(_run_delete_mutation(
            name, target, attr, replacement, cls, test, sites,
            recompiled_control=intact_plan if name == "revalidation_omitted" else None))
    return results


def run_previous_mutations_with_a7_boundary(*, only=None):
    """Replay the 59 historical faults without weakening their test assertions.

    A7/A8 add independent guards around old functions. To reintroduce each old
    vulnerability, the compound fault must also remove those guards and the
    physical proof. Only temporary Python objects change; source files do not.
    Source indentation is restored before the historical textual substitutions.
    """
    import inspect as source_inspect
    import ast
    import sys
    from contextlib import ExitStack
    from test_rbac_matrix import run_phase_a_mutations
    from app.services import content_cleanup, media_references
    real_source = source_inspect.getsource

    def before_a7_wrapper(fn):
        source = real_source(fn)
        tree = ast.parse(source)
        regions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "delete_guard" for item in node.items):
                regions.append((node.lineno, node.end_lineno))
        lines = source.splitlines(keepends=True)
        for start, end in sorted(regions, reverse=True):
            lines[start - 1:end] = [line[4:] if line.startswith("    ") else line
                                   for line in lines[start:end]]
        return "".join(lines)

    @contextmanager
    def no_a7_guard(*args, **kwargs):
        yield None

    original_guard = mutations.delete_guard
    original_write_guard = mutations.write_guard
    original_player_guard = mutations.player_write_guard
    original_coverage = mutations.require_delete_coverage
    with ExitStack() as stack:
        stack.enter_context(patch.object(source_inspect, "getsource", before_a7_wrapper))
        # A9 adds independent SQL/projection/snapshot barriers. Historical fault
        # replay removes those additional barriers only in this temporary QA
        # context; A9's own catalog separately tests the new defenses in place.
        # Never rewrite a closure's __code__ as though it were its route body.
        from app.services import tenant_queries, tenant_responses
        from app.api.router import api_router
        from sqlalchemy import true
        original_readable = tenant_queries.readable_query
        for route in api_router.routes:
            endpoint = getattr(route, "endpoint", None)
            if getattr(endpoint, "__wrapped__", None) is not None:
                inner = source_inspect.unwrap(endpoint)
                stack.enter_context(patch.object(route, "endpoint", inner))
                stack.enter_context(patch.object(route.dependant, "call", inner))
        stack.enter_context(patch.object(tenant_queries, "read_integrity_predicate", lambda *a, **k: true()))
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith("app."):
                if getattr(module, "readable_query", None) is original_readable:
                    stack.enter_context(patch.object(module, "readable_query", lambda statement, *a, **k: statement))
                for key, value in list(vars(module).items()):
                    if (getattr(value, "__wrapped__", None) is not None and
                            getattr(getattr(value, "__code__", None), "co_filename", None) == tenant_responses.__file__):
                        stack.enter_context(patch.object(module, key, source_inspect.unwrap(value)))
            if module_name.startswith("app.") and getattr(module, "delete_guard", None) is original_guard:
                stack.enter_context(patch.object(module, "delete_guard", no_a7_guard))
            if module_name.startswith("app.") and getattr(module, "require_delete_coverage", None) is original_coverage:
                stack.enter_context(patch.object(module, "require_delete_coverage", lambda *a, **k: None))
            if module_name.startswith("app.") and getattr(module, "write_guard", None) is original_write_guard:
                stack.enter_context(patch.object(module, "write_guard", no_a7_guard))
            if module_name.startswith("app.") and getattr(module, "player_write_guard", None) is original_player_guard:
                stack.enter_context(patch.object(module, "player_write_guard", no_a7_guard))
        stack.enter_context(patch.object(content_cleanup, "approve_media_cleanup", lambda *a, **k: []))
        stack.enter_context(patch.object(content_cleanup, "approved_media_cleanup_paths",
                                        lambda db: frozenset(db.info[media_references._STATE]["paths"])))
        return run_phase_a_mutations(only=only)


from test_tenant_isolation import HistoricalRelationFixture, LocalPostgresFixture


class DeleteHTTPBoundaryTests(HistoricalRelationFixture):
    def delete_case(self, name):
        from app.services.tenant_relations import OPERATIONS
        a, b = self.res["A"], self.res["B"]
        client_a, client_b = self.tenants["A"].id, self.tenants["B"].id
        ids = {key + "_id": row.id for key, row in a.items()}
        ids.update(client_id=client_a, playlist_item_id=a["legacy"].id,
                   sequence_item_id=a["sequence"].id, content_id=a["content"].id,
                   item_id=a["audioitem"].id, videowall_id=a["wall"].id)
        if name in {"delete_branch", "delete_client"}:
            self.users["B_client_admin"].branch_id = a["branch"].id
        elif name == "delete_channel":
            b["schedule"].channel_id = a["channel"].id
        elif name in {"delete_campaign", "delete_playlist_item", "delete_sequence_item"}:
            a["sequence"].content_id = b["content"].id
        elif name == "delete_content":
            b["sequence"].content_id = a["content"].id
        elif name == "delete_content_folder":
            b["content"].folder_id = a["folder"].id
        elif name == "delete_audio_playlist_item":
            a["audioitem"].content_id = b["audio"].id
        elif name == "delete_layout_binding":
            a["binding"].dataset_id = b["dataset"].id
        elif name == "delete_schedule":
            a["schedule"].layout_id = b["layout"].id
        elif name == "delete_client_user":
            user = self.users["A_client_operator"]
            user.branch_id = b["branch"].id
            ids["user_id"] = user.id
        elif name == "delete_team_user":
            user = self.users["A_staff_operator"]
            ids["user_id"] = user.id
            self.add(E.LayoutRevision, layout_id=b["layout"].id, name="QA", created_by_user_id=user.id)
        elif name == "delete_videowall":
            a["wall"].primary_channel_id = b["channel"].id
        elif name == "delete_node_screen":
            node = self.add(E.VideowallNode, videowall_id=a["wall"].id, channel_id=b["channel"].id,
                            position_index=1, row_index=0, column_index=0, x=0, y=0, width=1920, height=1080)
            ids["node_id"] = node.id
        elif name == "delete_touch_experience":
            b["screen"].experience_id = a["experience"].id
        elif name == "delete_touch_screen":
            self.add(E.KioskButton, screen_id=b["screen"].id, label="QA",
                     action_type=E.KioskActionType.SWITCH_SCREEN, target_screen_id=a["screen"].id)
        elif name == "delete_touch_button":
            button = self.add(E.KioskButton, screen_id=a["screen"].id, label="QA",
                              action_type=E.KioskActionType.SWITCH_SCREEN, target_screen_id=b["screen"].id)
            ids["button_id"] = button.id
        elif name in {"delete_touch_location", "delete_touch_map"}:
            a["experience"].home_screen_id = b["screen"].id
            if name == "delete_touch_map":
                ids["map_id"] = self.add(E.TouchMap, experience_id=a["experience"].id, name="QA").id
        elif name == "delete_touch_assignment":
            assignment = self.add(E.TouchExperienceAssignment, client_id=client_a,
                                  experience_id=a["experience"].id, branch_id=b["branch"].id)
            ids["assignment_id"] = assignment.id
        else:
            self.fail("Missing HTTP delete scenario: " + name)
        self.db.commit()
        operation = next(o for o in OPERATIONS if o.function == name)
        kwargs = {"body": {"confirm_text": "ELIMINAR"}} if name == "delete_client" else {}
        self.reject("DELETE", operation.path.format(**ids), who="A_super_admin", **kwargs)


from app.services.tenant_relations import OPERATIONS as _operations
for _op in _operations:
    if _op.method == "DELETE":
        def _test_http(self, name=_op.function):
            self.delete_case(name)
        setattr(DeleteHTTPBoundaryTests, "test_http_" + _op.function, _test_http)


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteHTTPPostgresTests(LocalPostgresFixture, DeleteHTTPBoundaryTests):
    pass

# A7-R1: permanent regressions from the independent hostile audit.
@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteR1ConcurrencyTests(DeleteMatrixFixture):
    make_engine = DeleteEdgePostgresTests.make_engine

    def test_branch_stale_plan_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.api.routes import branches
        channel, foreign = self.ensure("Channel", "A"), self.ensure("Channel", "B")
        branch, actor = self.ensure("Branch", "A"), self.actor("A", "super_admin")
        self.db.commit()
        channel_id, foreign_id, branch_id, client_id = channel.id, foreign.id, branch.id, channel.client_id
        original = mutations._effect_roots
        inserted = []
        def discover(graph, roots, operation):
            result = original(graph, roots, operation)
            if operation == "delete_branch" and not inserted:
                def writer():
                    with Session(self.engine) as db:
                        wall = E.Videowall(client_id=client_id, name="QA racing wall", rows=1, columns=2)
                        db.add(wall)
                        db.flush()
                        for index, identity in enumerate((channel_id, foreign_id)):
                            db.add(E.VideowallNode(videowall_id=wall.id, channel_id=identity,
                                position_index=index + 1, row_index=0, column_index=index,
                                x=1920 * index, y=0, width=1920, height=1080))
                        db.commit()
                        return wall.id
                with ThreadPoolExecutor(max_workers=1) as pool:
                    inserted.append(pool.submit(writer).result(timeout=8))
            return result
        # The independent writer intentionally mutates QA data. Observe only
        # the DELETE transaction, not the other connection's fixture creation.
        effects = []
        connection = self.db.connection()
        def observe(conn, cursor, sql, *args):
            if conn is connection and sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                effects.append(sql.split()[0].upper())
        event.listen(self.engine, "before_cursor_execute", observe)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", observe)
        with patch.object(mutations, "_effect_roots", discover):
            with self.assertRaises(HTTPException) as caught:
                branches.delete_branch(branch_id, self.db, actor)
            self.assertEqual((caught.exception.status_code, caught.exception.detail),
                             (409, "TENANT_RELATION_CONFLICT"))
        self.assertEqual(effects, [])
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        self.db.rollback()
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(E.Branch, branch_id))
            self.assertIsNotNone(db.get(E.Videowall, inserted[0]))
            self.assertIsNotNone(db.get(E.Channel, foreign_id))

    def concurrent_admin_deletes(self, start, reverse=False, direct_policy=False):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock
        from time import monotonic
        from sqlalchemy import func
        from app.api.routes import team
        first = self.actor("A", "super_admin")
        others = [self.add(E.User, email=uuid.uuid4().hex + "@example.com", full_name="QA",
                          password_hash="synthetic", role="super_admin", is_active=True, status="active")
                  for _ in range(start - 1)]
        self.db.commit()
        identities = [first.id, others[0].id]
        if reverse:
            identities.reverse()
        ready, release, second_checked = Event(), Event(), Event()
        mutex, pids, checked = Lock(), {}, []
        original = team.ensure_super_admin_survives
        def held(**kwargs):
            original(**kwargs)
            with mutex:
                checked.append(kwargs["target_user"].id)
                if len(checked) == 1:
                    ready.set()
                else:
                    second_checked.set()
            if not release.wait(12):
                raise AssertionError("QA barrier timed out")
        def worker(identity):
            with Session(self.engine, expire_on_commit=False, autoflush=False) as db:
                pids[identity] = db.scalar(text("SELECT pg_backend_pid()"))
                actor = db.get(E.User, identity)
                try:
                    if direct_policy:
                        team.ensure_super_admin_survives(db=db, target_user=actor, deleting=True)
                        db.delete(actor)
                        db.commit()
                    else:
                        team.delete_team_user(identity, db, actor)
                    return "COMMITTED"
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code, exc.detail
        blocked = False
        with patch.object(team, "ensure_super_admin_survives", held), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(worker, identities[0])
            try:
                self.assertTrue(ready.wait(8))
                two = pool.submit(worker, identities[1])
                deadline = monotonic() + 8
                with self.engine.connect() as observer:
                    while monotonic() < deadline and not second_checked.is_set():
                        pid = pids.get(identities[1])
                        if pid and pids[identities[0]] in observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}):
                            blocked = True
                            break
                        second_checked.wait(.02)
            finally:
                release.set()
            results = [one.result(timeout=12), two.result(timeout=12)]
        with Session(self.engine) as db:
            count = db.scalar(select(func.count()).select_from(E.User).where(
                E.User.role == E.UserRole.SUPER_ADMIN, E.User.is_active.is_(True), E.User.status == "active"))
        self.assertEqual(count, 1, (results, checked, blocked))
        self.assertTrue(blocked, "Second transaction must wait on a real PostgreSQL lock")
        self.assertEqual(results.count("COMMITTED"), start - 1)
        if start == 2:
            self.assertIn((409, "No se puede eliminar el ultimo super_admin"), results)

    def test_last_superadmin_concurrent_delete(self):
        self.concurrent_admin_deletes(2)

    def test_three_superadmins_two_concurrent_deletes(self):
        self.concurrent_admin_deletes(3)

    def test_last_superadmin_reverse_order(self):
        self.concurrent_admin_deletes(2, reverse=True)

    def test_superadmin_decision_uses_locked_state(self):
        self.concurrent_admin_deletes(2, direct_policy=True)


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A8")
class WriteSuperAdminConcurrencyTests(DeleteMatrixFixture):
    make_engine = DeleteEdgePostgresTests.make_engine

    def race(self, start, actions):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock
        from time import monotonic
        from sqlalchemy import func
        from app.api.routes import team
        from app.schemas.domain import UserStatusUpdate, UserUpdate

        first = self.actor("A", "super_admin")
        others = [self.add(E.User, email=uuid.uuid4().hex + "@example.com", full_name="QA",
                           password_hash="synthetic", role="super_admin", is_active=True, status="active")
                  for _ in range(start - 1)]
        self.db.commit()
        identities = [first.id, others[0].id]
        ready, release, second_checked = Event(), Event(), Event()
        mutex, pids, checked = Lock(), {}, []
        original = team.ensure_super_admin_survives

        def held(**kwargs):
            original(**kwargs)
            with mutex:
                checked.append(kwargs["target_user"].id)
                (ready if len(checked) == 1 else second_checked).set()
            if not release.wait(15):
                raise AssertionError("QA invariant barrier timed out")

        def worker(identity, action):
            with Session(self.engine, expire_on_commit=False, autoflush=False) as db:
                pids[identity] = db.scalar(text("SELECT pg_backend_pid()"))
                actor = db.get(E.User, identity)
                try:
                    if action == "DELETE":
                        team.delete_team_user(identity, db, actor)
                    elif action == "DEMOTE":
                        team.update_team_user(identity, UserUpdate(role="staff_admin"), db, actor)
                    else:
                        team.update_team_user_status(identity, UserStatusUpdate(status="suspended"), db, actor)
                    return "COMMITTED"
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code, exc.detail

        blocked, two = False, None
        with patch.object(team, "ensure_super_admin_survives", held), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(worker, identities[0], actions[0])
            try:
                self.assertTrue(ready.wait(8), "First transaction must reach the locked invariant")
                two = pool.submit(worker, identities[1], actions[1])
                deadline = monotonic() + 8
                with self.engine.connect() as observer:
                    while monotonic() < deadline and not second_checked.is_set():
                        pid = pids.get(identities[1])
                        if pid and pids[identities[0]] in observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}):
                            blocked = True
                            break
                        second_checked.wait(.02)
            finally:
                release.set()
            results = [one.result(timeout=15), two.result(timeout=15)]
        with Session(self.engine) as db:
            count = db.scalar(select(func.count()).select_from(E.User).where(
                E.User.role == E.UserRole.SUPER_ADMIN, E.User.is_active.is_(True), E.User.status == "active"))
        self.assertEqual(count, 1, (actions, results, checked, blocked))
        self.assertTrue(blocked, "Second transaction must wait on a real PostgreSQL lock")
        self.assertEqual(results.count("COMMITTED"), start - 1)
        if start == 2:
            rejected = [result for result in results if result != "COMMITTED"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0][0], 409)
            self.assertIn("ultimo super_admin", rejected[0][1])


for _count in (2, 3):
    for _actions in (("DEMOTE", "DEMOTE"), ("DEACTIVATE", "DEACTIVATE"),
                     ("DELETE", "DEMOTE"), ("DELETE", "DEACTIVATE"),
                     ("DEMOTE", "DEACTIVATE"), ("DEMOTE", "DELETE"),
                     ("DEACTIVATE", "DELETE"), ("DEACTIVATE", "DEMOTE")):
        def _race(self, count=_count, actions=_actions):
            self.race(count, actions)
        setattr(WriteSuperAdminConcurrencyTests,
                "test_" + str(_count) + "_admins_" + "_".join(_actions).lower(), _race)
del _count, _actions, _race


class DeleteR1ORMInventoryTests(unittest.TestCase):
    def test_orm_metadata_matches_exact_foreign_key(self):
        from app.services import tenant_relations as registry
        mismatches = []
        for edge in registry.RELATION_SPECS:
            source = model_class(edge.model)
            fk = inspect(source).columns[edge.field]
            expected = []
            for rel in inspect(model_class(edge.target)).relationships:
                if rel.mapper.class_ is source and fk in rel._calculated_foreign_keys:
                    expected.append(f"{edge.target}.{rel.key}: {rel.cascade}; passive_deletes={str(rel.passive_deletes).lower()}")
            actual = " | ".join(expected) or "Sin relationship inversa ORM; la FK de DB sigue aplicando"
            if actual != edge.orm_cascade:
                mismatches.append((edge.key, edge.orm_cascade, actual))
        self.assertEqual(mismatches, [])

    def test_delete_orphan_real_inventory_is_43_not_45(self):
        from app.services import tenant_relations as registry
        real = [(spec.name, rel.key, rel.mapper.class_.__name__, str(rel.cascade))
                for spec in MODEL_SPECS for rel in inspect(model_class(spec.name)).relationships
                if "delete-orphan" in rel.cascade]
        self.assertEqual(len(real), 43)
        self.assertEqual(registry.inspect_orm_delete_orphans(), tuple(sorted(real)))
        registered = [edge.key for edge in RELATION_SPECS if "delete-orphan" in edge.orm_cascade]
        self.assertEqual(len(registered), 43)
        self.assertNotIn("DeviceOutput.channel_id", registered)
        self.assertNotIn("KioskButton.target_screen_id", registered)


class DeleteR1ExecutionTests(DeleteMatrixFixture):
    def test_legacy_sequence_delete_materializes_only_survivors(self):
        from app.api.routes import campaigns
        campaign, content = self.ensure("Campaign", "A"), self.ensure("ContentItem", "A")
        first = self.add(E.CampaignPlaylistItem, campaign_id=campaign.id, content_id=content.id,
                         sort_order=1, duration_seconds=7, zone_key="main")
        survivor = self.add(E.CampaignPlaylistItem, campaign_id=campaign.id, content_id=content.id,
                            sort_order=2, duration_seconds=11, zone_key="side")
        self.db.commit()
        deleted_id, surviving_id = first.id, survivor.id
        campaigns.delete_sequence_item(campaign.id, deleted_id, self.db, self.actor("A", "super_admin"))
        sequence = list(self.db.scalars(select(E.CampaignSequenceItem).where(E.CampaignSequenceItem.campaign_id == campaign.id)))
        playlist = list(self.db.scalars(select(E.CampaignPlaylistItem).where(E.CampaignPlaylistItem.campaign_id == campaign.id)))
        self.assertEqual([(x.id, x.sort_order, x.duration_seconds, x.options_json) for x in sequence],
                         [(surviving_id, 1, 11, {"zone_key": "side"})])
        self.assertEqual([(x.content_id, x.sort_order, x.duration_seconds, x.zone_key) for x in playlist],
                         [(content.id, 1, 11, "side")])
        self.assertIsNone(self.db.get(E.CampaignSequenceItem, deleted_id))

    def test_discovery_runs_after_anchor_lock_and_before_approval(self):
        target = self.ensure("Branch", "A")
        self.ensure("Channel", "A")
        self.db.commit()
        phases = []
        discovery, lock = mutations._effect_roots, mutations._lock_delete_anchors
        def discover(*args):
            phases.append("DISCOVER")
            return discovery(*args)
        def anchors(*args):
            lock(*args)
            phases.append("LOCK")
        with patch.object(mutations, "_effect_roots", discover), patch.object(mutations, "_lock_delete_anchors", anchors):
            with mutations.delete_guard(self.db, (target,), operation="delete_branch"):
                self.assertEqual(phases, ["DISCOVER", "LOCK", "DISCOVER", "DISCOVER"])
        self.db.rollback()

    def test_bulk_cleanup_cannot_expand_execution_set(self):
        from sqlalchemy import delete
        local, foreign = self.ensure("Campaign", "A"), self.ensure("Campaign", "B")
        self.db.commit()
        with mutations.delete_guard(self.db, (local,)), self.no_effects():
            with self.assertRaises(HTTPException):
                self.db.execute(delete(E.Campaign).where(E.Campaign.id.in_((local.id, foreign.id))))
        self.db.rollback()

    def test_post_gate_query_cannot_turn_context_into_delete_target(self):
        from sqlalchemy import delete
        campaign, client = self.ensure("Campaign", "A"), self.ensure("Client", "A")
        self.db.commit()
        with mutations.delete_guard(self.db, (campaign,)), self.no_effects():
            rediscovered = self.db.scalar(select(E.Client).where(E.Client.id == client.id))
            with self.assertRaises(HTTPException):
                self.db.execute(delete(E.Client).where(E.Client.id == rediscovered.id))
        self.db.rollback()

    def test_orm_cleanup_cannot_expand_execution_set(self):
        local, foreign = self.ensure("Campaign", "A"), self.ensure("Campaign", "B")
        self.db.commit()
        with mutations.delete_guard(self.db, (local,)):
            self.db.delete(foreign)
            with self.assertRaises(HTTPException):
                self.db.flush()
            self.db.rollback()
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(E.Campaign, foreign.id))

    def test_approved_set_is_immutable(self):
        target = self.ensure("Campaign", "A")
        self.db.commit()
        with mutations.delete_guard(self.db, (target,)) as approval:
            self.assertIsInstance(approval.covered, frozenset)
            with self.assertRaises(AttributeError):
                approval.covered = frozenset()
        self.db.rollback()

    def test_secondary_targets_survive_without_false_delete_orphan(self):
        for key in ("DeviceOutput.channel_id", "KioskButton.target_screen_id"):
            edge = next(e for e in RELATION_SPECS if e.key == key)
            child, target, _ = self.scenario(edge, "A", foreign=False)
            child_id = child.id
            with mutations.delete_guard(self.db, (target,)):
                self.db.delete(target)
                self.db.commit()
            self.db.expire_all()
            survivor = self.db.get(type(child), child_id)
            self.assertIsNotNone(survivor)
            self.assertIsNone(getattr(survivor, edge.field))


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A7")
class DeleteR1AdditionalPostgresTests(DeleteR1ExecutionTests):
    make_engine = DeleteEdgePostgresTests.make_engine
    blocked_writer = DeleteConcurrencyTests.blocked_writer

    def test_client_stale_graph_before_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        client, branch = self.ensure("Client", "A"), self.ensure("Branch", "A")
        foreign = self.ensure("Client", "B").id
        self.db.commit()
        branch_id = branch.id
        original, inserted = mutations._effect_roots, []
        def discover(graph, roots, operation):
            result = original(graph, roots, operation)
            if not inserted:
                def write():
                    with Session(self.engine) as db:
                        db.add(E.User(client_id=foreign, branch_id=branch_id, role="client_admin",
                                      email=uuid.uuid4().hex+"@example.com", full_name="QA", password_hash="synthetic"))
                        db.commit()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(write).result(timeout=8)
                inserted.append(True)
            return result
        with patch.object(mutations, "_effect_roots", discover):
            with self.assertRaises(HTTPException) as caught:
                with mutations.delete_guard(self.db, (client,)):
                    self.fail("Corrupt new dependent approved")
            self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)

    def test_preliminary_anchor_prevents_insert_before_rediscovery(self):
        parent = self.ensure("Branch", "A")
        client_id = self.ensure("Client", "A").id
        self.db.commit()
        parent_id = parent.id
        original, calls = mutations._lock_delete_anchors, []
        def locked(graph, refs):
            original(graph, refs)
            calls.append(True)
            def insert(db):
                db.add(E.Channel(client_id=client_id, branch_id=parent_id, name="QA new channel"))
                db.flush()
            self.blocked_writer(insert)
        with patch.object(mutations, "_lock_delete_anchors", locked):
            with mutations.delete_guard(self.db, (parent,), operation="delete_branch"):
                pass
        self.assertEqual(len(calls), 1)
        self.db.rollback()

    def test_post_lock_rediscovery_includes_new_manual_root(self):
        from concurrent.futures import ThreadPoolExecutor
        branch, channel = self.ensure("Branch", "A"), self.ensure("Channel", "A")
        self.db.commit()
        client_id, channel_id = channel.client_id, channel.id
        original, inserted = mutations._effect_roots, []
        def discovery(graph, roots, operation):
            result = original(graph, roots, operation)
            if not inserted:
                def writer():
                    with Session(self.engine) as db:
                        wall = E.Videowall(client_id=client_id, name="QA legitimate racing wall", rows=1, columns=1)
                        db.add(wall)
                        db.flush()
                        db.add(E.VideowallNode(videowall_id=wall.id, channel_id=channel_id, position_index=1,
                                              row_index=0, column_index=0, x=0, y=0, width=1920, height=1080))
                        db.commit()
                        return wall.id
                with ThreadPoolExecutor(max_workers=1) as pool:
                    inserted.append(pool.submit(writer).result(timeout=8))
            return result
        with patch.object(mutations, "_effect_roots", discovery):
            with mutations.delete_guard(self.db, (branch,), operation="delete_branch") as plan:
                self.assertIn(RowRef("Videowall", inserted[0]), plan.covered)
                self.assertIn(RowRef("Videowall", inserted[0]), {i.reference for p in plan.plans for i in p.intents})
        self.db.rollback()

    def test_branch_incoming_wall_after_approval_is_blocked(self):
        branch, channel = self.ensure("Branch", "A"), self.ensure("Channel", "A")
        foreign = self.ensure("Channel", "B")
        self.db.commit()
        client_id, channel_id, foreign_id = channel.client_id, channel.id, foreign.id
        def insert(db):
            wall = E.Videowall(client_id=client_id, name="QA blocked wall", rows=1, columns=2)
            db.add(wall)
            db.flush()
            for index, identity in enumerate((channel_id, foreign_id)):
                db.add(E.VideowallNode(videowall_id=wall.id, channel_id=identity, position_index=index+1,
                    row_index=0, column_index=index, x=index*1920, y=0, width=1920, height=1080))
            db.flush()
        with mutations.delete_guard(self.db, (branch,), operation="delete_branch"):
            self.blocked_writer(insert)
        self.db.rollback()

    def test_inverse_context_deadlock_is_controlled_without_dml(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from sqlalchemy.exc import DBAPIError
        a, b = self.ensure("Branch", "A"), self.ensure("Branch", "B")
        actor = self.actor("A", "super_admin")
        self.add(E.User, client_id=a.client_id, branch_id=b.id, role="client_admin",
                 email=uuid.uuid4().hex+"@example.com", full_name="QA", password_hash="synthetic")
        self.add(E.User, client_id=b.client_id, branch_id=a.id, role="client_admin",
                 email=uuid.uuid4().hex+"@example.com", full_name="QA", password_hash="synthetic")
        self.db.commit()
        actor_id = actor.id
        inputs = ((a.id, a.client_id), (b.id, b.client_id))
        barrier, sqlstates, effects = Barrier(2), [], []
        original = mutations._DeleteGraph.snapshot
        def snapshot(graph, row):
            result = original(graph, row)
            ref = graph.reference(row)
            if graph.locking and ref.model == "Client" and ref.identity == graph.session.info.get("qa_owner"):
                if not graph.session.info.get("qa_barrier"):
                    graph.session.info["qa_barrier"] = True
                    barrier.wait(timeout=8)
            return result
        def error(ctx):
            state = getattr(ctx.original_exception, "sqlstate", None)
            if state:
                sqlstates.append(state)
        def statement(conn, cursor, sql, *args):
            if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                effects.append(sql.split()[0].upper())
        event.listen(self.engine, "handle_error", error)
        event.listen(self.engine, "before_cursor_execute", statement)
        def worker(values):
            with Session(self.engine, autoflush=False) as db:
                db.info["qa_owner"] = values[1]
                target, user = db.get(E.Branch, values[0]), db.get(E.User, actor_id)
                try:
                    with mutations.delete_guard(db, (target,), actor=user, operation="delete_branch"):
                        self.fail("Corrupt branch approved")
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code, exc.detail
        try:
            with patch.object(mutations._DeleteGraph, "snapshot", snapshot), ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(worker, values) for values in inputs]
                results = [job.result(timeout=15) for job in jobs]
        finally:
            event.remove(self.engine, "handle_error", error)
            event.remove(self.engine, "before_cursor_execute", statement)
        self.assertEqual(results, [(409, "TENANT_RELATION_CONFLICT")]*2)
        self.assertIn("40P01", sqlstates)
        self.assertEqual(effects, [])


def run_a7_r1_mutations():
    """Twelve defense removals, with intact controls and assertion-site checks."""
    import inspect as source_inspect
    from dataclasses import replace
    from app.services import user_admin, tenant_relations as registry
    original_lock = user_admin.lock_super_admin_delete_set
    def early_count(db):
        stale = user_admin.list_super_admins(db)
        original_lock(db)
        return stale
    def released(db):
        rows = original_lock(db)
        db.commit()
        return rows
    @contextmanager
    def no_execution_check(*args):
        yield
    mutant_plan = _recompile_delete_plan
    intact_plan = _recompile_delete_plan()
    stale_plan = mutant_plan([
        ("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),
        ("rediscovered = _effect_roots(graph, initial, operation)", "rediscovered = expanded"),
    ])
    no_rediscovery = mutant_plan([
        ("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),
    ])
    cases = [
        ("stale_branch_plan_reused", mutations, "_locked_delete_plan", stale_plan,
         DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval"),
        ("cleanup_unapproved_target", mutations, "_approved_delete_execution", no_execution_check,
         DeleteR1ExecutionTests, "test_bulk_cleanup_cannot_expand_execution_set"),
        ("rediscovery_omitted_after_lock", mutations, "_locked_delete_plan", no_rediscovery,
         DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval"),
        ("lock_anchor_removed", mutations, "_lock_delete_anchors", lambda *args: None,
         DeleteR1AdditionalPostgresTests, "test_preliminary_anchor_prevents_insert_before_rediscovery"),
        ("post_gate_execution_expansion", mutations, "_approved_delete_execution", no_execution_check,
         DeleteR1ExecutionTests, "test_post_gate_query_cannot_turn_context_into_delete_target"),
        ("last_admin_no_lock", user_admin, "lock_super_admin_delete_set", user_admin.list_super_admins,
         DeleteR1ConcurrencyTests, "test_superadmin_decision_uses_locked_state"),
        ("admin_count_before_lock", user_admin, "lock_super_admin_delete_set", early_count,
         DeleteR1ConcurrencyTests, "test_superadmin_decision_uses_locked_state"),
        ("admin_lock_released_before_decision", user_admin, "lock_super_admin_delete_set", released,
         DeleteR1ConcurrencyTests, "test_superadmin_decision_uses_locked_state"),
    ]
    for name, key, other in (
        ("device_output_false_orphan", "DeviceOutput.channel_id", "DeviceOutput.root_channel_id"),
        ("button_target_false_orphan", "KioskButton.target_screen_id", "KioskButton.screen_id"),
        ("orm_mapped_to_wrong_fk", "AudioAssignment.music_playlist_id", "AudioAssignment.spot_playlist_id"),
    ):
        specs = tuple(replace(edge, orm_cascade=registry.relation_spec(other).orm_cascade)
                      if edge.key == key else edge for edge in registry.RELATION_SPECS)
        cases.append((name, registry, "RELATION_SPECS", specs,
                      DeleteR1ORMInventoryTests, "test_orm_metadata_matches_exact_foreign_key"))
    real_inventory = registry.inspect_orm_delete_orphans()
    cases.append(("orphan_inventory_incorrect_45", registry, "inspect_orm_delete_orphans",
                  lambda: real_inventory + real_inventory[:2],
                  DeleteR1ORMInventoryTests, "test_delete_orphan_real_inventory_is_43_not_45"))
    results = []
    for name, target, attr, replacement, cls, method in cases:
        results.append(_run_delete_mutation(
            name, target, attr, replacement, cls, method, _a7_defense_sites(name, cls, method),
            recompiled_control=intact_plan if name in {
                "stale_branch_plan_reused", "rediscovery_omitted_after_lock"} else None))
    return results


class DeleteMutationIdentityTests(unittest.TestCase):
    def test_intact_code_with_only_mutant_observers_disconnected_is_invalid(self):
        import inspect as source_inspect
        source = source_inspect.getsource(mutations._locked_delete_plan)
        for name, cls, method in (
            ("stale_branch_plan_reused", DeleteR1ExecutionTests,
             "test_discovery_runs_after_anchor_lock_and_before_approval"),
            ("rediscovery_omitted_after_lock", DeleteR1ExecutionTests,
             "test_discovery_runs_after_anchor_lock_and_before_approval"),
            ("revalidation_omitted", DeleteExecutionTests,
             "test_revalidation_detects_dependency_change"),
        ):
            with self.subTest(mutation=name):
                namespace = {}
                exec(compile(source, "<C-only disconnected observers>", "exec"),
                     dict(vars(mutations)), namespace)
                result = _run_delete_mutation(
                    name, mutations, "_locked_delete_plan", namespace["_locked_delete_plan"],
                    cls, method, _a7_defense_sites(name, cls, method),
                    recompiled_control=_recompile_delete_plan())
                self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
                self.assertFalse(result["detected"])
                self.assertIsNone(result["mutant"])



    @staticmethod
    def plan_case(name, replacement, *, control=None):
        cls, method = (
            (DeleteExecutionTests, "test_revalidation_detects_dependency_change")
            if name == "revalidation_omitted"
            else (DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval")
        )
        return _run_delete_mutation(
            name, mutations, "_locked_delete_plan", replacement, cls, method,
            _a7_defense_sites(name, cls, method),
            recompiled_control=control if control is not None else _recompile_delete_plan())

    def test_four_variants_require_independent_health_and_provenance(self):
        from types import FunctionType
        for name, edits in _PLAN_EDITS.items():
            with self.subTest(mutation=name):
                real = _recompile_delete_plan(edits)
                result = self.plan_case(name, real)
                self.assertEqual(result["control_original"]["status"], "PASS")
                self.assertEqual(result["control_recompiled"]["status"], "PASS")
                self.assertEqual(result["harness_health"]["status"], "PASS")
                self.assertTrue(all(c["sentinel_visible"]
                                    for c in result["harness_health"]["sentinel_checks"]))
                provenance = result["mutation_provenance"]
                self.assertEqual(provenance["MUTATION_APPLIED"], "YES")
                self.assertNotEqual(provenance["BEFORE_AST"], provenance["AFTER_AST"])
                self.assertEqual(provenance["AFTER_FINGERPRINT"],
                                 provenance["EXPECTED_AFTER_FINGERPRINT"])
                self.assertTrue(provenance["checked_before_test"])
                self.assertEqual(result["mutation_result"], "DETECTED", result)
                # The real target edit does not license a disconnected harness.
                broken = FunctionType(real.__code__, dict(vars(mutations)), real.__name__,
                                      real.__defaults__, real.__closure__)
                result = self.plan_case(name, broken)
                self.assertEqual(result["mutation_provenance"]["MUTATION_APPLIED"], "YES")
                self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
                self.assertFalse(result["detected"])
                self.assertIsNone(result["mutant"])

    def missing_binding(self, key):
        from types import FunctionType
        namespace = dict(vars(mutations))
        del namespace[key]
        candidate = FunctionType(_PLAN_ORIGINAL.__code__, namespace, _PLAN_ORIGINAL.__name__)
        result = self.plan_case("stale_branch_plan_reused", candidate)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])
        self.assertTrue(any("missing" in reason for reason in result["harness_health"]["issues"]))
        return result

    def test_observer_missing_is_invalid_before_mutant(self):
        self.missing_binding("_effect_roots")

    def test_collaborator_missing_is_invalid_before_mutant(self):
        self.missing_binding("preflight_relationship_graph")

    def test_live_collaborator_drift_is_not_accepted_as_baseline(self):
        candidate = _recompile_delete_plan(_PLAN_EDITS["stale_branch_plan_reused"])
        with patch.object(mutations, "_effect_roots", lambda *args: ()):
            result = self.plan_case("stale_branch_plan_reused", candidate)
        self.assertEqual(result["harness_health"]["status"], "FAIL")
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_target_unchanged_is_invalid_mutation(self):
        for name in _PLAN_EDITS:
            with self.subTest(mutation=name):
                result = self.plan_case(name, _recompile_delete_plan())
                self.assertEqual(result["harness_health"]["status"], "PASS")
                self.assertEqual(result["mutation_provenance"]["MUTATION_APPLIED"], "NO")
                self.assertEqual(result["mutation_result"], "INVALID_MUTATION")
                self.assertFalse(result["detected"])
                self.assertIsNone(result["mutant"])

    def test_target_not_found_is_invalid_mutation(self):
        name = "stale_branch_plan_reused"
        real = _recompile_delete_plan(_PLAN_EDITS[name])
        with patch.dict(_PLAN_EDITS, {name: (("nonexistent synthetic target", "pass"),)}):
            result = self.plan_case(name, real)
        self.assertEqual(result["mutation_provenance"]["reason"], "mutation target not found exactly once")
        self.assertEqual(result["mutation_result"], "INVALID_MUTATION")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_wrong_registered_edit_is_invalid_mutation(self):
        result = self.plan_case("stale_branch_plan_reused",
                                _recompile_delete_plan(_PLAN_EDITS["rediscovery_omitted_after_lock"]))
        self.assertEqual(result["mutation_result"], "INVALID_MUTATION")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_forged_provenance_attribute_is_not_trusted(self):
        candidate = _recompile_delete_plan()
        candidate.mutation_provenance = {"MUTATION_APPLIED": "YES"}
        result = self.plan_case("stale_branch_plan_reused", candidate)
        self.assertEqual(result["mutation_result"], "INVALID_MUTATION")
        self.assertFalse(result["detected"])

    def test_real_syntax_error_is_invalid_harness(self):
        def broken():
            compile("def invalid(", "<synthetic invalid syntax>", "exec")
        result = DeleteMutationHarnessTests().synthetic_run(broken)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertEqual(result["errors"], 1)

    def test_real_import_error_is_invalid_harness(self):
        def broken():
            import importlib
            importlib.import_module("_synthetic_a7r3_missing_module_7158")
        result = DeleteMutationHarnessTests().synthetic_run(broken)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertEqual(result["errors"], 1)

    def test_missing_name_is_invalid_before_mutant(self):
        def broken():
            return synthetic_a7r3_missing_global_7158()
        result = DeleteMutationHarnessTests().synthetic_run(broken)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_fixture_failure_is_not_detection(self):
        result = DeleteMutationHarnessTests().synthetic_run(lambda: False, broken_fixture=True)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_instrumentation_drift_between_controls_and_mutant_is_invalid(self):
        state, cls, sites = DeleteMutationHarnessTests.synthetic_case()
        def control():
            _MutationProbe.evidence = lambda self: {"status": "PASS"}
            return True
        with patch.object(_MutationProbe, "evidence", _MutationProbe.evidence):
            # Detection must use the health snapshot even if a hook claims PASS.
            result = _run_delete_mutation(
                "synthetic", state, "callback", lambda: False, cls, "test_defense",
                sites, recompiled_control=control)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

class DeleteMutationHarnessTests(unittest.TestCase):
    @staticmethod
    def synthetic_case(*, broken_fixture=False):
        from types import SimpleNamespace
        state = SimpleNamespace(callback=lambda: True)

        class ProbeCase(unittest.TestCase):
            def setUp(self):
                self.assertFalse(broken_fixture, "broken synthetic fixture")

            def test_defense(self):
                value = state.callback()
                if value == "unrelated":
                    self.fail("unrelated assertion")
                self.assertTrue(value, "expected synthetic defense")

        sites = _assertion_sites(ProbeCase.test_defense, 'self.assertTrue(value, "expected synthetic defense")')
        return state, ProbeCase, sites

    def synthetic_run(self, replacement, *, control=None, broken_fixture=False):
        state, cls, sites = self.synthetic_case(broken_fixture=broken_fixture)
        return _run_delete_mutation("synthetic", state, "callback", replacement, cls, "test_defense",
                                    sites, recompiled_control=control)

    def test_expected_assertion_discriminates(self):
        result = self.synthetic_run(lambda: False, control=lambda: True)
        self.assertEqual(result["control_original"]["status"], "PASS")
        self.assertEqual(result["control_recompiled"]["status"], "PASS")
        self.assertEqual(result["mutation_result"], "DETECTED")
        self.assertEqual((result["harness_errors"], result["errors"]), (0, 0))

    def test_intact_control_recompiles_source_without_executing_module(self):
        original = mutations._locked_delete_plan
        with patch.object(mutations, "_effect_roots", object()):
            control = _intact_control(original)
            self.assertIsNot(control.__code__, original.__code__)
            self.assertEqual(_mutation_fingerprint(control), _mutation_fingerprint(original))
            self.assertIs(control.__globals__, original.__globals__)
            self.assertIs(control.__globals__["_effect_roots"], mutations._effect_roots)

    def test_failed_original_fixture_invalidates_without_running_mutant(self):
        calls = []
        result = self.synthetic_run(lambda: calls.append(True), broken_fixture=True)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])
        self.assertEqual(calls, [])

    def test_failed_intact_recompiled_control_invalidates(self):
        result = self.synthetic_run(lambda: False, control=lambda: False)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertIsNone(result["mutant"])

    def test_unrelated_assertion_is_not_detection(self):
        result = self.synthetic_run(lambda: "unrelated")
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertEqual(result["harness_errors"], 1)

    def test_errors_are_never_detection(self):
        for exception in (ImportError, SyntaxError, NameError, RuntimeError):
            with self.subTest(exception=exception.__name__):
                def broken():
                    raise exception("synthetic unrelated failure")
                result = self.synthetic_run(broken)
                self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
                self.assertFalse(result["detected"])
                self.assertEqual(result["errors"], 1)

    def test_skip_is_not_detection(self):
        def skipped():
            raise unittest.SkipTest("synthetic skip")
        result = self.synthetic_run(skipped)
        self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
        self.assertFalse(result["detected"])
        self.assertEqual(result["skipped"], 1)

    def test_surviving_mutant_is_not_detection(self):
        # Different code with the same truthy behavior is a real surviving edit.
        result = self.synthetic_run(lambda: 1)
        self.assertEqual(result["mutation_result"], "SURVIVED")
        self.assertFalse(result["detected"])

    def test_live_globals_preserve_observer_identity(self):
        intact = _recompile_delete_plan()
        sentinel = object()
        self.assertIs(intact.__globals__, vars(mutations))
        with patch.object(mutations, "_effect_roots", sentinel):
            self.assertIs(intact.__globals__["_effect_roots"], sentinel)

    def test_disconnected_observer_controls_invalidate_runner(self):
        import inspect as source_inspect
        namespace = dict(vars(mutations))
        exec(compile(source_inspect.getsource(mutations._locked_delete_plan),
                     "<deliberately broken identity control>", "exec"), namespace)
        for cls, method in (
            (DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval"),
            (DeleteExecutionTests, "test_revalidation_detects_dependency_change"),
        ):
            with self.subTest(method=method):
                result = _run_delete_mutation(
                    "broken_identity", mutations, "_locked_delete_plan", _recompile_delete_plan(),
                    cls, method, frozenset(), recompiled_control=namespace["_locked_delete_plan"])
                self.assertEqual(result["control_original"]["status"], "PASS")
                self.assertEqual(result["mutation_result"], "INVALID_HARNESS")
                self.assertFalse(result["detected"])
                self.assertIsNone(result["mutant"])

    def test_three_plan_mutations_have_original_and_recompiled_controls(self):
        revalidation = ('confirmed = preflight_relationship_graph(gate, tuple(MutationIntent("DELETE", ref) for ref in rediscovered),\n'
                        '                                                      discovery_spec, semantic_adapters=_delete_adapters())')
        for name, replacements, cls, method in (
            ("stale_branch_plan_reused",
             (("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),
              ("rediscovered = _effect_roots(graph, initial, operation)", "rediscovered = expanded")),
             DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval"),
            ("rediscovery_omitted_after_lock",
             (("expanded = _effect_roots(graph, initial, operation)", "expanded = preliminary"),),
             DeleteR1ExecutionTests, "test_discovery_runs_after_anchor_lock_and_before_approval"),
            ("revalidation_omitted", ((revalidation, "confirmed = plan"),),
             DeleteExecutionTests, "test_revalidation_detects_dependency_change"),
        ):
            with self.subTest(mutation=name):
                result = _run_delete_mutation(
                    name, mutations, "_locked_delete_plan", _recompile_delete_plan(replacements),
                    cls, method, _a7_defense_sites(name, cls, method),
                    recompiled_control=_recompile_delete_plan())
                self.assertEqual(result["control_original"]["status"], "PASS")
                self.assertEqual(result["control_recompiled"]["status"], "PASS")
                self.assertEqual(result["mutation_result"], "DETECTED", result)
                self.assertEqual((result["harness_errors"], result["non_discriminating"]), (0, 0))


WRITE_COVERAGE = []


class WriteEdgeMatrixTests(DeleteMatrixFixture):
    """97 write edges: positive control, current corruption and proposed cross-tenant FK.

    Service preflight matrix; HTTP routing/permission coverage is exercised
    separately. Factory writes happen before measured production validation.
    """
    def check_write_edge(self, edge):
        from app.services.tenant_relations import OPERATIONS
        source, target, _ = self.scenario(edge, "A", foreign=False)
        entrypoints = [o for o in OPERATIONS if o.method in {"POST", "PUT", "PATCH"}
                       and edge.model in mutations._write_models(mutations._WriteGraph(self.db), o)]
        self.assertTrue(entrypoints, "Every write edge needs an actual registered write entrypoint")
        operation = entrypoints[0].function
        foreign = self.ensure(edge.target, "B")
        self.db.commit()
        original = getattr(source, edge.field)
        own_client = self.ensure("Client", "A").id
        for case in ("CURRENT", "PROPOSED"):
            if case == "CURRENT":
                self.corrupt(source, **{edge.field: foreign.id})
                proposed = ()
            else:
                self.corrupt(source, **{edge.field: original})
                proposed = ((edge.field, foreign.id),)
            for role in ROLES:
                with self.subTest(edge=edge.key, case=case, role=role), self.no_effects() as effects:
                    intent = mutations.MutationIntent("UPDATE", self.ref(source), proposed)
                    with self.assertRaises(HTTPException) as caught:
                        with mutations.write_guard(self.db, actor=self.actor("A", role), client_id=own_client,
                                                   operation=operation, intents=(intent,)):
                            self.fail("Cross-tenant write approved")
                    self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                     (409, "TENANT_RELATION_CONFLICT"))
                    WRITE_COVERAGE.append(dict(
                        EDGE=edge.key, WRITE_ENTRYPOINT=operation, ENTRYPOINTS=[o.function for o in entrypoints],
                        CURRENT_RELATION="FOREIGN" if case == "CURRENT" else "OWNED",
                        PROPOSED_RELATION="UNCHANGED_FOREIGN" if case == "CURRENT" else "FOREIGN",
                        ENGINE=self.engine.dialect.name, ROLE=role, RESULT="PASS",
                        ORM_ASSIGNMENT=effects.count("ORM_ASSIGNMENT"), DML=sum(x in {"INSERT", "UPDATE", "DELETE"} for x in effects),
                        FLUSH=effects.count("FLUSH"), COMMIT=effects.count("COMMIT")))
                self.db.rollback()
        self.corrupt(source, **{edge.field: original})
        for role in ROLES:
            with self.subTest(edge=edge.key, role=role, positive=True), self.no_effects():
                intent = mutations.MutationIntent("UPDATE", self.ref(source), ())
                with mutations.write_guard(self.db, actor=self.actor("A", role), client_id=own_client,
                                           operation=operation, intents=(intent,)) as proof:
                    self.assertIn(self.ref(source), proof.checked)
            self.db.rollback()


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A8")
class WriteEdgePostgresTests(WriteEdgeMatrixTests):
    make_engine = DeleteEdgePostgresTests.make_engine


for _write_edge in RELATION_SPECS:
    def _write_test(self, edge=_write_edge):
        self.check_write_edge(edge)
    setattr(WriteEdgeMatrixTests, "test_write_edge_" + _write_edge.key.replace(".", "_"), _write_test)
del _write_edge, _write_test


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A8")
class WriteBatchConcurrencyTests(DeleteMatrixFixture):
    make_engine = DeleteEdgePostgresTests.make_engine

    def batch_race(self, kind):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from time import monotonic
        from tempfile import TemporaryDirectory
        from fastapi import UploadFile
        from starlette.datastructures import Headers
        from app.api.routes import audio, channels, layouts, data_sources
        from app.services import layout_editor
        from app.schemas import domain as D
        from app.core.config import get_settings

        client, channel = self.ensure("Client", "A"), self.ensure("Channel", "A")
        actor_id = self.actor("A", "client_admin").id
        playlist = self.ensure("AudioPlaylist", "A")
        playlist.kind = E.AudioPlaylistKind.MUSIC
        campaign = self.ensure("Campaign", "A")
        second_campaign = self.add(E.Campaign, client_id=client.id, name="Second campaign")
        layout, dataset = self.ensure("Layout", "A"), self.ensure("Dataset", "A")
        binding = self.add(E.LayoutDataBinding, layout_id=layout.id, dataset_id=dataset.id,
                           name="QA", preset_key=list(E.LayoutBindingPreset)[0])
        revisions = [self.add(E.LayoutRevision, layout_id=layout.id, revision_number=i + 1,
                              name="QA", status="draft", editor_state_json={}, preview_state_json={})
                     for i in range(2)]
        self.db.commit()
        ids = dict(channel=channel.id, playlist=playlist.id, campaigns=[campaign.id, second_campaign.id],
                   layout=layout.id, dataset=dataset.id, binding=binding.id, revisions=[r.id for r in revisions])
        modules = {"upsert": audio, "assignment": channels, "collection": layouts,
                   "import": data_sources, "publish": layout_editor}
        module = modules[kind]
        original = module.write_guard
        ready, release = Event(), Event()
        pids, effects = {}, {0: [], 1: []}

        @contextmanager
        def held(db, **kwargs):
            index = db.info["a8_worker"]
            with original(db, **kwargs) as proof:
                if index == 0:
                    ready.set()
                    if not release.wait(20):
                        raise AssertionError("QA write barrier timed out")
                yield proof

        def invoke(db, index):
            actor = db.get(E.User, actor_id)
            if kind == "upsert":
                return audio.upsert_audio_assignment(D.AudioAssignmentCreate(
                    channel_id=ids["channel"], music_playlist_id=ids["playlist"], songs_between_spots=index+2), db, actor)
            if kind == "assignment":
                return channels.replace_channel_campaign_assignment(ids["channel"],
                    D.ChannelCampaignAssignmentReplaceRequest(campaign_id=ids["campaigns"][index]), db, actor)
            if kind == "collection":
                return layouts.replace_layout_binding_fields(ids["layout"], ids["binding"],
                    D.LayoutBindingFieldSet(fields=[D.LayoutBindingFieldCreate(target_field="field"+str(index))]), db, actor)
            if kind == "import":
                file = UploadFile(io.BytesIO(b"value\nsynthetic\n"), filename="qa.csv",
                                  headers=Headers({"content-type": "text/csv"}))
                return data_sources.upload_dataset_import(ids["dataset"], file, db, actor)
            return layouts.publish_revision(ids["layout"], ids["revisions"][index], db, actor)

        def worker(index):
            with Session(self.engine, expire_on_commit=False) as db:
                db.info["a8_worker"] = index
                conn = db.connection()
                pids[index] = conn.scalar(text("SELECT pg_backend_pid()"))
                def sql(conn, cursor, statement, params, context, many):
                    if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                        effects[index].append("DML")
                def flushing(*args): effects[index].append("FLUSH")
                def committed(*args): effects[index].append("COMMIT")
                def assigned(target, *args):
                    if inspect(target).session is db:
                        effects[index].append("ORM_ASSIGNMENT")
                listeners = [(conn, "before_cursor_execute", sql), (db, "before_flush", flushing),
                             (db, "after_commit", committed)]
                listeners += [(getattr(mapper.class_, attr.key), "set", assigned)
                              for mapper in Base.registry.mappers for attr in mapper.column_attrs]
                for target, name, fn in listeners: event.listen(target, name, fn)
                try:
                    invoke(db, index)
                    return "COMMITTED"
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code, exc.detail
                finally:
                    for target, name, fn in reversed(listeners): event.remove(target, name, fn)

        blocked, second = False, None
        with TemporaryDirectory(prefix="a8-pg-batch-") as media, patch.object(get_settings(), "MEDIA_ROOT", Path(media)), \
                patch.object(module, "write_guard", held), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker, 0)
            try:
                self.assertTrue(ready.wait(10), "First writer must reach approval")
                second = pool.submit(worker, 1)
                deadline = monotonic() + 10
                with self.engine.connect() as observer:
                    while monotonic() < deadline:
                        if 1 in pids and pids[0] in observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[1]}):
                            blocked = True
                            break
                        if second.done():
                            break
                        release.wait(.02)
            finally:
                release.set()
            results = [first.result(timeout=20), second.result(timeout=20)]
            self.assertTrue(blocked, "Second writer must block on the shared PostgreSQL transaction gate")
            self.assertEqual(results, ["COMMITTED", (409, "TENANT_RELATION_CONFLICT")])
            self.assertEqual(effects[1], [], "Stale second proposal must not mutate before rejection")
            if kind == "import":
                self.assertEqual(len([p for p in Path(media).rglob("*") if p.is_file()]), 1)
            # A fresh retry must prepare the newly committed state and succeed.
            with Session(self.engine, expire_on_commit=False) as db:
                db.info["a8_worker"] = 2
                invoke(db, 1)
            with Session(self.engine) as db:
                if kind == "upsert":
                    self.assertEqual(len(list(db.scalars(select(E.AudioAssignment).where(E.AudioAssignment.channel_id == ids["channel"])))), 1)
                elif kind == "assignment":
                    rows = list(db.scalars(select(E.CampaignChannelAssignment).where(E.CampaignChannelAssignment.channel_id == ids["channel"])))
                    self.assertEqual([r.campaign_id for r in rows], [ids["campaigns"][1]])
                elif kind == "collection":
                    rows = list(db.scalars(select(E.LayoutBindingField).where(E.LayoutBindingField.binding_id == ids["binding"])))
                    self.assertEqual([r.target_field for r in rows], ["field1"])
                elif kind == "publish":
                    rows = list(db.scalars(select(E.LayoutRevision).where(E.LayoutRevision.layout_id == ids["layout"], E.LayoutRevision.is_current_published.is_(True))))
                    self.assertEqual([r.id for r in rows], [ids["revisions"][1]])


for _a8_kind in ("upsert", "assignment", "collection", "import", "publish"):
    def _batch_test(self, kind=_a8_kind):
        self.batch_race(kind)
    setattr(WriteBatchConcurrencyTests, "test_concurrent_" + _a8_kind, _batch_test)
del _a8_kind, _batch_test


def _a8_compile_registered(function, edits):
    """Compile only reviewed exact source edits against live production globals."""
    import ast
    import inspect as source_inspect
    import __future__
    from hashlib import sha256
    source = source_inspect.getsource(function)
    # Route registration is not part of the mutant; direct invocation probes
    # exercise that handler. Keep contextmanager on guard functions.
    lines = source.splitlines(keepends=True)
    while lines and lines[0].lstrip().startswith("@router."):
        lines.pop(0)
    source = "".join(lines)
    altered = source
    for before, after in edits:
        if altered.count(before) != 1:
            raise AssertionError("Registered A8 edit missing or ambiguous")
        altered = altered.replace(before, after)
    original = source_inspect.unwrap(function)
    flags = original.__code__.co_flags & __future__.annotations.compiler_flag
    namespace = {}
    exec(compile(altered, "<registered A8 mutation>", "exec", flags=flags, dont_inherit=True),
         original.__globals__, namespace)
    replacement = namespace[original.__name__]
    return replacement, dict(BEFORE_AST=sha256(ast.dump(ast.parse(source)).encode()).hexdigest(),
                             AFTER_AST=sha256(ast.dump(ast.parse(altered)).encode()).hexdigest(),
                             EXPECTED_AFTER_FINGERPRINT=_mutation_fingerprint(replacement),
                             REGISTERED_EDIT_COUNT=len(edits))


def run_a8_mutations():
    from app.api.routes import layouts, kiosk, schedules, data_sources, audio, team
    from app.services import layout_editor
    from test_tenant_isolation import A8WriteHTTPTests
    from test_tenant_mutation_guards import TenantA8WriteEntryTests, TenantA8MutationEntryTests
    from test_runtime_tenant_safety import PhaseAR1RuntimeTests

    zero_http = (_assertion_sites(HistoricalRelationFixture.observe_unchanged, "self.assertEqual(counters,") |
                 _assertion_sites(A8WriteHTTPTests.observe_unchanged, "self.assertEqual(assignments,"))
    zero_core = _assertion_sites(GraphFixture.no_effects, "self.assertEqual(effects, []", "self.assertFalse(self.db.new")
    cases = []
    def guard_case(name, module, prefix, cls, method, sites):
        # Match the top-level preflight boundary, not intent_factory's nested
        # cleanliness check. The original mutant payload/defense is unchanged.
        cases.append((name, module, "write_guard", (("\n    _clean(db)\n", "\n" + prefix + "    _clean(db)\n"),), cls, method, sites))
    guard_case("orm_assignment_before_guard", schedules,
        '    db.get(model_class("Schedule"), intents[0].reference.identity).title = "premature"\n',
        TenantA8WriteEntryTests, "test_schedule_name_patch_rejects_historical_foreign_relation_before_assignment", zero_core)
    guard_case("current_state_guard_omitted", layout_editor, "    yield None\n    return\n",
        A8WriteHTTPTests, "test_layout_create_revision_rejects_corrupt_clone_before_draft_flags", zero_http)
    guard_case("proposed_state_guard_omitted", layout_editor, "    yield None\n    return\n",
        A8WriteHTTPTests, "test_layout_patch_rejects_proposed_foreign_widget_before_name", zero_http)
    guard_case("global_context_bypass", layout_editor,
        '    if getattr(actor.role, "value", actor.role) in {"staff_operator", "staff_admin", "super_admin"}:\n        yield None\n        return\n',
        A8WriteHTTPTests, "test_layout_publish_historical_foreign_widget_before_flags", zero_http)
    cases.append(("ownership_transfer_upsert", audio, "upsert_audio_assignment", (
        ("        assert_related_clients(target_client_id, [item.client_id for item in candidates])\n", ""),
        ("    existing = candidates[0] if candidates else None\n",
         "    existing = candidates[0] if candidates else None\n    if existing is not None:\n        existing.client_id = target_client_id\n"),
    ), TenantA8MutationEntryTests, "test_direct_upsert_cannot_transfer_foreign_owner", zero_core))
    guard_case("collection_delete_before_full_validation", layouts,
        '    for intent in intents:\n        if intent.action == "DELETE":\n            db.delete(db.get(model_class(intent.reference.model), intent.reference.identity))\n',
        A8WriteHTTPTests, "test_binding_collection_old_corruption_is_not_deleted_first", zero_http)
    guard_case("import_insert_before_preflight", data_sources,
        '    db.add(model_class("DatasetImport")(dataset_id=intents[0].reference.identity, source_filename="synthetic.csv", storage_path="synthetic"))\n',
        A8WriteHTTPTests, "test_dataset_import_current_foreign_source_precedes_file_and_rows", zero_http)
    guard_case("publish_flags_before_validation", layout_editor,
        '    for intent in intents:\n        if intent.reference.model == "LayoutRevision":\n            db.get(model_class("LayoutRevision"), intent.reference.identity).is_current_published = True\n',
        A8WriteHTTPTests, "test_layout_publish_historical_foreign_widget_before_flags", zero_http)
    for name, condition, method in (
        ("demotion_invariant_removed", "next_role is not None", "test_2_admins_demote_demote"),
        ("deactivation_invariant_removed", "next_status is not None", "test_2_admins_deactivate_deactivate"),
    ):
        cases.append((name, team, "ensure_super_admin_survives",
            (("    if not (deleting or next_role is not None or next_status is not None):\n",
              "    if " + condition + ":\n        return\n    if not (deleting or next_role is not None or next_status is not None):\n"),),
            WriteSuperAdminConcurrencyTests, method,
            _assertion_sites(WriteSuperAdminConcurrencyTests.race, "self.assertEqual(count, 1,")))
    cases.append(("shared_post_write_response_serializer_bypass", layouts, "safe_revision_payload", (
        ('    payload = serialize_layout_revision(revision)\n',
         '    return serialize_layout_revision(revision)\n    payload = serialize_layout_revision(revision)\n'),),
        PhaseAR1RuntimeTests, "test_all_revision_reads_sanitize_historical_preview",
        _assertion_sites(PhaseAR1RuntimeTests.test_all_revision_reads_sanitize_historical_preview, "self.assertNotIn(")))
    guard_case("legacy_kiosk_alternate_guard_omitted", kiosk, "    yield None\n    return\n",
        A8WriteHTTPTests, "test_touch_and_legacy_alternates_reject_corrupt_parent_before_insert", zero_http)

    results = []
    for name, module, attr, edits, cls, method, sites in cases:
        baseline = getattr(module, attr)
        replacement, registered = _a8_compile_registered(baseline, edits)
        # Recompile independently; a caller-supplied provenance marker is never proof.
        independently_compiled, independent = _a8_compile_registered(baseline, edits)
        if (_mutation_fingerprint(replacement) != _mutation_fingerprint(independently_compiled)
                or registered != independent):
            raise AssertionError("A8 mutation identity mismatch")
        result = _run_delete_mutation(name, module, attr, replacement, cls, method, sites)
        result["registered_source_proof"] = registered
        result["INDEPENDENT_IDENTITY_MATCH"] = True
        results.append(result)
    return results


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A8")
class WritePlayerFreshScopeTests(DeleteMatrixFixture):
    make_engine = DeleteEdgePostgresTests.make_engine

    def stale_active_scope(self, model, field, value, expected):
        from fastapi import Request
        from app.api.routes import player
        device = self.ensure("PlayerDevice", "A")
        self.db.commit()
        client = self.db.get(E.Client, device.client_id)
        branch = self.db.get(E.Branch, device.branch_id)
        channel = self.db.get(E.Channel, device.channel_id)
        self.assertEqual(client.status, "active")
        self.assertTrue(branch.is_active)
        identity = {"Client": client.id, "Branch": branch.id, "PlayerDevice": device.id}[model]
        # A second real connection commits after this request loaded its actor.
        with Session(self.engine) as concurrent:
            concurrent.execute(update(model_class(model)).where(model_class(model).id == identity).values(**{field: value}))
            concurrent.commit()
        with self.no_effects(), self.assertRaises(HTTPException) as caught:
            player.get_runtime_config(Request({"type": "http", "scheme": "http",
                "server": ("testserver", 80), "headers": []}), device, self.db)
        self.assertEqual(caught.exception.status_code, expected)

    def test_client_suspension_rechecked_after_cached_actor(self):
        self.stale_active_scope("Client", "status", "suspended", 403)

    def test_branch_deactivation_rechecked_after_cached_actor(self):
        self.stale_active_scope("Branch", "is_active", False, 403)

    def test_player_deactivation_rechecked_after_cached_actor(self):
        self.stale_active_scope("PlayerDevice", "is_active", False, 409)


# A9 uses the frozen registry classification, independently of write/delete tests.
READ_COVERAGE = []
RUNTIME_COVERAGE = []


class A9ReadEdgeTests(DeleteMatrixFixture):
    profile = "READ"

    def check_read_edge(self, edge):
        import json
        from typing import Any
        from pydantic import ConfigDict, create_model
        from sqlalchemy import func
        from app.services.tenant_ownership import context_for_actor
        from app.services.tenant_queries import readable_query
        from app.services.tenant_responses import safe_model_read
        cls = model_class(edge.model)
        columns = tuple(key for key in model_spec(edge.model).columns
                        if key not in {"password_hash", "player_token_hash"})
        schema = create_model("A9" + edge.model, __config__=ConfigDict(from_attributes=True),
                              **{key: (Any, None) for key in columns})
        for side in ("A", "B"):
            source, target, _ = self.scenario(edge, side, foreign=False)
            source_id = source.id
            client = self.ensure("Client", side)
            for role in ROLES:
                actor = self.actor(side, role)
                context = context_for_actor(self.db, actor, client_id=client.id)
                query = readable_query(select(cls), cls, db=self.db, profile=self.profile,
                                       client_id=context.client_id).where(cls.id == source_id)
                with self.no_effects():
                    self.assertEqual([r.id for r in self.db.scalars(query)], [source_id],
                                     "A9 intact control must remain eligible")
            source, target, _ = self.scenario(edge, side, foreign=True)
            for role in ROLES:
                context = context_for_actor(self.db, self.actor(side, role), client_id=client.id)
                query = readable_query(select(cls), cls, db=self.db, profile=self.profile,
                                       client_id=context.client_id).where(cls.id == source_id)
                with self.no_effects():
                    rows = list(self.db.scalars(query.limit(1)))
                    neutral = self.profile == "READ" and edge.read_policy == "FOREIGN_FIELD_NEUTRALIZED"
                    self.assertEqual(len(rows), 1 if neutral else 0, "A9 SQL eligibility before page")
                    self.assertEqual(self.db.scalar(select(func.count()).select_from(query.subquery())),
                                     len(rows), "A9 counts use the identical eligible set")
                    payload = [safe_model_read(row, schema, self.db, client_id=context.client_id,
                                               profile=self.profile).model_dump(mode="json") for row in rows]
                    self.assertNotIn(target.id, json.dumps(payload), "A9 foreign ID leaked in projection")
                    if rows:
                        self.assertIsNone(payload[0][edge.field], "A9 optional FK must be neutralized")
                        self.assertEqual(getattr(source, edge.field), target.id, "A9 must not repair ORM")
                        payload[0][edge.field] = "detached copy edit"
                        self.assertEqual(getattr(source, edge.field), target.id, "A9 response copy aliases ORM")
                collection = READ_COVERAGE if self.profile == "READ" else RUNTIME_COVERAGE
                collection.append(dict(EDGE=edge.key, PROFILE=self.profile, ENGINE=self.engine.dialect.name,
                    ROLE=role, CORRUPTION_DIRECTION=side + "->" + ("B" if side == "A" else "A"),
                    READ_ENTRYPOINT="readable_query + safe_model_read", EXPECTED_STRATEGY=(
                        "FOREIGN_FIELD_NEUTRALIZED" if neutral else "ROW_OMITTED"), RESULT="PASS",
                    FOREIGN_ID_LEAK_COUNT=0, BUSINESS_WRITES=0))
            self.doCleanups()
            self.setUp()


class A9RuntimeEdgeTests(DeleteMatrixFixture):
    profile = "RUNTIME"
    check_read_edge = A9ReadEdgeTests.check_read_edge


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A9")
class A9ReadEdgePostgresTests(A9ReadEdgeTests):
    make_engine = DeleteEdgePostgresTests.make_engine


@unittest.skipUnless(os.environ.get("A7_TEST_DATABASE_URL"), "Requires isolated local PostgreSQL A9")
class A9RuntimeEdgePostgresTests(A9RuntimeEdgeTests):
    make_engine = DeleteEdgePostgresTests.make_engine


for _a9profile, _a9classes in (
    ("READ", (A9ReadEdgeTests, A9ReadEdgePostgresTests)),
    ("RUNTIME", (A9RuntimeEdgeTests, A9RuntimeEdgePostgresTests)),
):
    for _a9edge in RELATION_SPECS:
        if _a9profile in _a9edge.operations:
            def _a9test(self, edge=_a9edge):
                self.check_read_edge(edge)
            for _a9class in _a9classes:
                setattr(_a9class, "test_" + _a9profile.lower() + "_" + _a9edge.key.replace(".", "_"), _a9test)

# unittest discovers class aliases too; retain only the four named test classes.
del _a9class


# A9-R3 legacy defense sites: exact tenant/ownership/no-write assertions.
_LEGACY_DEFENSE_ANCHORS = {
    ('A6', 'removed_relation'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_removed_and_ghost_relation_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'wrong_owner'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_direct_structural_and_foreign_ownership', 'self.assertEqual(self.graph.resolve_owner(self.rows[other][key], ctx.client_id).state, OwnerState.FOREIGN)', 0),),
    ('A6', 'global_bypass'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_secondary_foreign_fk_never_transfers_authoritative_owner', 'with self.assertRaises(HTTPException) as caught:', 0),),
    ('A6', 'unknown_accepted'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_unknown_and_ambiguous_never_authorize', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'ambiguous_accepted'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_unknown_and_ambiguous_never_authorize', 'self.assertEqual(self.graph.resolve_owner(candidates).state, OwnerState.AMBIGUOUS)', 0),),
    ('A6', 'pagination_before_filter'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_eligibility_precedes_count_search_and_pagination', 'self.assertEqual(count, 1)', 0),),
    ('A6', 'foreign_projection'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_safe_optional_projection_is_copy_and_essential_is_omitted', 'self.assertEqual(result, {"id": a["campaign"].id, "name": "A", "layout_id": None})', 0),),
    ('A6', 'write_before_preflight'): (('tenant_graph_cases', 'GraphFixture', 'no_effects', 'self.assertEqual(effects, [])', 0),),
    ('A6', 'error_leaks_id'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_generic_error_never_reveals_foreign_input', 'self.assertEqual(serialized, \'{"detail": "TENANT_RELATION_CONFLICT"}\')', 0),),
    ('A6', 'ignored_introspection_mismatch'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_new_fk_or_phantom_registration_is_rejected', 'with self.subTest(extra=extra), self.assertRaises(HTTPException):', 0),),
    ('A6', 'context_edge_not_traversed'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_r1_transitive_schedule_preflight', 'with self.assertRaises(HTTPException) as caught:', 0),),
    ('A6', 'depth_gt_one_ignored'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_r1_transitive_schedule_preflight', 'with self.assertRaises(HTTPException) as caught:', 0),),
    ('A6', 'global_context_bypass'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_r1_owner_and_context_are_separate_for_all_actors', 'self.assertEqual(proof.state.value, "INCONSISTENT")', 0),),
    ('A6', 'query_context_ignored'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_r1_transitive_schedule_query', 'self.assertEqual(self.db.scalar(select(func.count()).select_from(eligible.subquery())), 0)', 0),),
    ('A6', 'preflight_context_ignored'): (('test_tenant_mutation_guards', 'TenantCoreBehaviorTests', 'test_r1_transitive_schedule_preflight', 'with self.assertRaises(HTTPException) as caught:', 0),),
    ('A6', 'context_structural_drift'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_semantic_policy_drift_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'owner_path_removed'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_semantic_policy_drift_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'owner_path_changed'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_semantic_policy_drift_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'runtime_applicability_removed'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_semantic_policy_drift_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('A6', 'conflict_strategy_changed'): (('test_tenant_relation_registry', 'TenantRelationRegistryTests', 'test_semantic_policy_drift_rejected_by_validator', 'with self.assertRaises(HTTPException):', 0),),
    ('historical', 'campaign_layout'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_foreign_relationship_writes_rejected_for_all_writer_roles', 'self.assertIn(response.status_code, (403, 404), response.text)', 0),),
    ('historical', 'kiosk_experience'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_foreign_relationship_writes_rejected_for_all_writer_roles', 'self.assertIn(response.status_code, (403, 404), response.text)', 0),),
    ('historical', 'audio_resource'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_foreign_relationship_writes_rejected_for_all_writer_roles', 'self.assertIn(response.status_code, (403, 404), response.text)', 0),),
    ('historical', 'schedule_branch'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_foreign_relationship_writes_rejected_for_all_writer_roles', 'self.assertIn(response.status_code, (403, 404), response.text)', 0),),
    ('historical', 'orphan_identity'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_orphan_and_corrupt_branch_identity_denied_before_reads', 'self.assertEqual(self.request("GET", path, who="A_" + role).status_code, 403)', 0),),
    ('historical', 'runtime_graph'): (('test_runtime_tenant_safety', 'RuntimeTenantTests', 'assert_no_foreign', 'self.assertNotIn(resource.id, rendered, key)', 0),),
    ('historical', 'touch_target'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_touch_and_legacy_targets_same_tenant_and_experience', 'self.assertIn(response.status_code, (200, 201) if target is a["screen"] else (404,), response.text)', 0),),
    ('historical', 'layout_get_write'): (('test_tenant_isolation', 'TenantRelationsTests', 'test_operator_layout_gets_never_persist', 'self.assertEqual(before, after)', 0),),
    ('historical', 'telemetry'): (('test_runtime_tenant_safety', 'RuntimeTenantTests', 'test_player_invalid_telemetry_sanitizes_only_bad_fields', 'self.assertIsNone(getattr(row, "current_" + field + "_id"))', 0),),
    ('historical', 'runtime_file'): (('test_runtime_tenant_safety', 'PhaseAR1RuntimeTests', 'test_foreign_file_omitted_without_repair', 'self.assertNotIn(foreign,r.text)', 0),),
    ('historical', 'revision_preview'): (('test_runtime_tenant_safety', 'PhaseAR1RuntimeTests', 'test_all_revision_reads_sanitize_historical_preview', 'self.assertNotIn(b["layout"].name,r.text);self.assertNotIn(b["content"].file_path,r.text)', 0),),
    ('historical', 'inactive_output'): (('test_runtime_tenant_safety', 'PhaseAR1RuntimeTests', 'test_inactive_secondary_branch_output_omitted', 'self.assertEqual(r.status_code,200,r.text);self.assertNotIn(out.id,json.dumps(r.json()["outputs"]))', 0),),
    ('historical', 'get_sequence_write'): (('test_runtime_tenant_safety', 'PhaseAR1RuntimeTests', 'test_all_gets_are_read_only_for_all_roles', 'self.assertEqual(writes,[])', 0),),
    ('historical', 'touch_assignment_list'): (('test_tenant_isolation', 'HistoricalRelationshipTests', 'test_touch_list_omits_foreign_assignment', 'self.assertEqual([r["id"] for r in response.json()], [own.id])', 0),),
    ('historical', 'touch_assignment_patch'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'touch_assignment_delete'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'audio_assignment_upsert'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'experience_foreign_screen'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'channel_foreign_audio'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'content_foreign_sequence'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'content_foreign_legacy'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'content_foreign_audio'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'suspended_client'): (('test_runtime_tenant_safety', 'RuntimeTenantTests', 'test_suspension_and_inactive_branch_block_players_without_token_changes', 'self.assertEqual(self.request(method, path, who="player_A", body=body).status_code, 403)', 0), ('test_runtime_tenant_safety', 'RuntimeTenantTests', 'test_suspension_and_inactive_branch_block_players_without_token_changes', 'self.assertEqual(a["player"].player_token_hash, token_before)', 0)),
    ('historical', 'ambiguous_owner'): (('test_media_ownership', 'MediaOwnershipTests', 'test_reference_ownership_same_foreign_dataset_and_unknown', 'self.assertEqual(response.status_code, 409)', 0),),
    ('historical', 'request_upload_provenance'): (('test_media_ownership', 'PhaseAR1MediaTests', 'test_untrusted_references_cannot_grant_ownership', 'self.assertEqual(r.status_code,409)', 0),),
    ('historical', 'duplicate_widget_id'): (('test_runtime_tenant_safety', 'PhaseAR2WidgetTests', '_assert_safe_reads', 'self.assertNotIn(value, response.text)', 0),),
    ('historical', 'screen_incoming_preflight'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'campaign_schedule_preflight'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'channel_replace_preflight'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'folder_context'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'schedule_context'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'branch_context'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'audio_item_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'audio_report_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'audio_assignment_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'touch_assignment_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'layout_binding_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'assert_safe_case', 'self.assertNotIn(corrupt_id, response.text)', 0),),
    ('historical', 'videowall_generic_error'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'reject', 'self.assertEqual(response.json(), {"detail": "TENANT_RELATION_CONFLICT"})', 0),),
    ('historical', 'schedule_campaign_context'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'test_schedule_campaign_context', 'self.assertEqual([row["id"] for row in response.json()], [a["schedule"].id])', 0),),
    ('historical', 'campaign_sequence_read_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'test_campaign_scoped_item_reads', 'self.assertEqual(len(response.json()), 1)', 0),),
    ('historical', 'touch_button_read_fk'): (('test_tenant_isolation', 'ScopedReadIntegrityTests', 'test_touch_button_scoped_read', 'self.assertEqual(len(response.json()), 1)', 0),),
    ('historical', 'r5_kiosk_target'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_dataset_source'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'check_read_case', 'self.assertNotIn(case["bad"].id, by_id)', 0),),
    ('historical', 'r5_dataset_import'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_campaign_layout'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_schedule_relations'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_content_folder'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_content_file'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(self.res["B"]["content"].file_path, response.text)', 0),),
    ('historical', 'r5_channel_branch'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_experience_home'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_experience_attract'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'assert_no_foreign_response', 'self.assertNotIn(row.id, response.text)', 0),),
    ('historical', 'r5_branch_grant'): (('test_tenant_isolation', 'ReadSideIntegrityTests', 'test_branch_transitive_foreign_grant', 'self.assertEqual(response.status_code, 404)', 0),),
    ('historical', 'r5_prevalidation'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'observe_unchanged', 'self.assertEqual(counters, dict.fromkeys(counters, 0), counters)', 0),),
    ('historical', 'r5_runtime_audio'): (('test_runtime_tenant_safety', 'PhaseAR5AudioTests', 'assert_audio', 'self.assertEqual(audio["assignment_scope"], scope)', 0),),
    ('historical', 'r5_folder_code'): (('test_tenant_isolation', 'HistoricalRelationFixture', 'reject', 'self.assertEqual(response.json(), {"detail": "TENANT_RELATION_CONFLICT"})', 0),),
    ('historical', 'dataset_delete'): (('test_media_ownership', 'MediaOwnershipTests', 'test_corrupt_content_delete_preserves_dataset_file', "self.assertTrue(path.exists(), 'Foreign dataset file was deleted')", 0),),
    ('historical', 'source_url_ownership'): (('test_media_ownership', 'PhaseAR1MediaTests', 'test_untrusted_references_cannot_grant_ownership', 'self.assertEqual(r.status_code,409)', 0),),
    ('historical', 'metadata_dos'): (('test_media_ownership', 'PhaseAR1MediaTests', 'test_invalid_metadata_does_not_block_other_tenant', 'self.assertEqual(r.status_code,201,r.text)', 0),),
    ('historical', 'folder_foreign_delete'): (('test_media_ownership', 'PhaseAR1MediaTests', 'test_folder_foreign_child_survives_unchanged', 'self.assertIsNotNone(item)', 0),),
}


def _legacy_defense_sites(group, name):
    import ast
    import importlib
    sites = set()
    for module, cls, method, anchor, occurrence in _LEGACY_DEFENSE_ANCHORS[(group, name)]:
        # Resolve approved assertion locations from source, not a potentially
        # monkeypatched test/fixture callback. Controls detect that disconnect.
        path = Path(importlib.import_module(module).__file__)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls)
        function = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method)
        first = function.lineno
        lines = source.splitlines()[first - 1:function.end_lineno]
        matches = [first + index for index, line in enumerate(lines) if anchor in line]
        if occurrence >= len(matches):
            raise AssertionError("Registered legacy defense moved/disappeared: " + name)
        sites.add((str(path), matches[occurrence]))
    return frozenset(sites)


class VW1HarnessAnchorTests(unittest.TestCase):
    def test_a8_guard_anchor_targets_top_level_preflight(self):
        import inspect as source_inspect
        source = source_inspect.getsource(mutations.write_guard)
        self.assertEqual(source.count("    _clean(db)\n"), 2,
                         "Old anchor also matches intent_factory's nested check")
        self.assertEqual(source.count("\n    _clean(db)\n"), 1)
        with self.assertRaises(AssertionError):
            _a8_compile_registered(mutations.write_guard, (("    _clean(db)\n", "    pass\n"),))
        changed, proof = _a8_compile_registered(mutations.write_guard,
            (("\n    _clean(db)\n", "\n    yield None\n    return\n    _clean(db)\n"),))
        self.assertNotEqual(_mutation_fingerprint(changed), _mutation_fingerprint(mutations.write_guard))
        self.assertTrue(proof)
