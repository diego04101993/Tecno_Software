"""R4: bounded ownership proofs, unchanged real locks, isolated synthetic data."""
import hashlib
import os
import tempfile
import threading
import time
import unittest
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("R4 tests require the isolated test harness, never app.main")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.routes import auth, player
from app.core.config import get_settings
from app.core.security import hash_password
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, CampaignPlaylistItem, CampaignSequenceItem, Channel,
    Client, ContentItem, ContentType, DataSource, DataSourceType, Dataset,
    DatasetImport, Layout, PlayerDevice, PlayerHeartbeat, User, UserRole,
)
from app.services import media_references as media
from app.services import tenant_mutations as mutations
from app.services.auth_rate_limit import AuthRateLimiter, AuthRateLimitMiddleware
from app.services.media_storage import MediaPathError, resolve_stored_media_path
from app.services.tenant_ownership import RowRef, TenantGraph, context_for_actor, freeze


R4_GATE_OBSERVATIONS = []


class GateFixture(unittest.TestCase):
    """No server startup; every test owns a fresh DB/schema and media directory."""
    password = "R4-Synthetic-Only!42"

    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(cls.password)

    def make_engine(self):
        return create_engine("sqlite:///" + str(self.root / "fixture.sqlite"),
                             hide_parameters=True)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="r4-gate-")))
        self.enterContext(patch.object(get_settings(), "MEDIA_ROOT", self.root))
        self.engine = self.make_engine()
        self.addCleanup(self.engine.dispose)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False, autoflush=False)
        self.addCleanup(self.db.close)
        self.rows = {}
        for side in ("A", "B"):
            client = Client(name="R4 " + side, slug="r4-" + side.lower())
            self.db.add(client); self.db.flush()
            branch = Branch(client_id=client.id, name=side, code=side)
            self.db.add(branch); self.db.flush()
            channel = Channel(client_id=client.id, branch_id=branch.id, name=side)
            layout = Layout(client_id=client.id, name=side)
            user = User(client_id=client.id, role=UserRole.CLIENT_ADMIN,
                        email="r4-" + side.lower() + "@example.com", full_name=side,
                        password_hash=self.password_hash, is_active=True, status="active")
            self.db.add_all([channel, layout, user]); self.db.flush()
            campaign = Campaign(client_id=client.id, layout_id=layout.id, name=side)
            physical = self.root / client.id / "owned.png"
            physical.parent.mkdir(); physical.write_bytes(b"R4 synthetic media " + side.encode())
            content = ContentItem(client_id=client.id, name=side, type=ContentType.IMAGE,
                                  file_path=f"/media/{client.id}/owned.png")
            self.db.add_all([campaign, content]); self.db.flush()
            token = "tpw_r4_synthetic_" + side
            device = PlayerDevice(client_id=client.id, branch_id=branch.id,
                channel_id=channel.id, hardware_id="R4-" + side, device_name=side,
                player_token_hash=hashlib.sha256(token.encode()).hexdigest(), is_active=True)
            self.db.add_all([device,
                CampaignSequenceItem(campaign_id=campaign.id, content_id=content.id,
                                     item_type="content", sort_order=1),
                CampaignPlaylistItem(campaign_id=campaign.id, content_id=content.id,
                                     sort_order=1)])
            self.db.flush()
            self.rows[side] = dict(client=client, branch=branch, channel=channel,
                layout=layout, user=user, campaign=campaign, content=content,
                device=device, token=token, path=physical)
        self.db.commit()
        self.ids = {side: {key: row.id for key, row in values.items()
                          if hasattr(row, "id")} for side, values in self.rows.items()}
        app = FastAPI()
        app.state.auth_rate_limiter = AuthRateLimiter()
        app.add_middleware(AuthRateLimitMiddleware)

        @app.middleware("http")
        async def synthetic_peer(request, call_next):
            request.scope["client"] = ("198.51.100.24", 12345)
            return await call_next(request)

        app.include_router(auth.router, prefix="/api/auth")
        app.include_router(player.router, prefix="/api/player")

        def database():
            with Session(self.engine, autoflush=False) as db:
                yield db
        app.dependency_overrides[get_db] = database
        self.http = self.enterContext(TestClient(app, raise_server_exceptions=False))

    @contextmanager
    def locked_phase(self, db=None, paths=None):
        db = self.db if db is None else db
        paths = [self.rows["A"]["path"]] if paths is None else paths
        db.connection()  # Actual guards already own a data transaction here.
        with media.media_reference_operation(db):
            media.lock_media_paths(db, paths)
            with media.media_ownership_validation_phase(db):
                yield db

    def owned(self, *, db=None, side="A", root=None, path=None, references=None):
        db = self.db if db is None else db
        return media.media_owned_by(db, root or self.root,
            path or self.rows[side]["path"], self.ids[side]["client"], references)

    def channel_guard(self, db, *, side="A", actor_side=None):
        actor = db.get(User, self.ids[actor_side or side]["user"])
        return mutations.write_guard(db, actor=actor, client_id=self.ids[side]["client"],
            operation="update_channel", intents=(mutations.MutationIntent("UPDATE",
                RowRef("Channel", self.ids[side]["channel"]), (("name", "R4 changed"),)),))

    def heartbeat(self):
        a = self.ids["A"]
        return self.http.post("/api/player/heartbeat",
            headers={"Authorization": "Bearer " + self.rows["A"]["token"]},
            json={"current_content_id": a["content"], "current_campaign_id": a["campaign"],
                  "current_layout_id": a["layout"], "status": "online"})

    def login(self, password=None):
        return self.http.post("/api/auth/login", json={"email": "r4-a@example.com",
            "password": self.password if password is None else password})

    def history(self, count):
        a = self.ids["A"]
        self.db.add_all([PlayerHeartbeat(player_device_id=a["device"], channel_id=a["channel"],
            client_id=a["client"], branch_id=a["branch"], current_content_id=a["content"],
            current_campaign_id=a["campaign"], current_layout_id=a["layout"])
            for _ in range(count)])
        self.db.commit()

    def assert_conflict(self, action):
        with self.assertRaises(HTTPException) as caught:
            action()
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (409, "TENANT_RELATION_CONFLICT"))


@contextmanager
def builtin_semantic_phase(fixture, *, confirmed=True, ref=None, path=None):
    """Model one real locked confirmation boundary, not a request capability."""
    db = fixture.db
    graph = mutations._WriteGraph(db)
    ref = ref or RowRef("ContentItem", fixture.ids["A"]["content"])
    path = path or fixture.rows["A"]["path"]
    context = context_for_actor(db, db.get(User, fixture.ids["A"]["user"]))
    try:
        with mutations.MutationGate(graph, context) as gate:
            graph.locking = True
            graph.snapshots.clear()
            graph.media_validation_locked = confirmed
            with ExitStack() as coordination:
                if confirmed:
                    coordination.enter_context(media.media_reference_operation(db))
                    media.lock_media_paths(db, [path])
                    graph.media_paths.add(path)
                graph.snapshot(ref)
                with mutations._graph_validation_phase(graph, gate):
                    yield graph, ref, context, mutations._write_adapters()
    finally:
        db.rollback()


class OwnershipIndexPhaseTests(GateFixture):
    def test_locked_same_phase_builds_once(self):
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            with self.locked_phase():
                for _ in range(5):
                    self.assertTrue(self.owned())
            self.assertEqual(builds.call_count, 1)

    def test_unlocked_phase_never_reuses_unprotected_ownership(self):
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            self.db.connection()
            with media.media_ownership_validation_phase(self.db):
                self.assertTrue(self.owned()); self.assertTrue(self.owned())
            self.assertEqual(builds.call_count, 2)

    def test_second_phase_and_new_transaction_rebuild(self):
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            with media.media_reference_operation(self.db):
                media.lock_media_paths(self.db, [self.rows["A"]["path"]])
                for _ in range(2):
                    with media.media_ownership_validation_phase(self.db):
                        self.assertTrue(self.owned()); self.assertTrue(self.owned())
                self.assertEqual(builds.call_count, 2)
            self.db.commit()
            with self.locked_phase():
                self.assertTrue(self.owned())
            self.assertEqual(builds.call_count, 3)

    def test_other_request_session_has_its_own_index(self):
        with Session(self.engine, autoflush=False) as other:
            with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
                with self.locked_phase():
                    self.assertTrue(self.owned())
                    with self.locked_phase(other):
                        self.assertTrue(self.owned(db=other)); self.assertTrue(self.owned(db=other))
                    self.assertTrue(self.owned())
                self.assertEqual(builds.call_count, 2)

    def test_different_roots_never_share_cached_paths(self):
        nested = self.root / "nested-root"
        other_path = nested / self.ids["A"]["client"] / "owned.png"
        other_path.parent.mkdir(parents=True); other_path.write_bytes(b"synthetic")
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            with self.locked_phase(paths=[self.rows["A"]["path"], other_path]):
                self.assertTrue(self.owned())
                self.assertTrue(self.owned(root=nested, path=other_path))
                self.assertTrue(self.owned())
                self.assertTrue(self.owned(root=nested, path=other_path))
            self.assertEqual(builds.call_count, 2)

    def test_exception_cleans_phase_and_next_phase_rebuilds(self):
        with patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            with self.assertRaisesRegex(RuntimeError, "synthetic abort"):
                with self.locked_phase():
                    self.assertTrue(self.owned())
                    raise RuntimeError("synthetic abort")
            with self.locked_phase():
                self.assertTrue(self.owned())
            self.assertEqual(builds.call_count, 2)

    def test_ambiguous_foreign_authoritative_reference_is_not_filtered_out(self):
        self.db.execute(update(ContentItem).where(ContentItem.id == self.ids["B"]["content"])
                        .values(file_path=self.rows["A"]["content"].file_path))
        self.db.commit()
        with self.locked_phase():
            self.assertFalse(self.owned())

    def test_new_phase_observes_ownership_mutation(self):
        with self.locked_phase():
            self.assertTrue(self.owned())
        self.db.execute(update(ContentItem).where(ContentItem.id == self.ids["B"]["content"])
                        .values(file_path=self.rows["A"]["content"].file_path))
        self.db.commit()
        with self.locked_phase():
            self.assertFalse(self.owned())

    def test_public_index_and_explicit_references_keep_existing_semantics(self):
        with self.locked_phase():
            self.assertTrue(self.owned())
            public = media.media_reference_owners(self.db, self.root)
            public[self.rows["A"]["path"]].add(self.ids["B"]["client"])
            self.assertTrue(self.owned())
            self.assertFalse(self.owned(references=public))
        self.db.execute(update(ContentItem).where(ContentItem.id == self.ids["B"]["content"])
                        .values(file_path="../not-a-valid-stored-path"))
        self.db.commit()
        with self.locked_phase():
            self.assertTrue(self.owned())
            with self.assertRaises(MediaPathError):
                media.media_reference_owners(self.db, self.root, strict=True)

    def test_dirty_exit_rejects_even_without_another_cache_lookup(self):
        content = self.db.get(ContentItem, self.ids["A"]["content"])
        def changed():
            with self.locked_phase():
                self.assertTrue(self.owned())
                content.name = "must not persist"
        self.assert_conflict(changed)
        self.db.rollback()
        self.assertEqual(self.db.get(ContentItem, content.id).name, "A")
        with self.locked_phase():
            self.assertTrue(self.owned())

    def test_nested_phase_is_rejected_and_next_scope_is_fresh(self):
        def nested_attempt():
            with self.locked_phase():
                self.assertTrue(self.owned())
                self._nested_phase()
        self.assert_conflict(nested_attempt)
        with self.locked_phase():
            self.assertTrue(self.owned())

    def _nested_phase(self):
        with media.media_ownership_validation_phase(self.db):
            self.fail("Nested phase was accepted")

    def test_flush_commit_core_dml_and_unknown_sql_cannot_change_a_proof(self):
        actions = {
            "flush": lambda: (setattr(self.db.get(ContentItem, self.ids["A"]["content"]), "name", "bad"), self.db.flush()),
            "commit": self.db.commit,
            "orm_dml": lambda: self.db.execute(update(ContentItem).values(name="bad")),
            "connection_dml": lambda: self.db.connection().execute(update(ContentItem).values(name="bad")),
            "unknown_text": lambda: self.db.execute(text("SELECT 1 + 1")),
            "unknown_driver_sql": lambda: self.db.connection().exec_driver_sql("SELECT 1 + 1"),
            "dml_cte": lambda: self.db.execute(select(update(ContentItem).values(name="bad").returning(ContentItem.id).cte())),
        }
        for name, action in actions.items():
            with self.subTest(action=name):
                statements = []
                def observe(conn, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}:
                        statements.append(statement)
                event.listen(self.engine, "before_cursor_execute", observe)
                try:
                    def attempted():
                        with self.locked_phase():
                            self.assertTrue(self.owned())
                            action()
                    self.assert_conflict(attempted)
                finally:
                    event.remove(self.engine, "before_cursor_execute", observe)
                    self.db.rollback()
                self.assertEqual(statements, [])
                self.assertEqual(self.db.get(ContentItem, self.ids["A"]["content"]).name, "A")

    def test_known_pure_text_is_allowed_but_declines_reuse_for_the_phase(self):
        actions = (lambda: self.db.execute(text("SELECT 1")),
                   lambda: self.db.connection().exec_driver_sql("  SELECT 1;  "))
        for action in actions:
            with self.subTest(action=action.__code__.co_firstlineno), \
                    patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
                with self.locked_phase():
                    self.assertTrue(self.owned())
                    self.assertEqual(action().scalar_one(), 1)
                    self.assertTrue(self.owned()); self.assertTrue(self.owned())
                self.assertEqual(builds.call_count, 3)

    def test_caught_mutation_attempt_still_taints_exit_without_next_lookup(self):
        for action in (lambda: self.db.execute(update(ContentItem).values(name="bad")),
                       self.db.commit):
            with self.subTest(action=action):
                def attempted():
                    with self.locked_phase():
                        self.assertTrue(self.owned())
                        self.assert_conflict(action)
                        # Deliberately no ownership lookup after the caught error.
                self.assert_conflict(attempted)
                self.db.rollback()
                self.assertEqual(self.db.get(ContentItem, self.ids["A"]["content"]).name, "A")
                with self.locked_phase():
                    self.assertTrue(self.owned())

    def test_transaction_end_inside_phase_cannot_leave_a_usable_snapshot(self):
        def attempted():
            with self.locked_phase():
                self.assertTrue(self.owned())
                self.db.rollback()
        self.assert_conflict(attempted)
        with self.locked_phase():
            self.assertTrue(self.owned())

    def test_coordination_binding_changes_are_rejected_at_exit_without_lookup(self):
        mutations_to_try = {
            "connection": lambda state: state.__setitem__("connection", object()),
            "engine": lambda state: state.__setitem__("engine", object()),
            "sqlite": lambda state: state.__setitem__("sqlite", not state["sqlite"]),
            "state_object": lambda state: self.db.info.__setitem__("media_reference_operation", dict(state)),
        }
        for name, alter in mutations_to_try.items():
            with self.subTest(binding=name):
                def attempted():
                    self.db.connection()
                    with media.media_reference_operation(self.db):
                        media.lock_media_paths(self.db, [self.rows["A"]["path"]])
                        state = self.db.info["media_reference_operation"]
                        original = dict(state)
                        try:
                            with media.media_ownership_validation_phase(self.db):
                                self.assertTrue(self.owned())
                                alter(state)
                                # Exit is the only check after the altered binding.
                        finally:
                            self.db.info["media_reference_operation"] = state
                            state.clear();state.update(original)
                self.assert_conflict(attempted)
                with self.locked_phase():
                    self.assertTrue(self.owned())

    def test_coordination_state_cannot_appear_inside_an_unlocked_phase(self):
        def attempted():
            self.db.connection()
            try:
                with media.media_ownership_validation_phase(self.db):
                    self.assertTrue(self.owned())
                    self.db.info["media_reference_operation"] = {"paths": set()}
            finally:
                self.db.info.pop("media_reference_operation", None)
        self.assert_conflict(attempted)
        self.db.rollback()
        with self.locked_phase():
            self.assertTrue(self.owned())

    def test_graph_snapshot_tampering_is_rejected_at_exit_without_next_lookup(self):
        self.history(1)
        real = mutations.preflight_relationship_graph
        ref = RowRef("Channel", self.ids["A"]["channel"])
        for action in (lambda store: store.clear(),
                       lambda store: store.__setitem__(ref, dict(store[ref]))):
            with self.subTest(action=action):
                def altered(gate, *args, **kwargs):
                    result = real(gate, *args, **kwargs)
                    if not gate.graph.media_validation_locked:
                        action(gate.graph.snapshots)
                    return result
                def attempted():
                    with self.channel_guard(self.db):
                        self.fail("Changed phase snapshots reached a writer")
                with patch.object(mutations, "preflight_relationship_graph", altered):
                    self.assert_conflict(attempted)
                self.db.rollback()
                with self.channel_guard(self.db):
                    pass
                self.db.rollback()

    def test_prepopulated_unlocked_snapshot_declines_graph_fact_reuse(self):
        graph = mutations._WriteGraph(self.db)
        ref = RowRef("Channel", self.ids["A"]["channel"])
        graph.snapshot(ref)  # Immutable, but not read under this writer's lock.
        context = context_for_actor(self.db, self.db.get(User, self.ids["A"]["user"]))
        foundation = TenantGraph.resolve_owner
        calls = []
        def observed(view, row, *args, **kwargs):
            if row == ref:
                calls.append(row)
            return foundation(view, row, *args, **kwargs)
        with mutations.MutationGate(graph, context) as gate:
            graph.locking = True
            with media.media_reference_operation(self.db):
                media.lock_media_paths(self.db, [self.rows["A"]["path"]])
                with patch.object(TenantGraph, "resolve_owner", observed):
                    with mutations._graph_validation_phase(graph, gate):
                        self.assertFalse(graph._validation_memo.locked(ref))
                        for _ in range(2):
                            self.assertEqual(graph.resolve_owner(ref).client_id, self.ids["A"]["client"])
                        self.assertEqual(len(calls), 2)
                    # A new authoritative phase can use only a new real locked
                    # read. Clearing outside the previous phase is deliberate.
                    graph.snapshots.clear()
                    graph.snapshot(ref)
                    calls.clear()
                    with mutations._graph_validation_phase(graph, gate):
                        self.assertTrue(graph._validation_memo.locked(ref))
                        for _ in range(2):
                            self.assertEqual(graph.resolve_owner(ref).client_id, self.ids["A"]["client"])
                        self.assertEqual(len(calls), 1)
        self.db.rollback()

    def test_unlocked_ancestor_declines_the_phase_even_for_a_locked_subject(self):
        graph = mutations._WriteGraph(self.db)
        subject = RowRef("Channel", self.ids["A"]["channel"])
        ancestor = RowRef("Client", self.ids["A"]["client"])
        graph.snapshot(ancestor)  # Prepopulated immutable data is not a lock proof.
        context = context_for_actor(self.db, self.db.get(User, self.ids["A"]["user"]))
        foundation = TenantGraph.resolve_owner
        calls = []
        def observed(view, row, *args, **kwargs):
            if row == subject:
                calls.append(row)
            return foundation(view, row, *args, **kwargs)
        with mutations.MutationGate(graph, context) as gate:
            graph.locking = True
            graph.snapshot(subject)
            with media.media_reference_operation(self.db):
                media.lock_media_paths(self.db, [self.rows["A"]["path"]])
                with patch.object(TenantGraph, "resolve_owner", observed), \
                        patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
                    with mutations._graph_validation_phase(graph, gate):
                        memo = graph._validation_memo
                        self.assertTrue(memo.locked(subject))
                        self.assertFalse(memo.locked(ancestor))
                        self.assertTrue(memo.authority.reusable)
                        for _ in range(2):
                            self.assertEqual(graph.resolve_owner(subject).client_id, self.ids["A"]["client"])
                        self.assertFalse(memo.authority.reusable)
                        self.assertEqual(len(calls), 2)
                        self.assertTrue(self.owned());self.assertTrue(self.owned())
                        self.assertEqual(builds.call_count, 2)
        self.db.rollback()

    def test_canonical_context_snapshot_reuses_but_custom_snapshot_never_does(self):
        graph = mutations._WriteGraph(self.db)
        ref = RowRef("Channel", self.ids["A"]["channel"])
        context = context_for_actor(self.db, self.db.get(User, self.ids["A"]["user"]))
        foundation = TenantGraph.context_integrity
        calls = []
        def observed(view, row, *args, **kwargs):
            if row == ref:
                calls.append(row)
            return foundation(view, row, *args, **kwargs)
        with mutations.MutationGate(graph, context) as gate:
            graph.locking = True
            graph.snapshots.clear()  # Match the real guard after gate authorization.
            with media.media_reference_operation(self.db):
                media.lock_media_paths(self.db, [self.rows["A"]["path"]])
                canonical = graph.snapshot(ref)
                with patch.object(TenantGraph, "context_integrity", observed):
                    with mutations._graph_validation_phase(graph, gate):
                        first = graph.context_integrity(ref, context, snapshot=canonical)
                        second = graph.context_integrity(ref, context, snapshot=canonical)
                        self.assertEqual(first.state.value, "CONSISTENT")
                        self.assertEqual(first, second)
                        self.assertEqual(len(calls), 1)
                        custom = dict(canonical)
                        for _ in range(2):
                            self.assertEqual(graph.context_integrity(ref, context, snapshot=custom), first)
                        self.assertEqual(len(calls), 3)
                        foreign = dict(canonical, branch_id=self.ids["B"]["branch"])
                        self.assertEqual(graph.context_integrity(ref, context, snapshot=foreign).state.value,
                                         "INCONSISTENT")
                        self.assertEqual(len(calls), 4)
                        self.assertEqual(graph.context_integrity(ref, context, snapshot=canonical), first)
                        self.assertEqual(len(calls), 4)
        self.db.rollback()

    def test_builtin_confirmation_replays_full_semantics_at_phase_end(self):
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                self.assertEqual(validator.call_count, 1)
            self.assertEqual(validator.call_count, 2)

    def test_semantic_replay_rejects_file_changed_to_directory(self):
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            def attempted():
                with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                    mutations._semantics(graph, ref, context, adapters)
                    mutations._semantics(graph, ref, context, adapters)
                    self.assertEqual(validator.call_count, 1)
                    self.rows["A"]["path"].unlink()
                    self.rows["A"]["path"].mkdir()
            self.assert_conflict(attempted)
            self.assertEqual(validator.call_count, 2)

    def test_decline_cannot_discard_a_coalesced_semantic_obligation(self):
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            def attempted():
                with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                    mutations._semantics(graph, ref, context, adapters)
                    mutations._semantics(graph, ref, context, adapters)
                    self.assertEqual(validator.call_count, 1)
                    self.db.execute(text("SELECT 1"))  # Allowed, but DECLINEs reuse.
                    self.rows["A"]["path"].unlink()
                    self.rows["A"]["path"].mkdir()
                    # No later lookup can be relied on to notice the change.
            self.assert_conflict(attempted)
            self.assertEqual(validator.call_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX contained-symlink canonicalization control")
    def test_semantic_replay_rejects_new_canonical_path_outside_held_batch(self):
        q = self.rows["A"]["path"].with_name("other.png")
        q.write_bytes(b"R4 second owned media")
        self.db.add(ContentItem(client_id=self.ids["A"]["client"], name="R4 other",
            type=ContentType.IMAGE, file_path=f"/media/{self.ids['A']['client']}/other.png"))
        self.db.commit()
        def attempted():
            with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                p = self.rows["A"]["path"]
                p.unlink();p.symlink_to(q)
                canonical = resolve_stored_media_path(self.root,
                    f"/media/{self.ids['A']['client']}/owned.png", must_exist=False)
                self.assertEqual(canonical, q)
                self.assertNotIn(q, self.db.info["media_reference_operation"]["paths"])
        self.assert_conflict(attempted)

    def test_semantic_replay_preserves_existing_missing_file_contract(self):
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                self.rows["A"]["path"].unlink()
            # The original validator uses must_exist=False; replay must not
            # silently introduce a new missing-file rejection policy.
            self.assertEqual(validator.call_count, 2)

    def test_discovery_replays_and_custom_or_changed_proposals_are_not_reused(self):
        for mode in ("discovery", "custom", "proposed"):
            with self.subTest(mode=mode), \
                    patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
                custom_calls = []
                def custom(*args):
                    custom_calls.append(True)
                    return True
                with builtin_semantic_phase(self, confirmed=mode != "discovery") as (graph, ref, context, adapters):
                    view = graph
                    if mode == "custom":
                        adapters = (mutations.SemanticAdapter("ContentItem", custom),)
                    elif mode == "proposed":
                        previous = graph.snapshot(ref)
                        proposal = mutations.ProposedState(ref, previous,
                            freeze(dict(previous, name="R4 proposed name")), frozenset({"name"}))
                        view = mutations._ProposedGraph(graph, (proposal,))
                    mutations._semantics(view, ref, context, adapters)
                    mutations._semantics(view, ref, context, adapters)
                    if mode == "discovery":
                        self.assertEqual(validator.call_count, 1)
                if mode == "custom":
                    self.assertEqual(custom_calls, [True, True])
                    self.assertEqual(validator.call_count, 0)
                else:
                    self.assertEqual(validator.call_count, 2)

    def test_discovery_end_rejects_file_changed_to_directory(self):
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            def attempted():
                with builtin_semantic_phase(self, confirmed=False) as (graph, ref, context, adapters):
                    mutations._semantics(graph, ref, context, adapters)
                    mutations._semantics(graph, ref, context, adapters)
                    self.assertEqual(validator.call_count, 1)
                    self.rows["A"]["path"].unlink();self.rows["A"]["path"].mkdir()
            self.assert_conflict(attempted)
            self.assertEqual(validator.call_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX contained-symlink canonicalization control")
    def test_discovery_end_retains_union_of_old_and_new_canonical_paths(self):
        p = self.rows["A"]["path"]
        q = p.with_name("other.png");q.write_bytes(b"R4 synthetic canonical Q")
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            with builtin_semantic_phase(self, confirmed=False) as (graph, ref, context, adapters):
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                self.assertEqual(validator.call_count, 1)
                self.assertEqual(graph.media_paths, {p})
                p.unlink();p.symlink_to(q)
            self.assertEqual(validator.call_count, 2)
            self.assertEqual(graph.media_paths, {p, q})

    @unittest.skipIf(os.name == "nt", "POSIX contained-symlink canonicalization control")
    def test_fresh_confirmation_rejects_a_changed_discovery_path_set(self):
        self.history(1)
        p = self.rows["A"]["path"]
        q = p.with_name("other.png");q.write_bytes(b"R4 second owned media")
        self.db.add(ContentItem(client_id=self.ids["A"]["client"], name="R4 other",
            type=ContentType.IMAGE, file_path=f"/media/{self.ids['A']['client']}/other.png"))
        self.db.commit()
        real = mutations.preflight_relationship_graph
        real_lock = media.lock_media_paths
        phases, batches = [], []
        def changed(gate, *args, **kwargs):
            phases.append(gate.graph.media_validation_locked)
            plan = real(gate, *args, **kwargs)
            if not gate.graph.media_validation_locked:
                p.unlink();p.symlink_to(q)
            return plan
        def locked(db, paths):
            batches.append(set(paths))
            return real_lock(db, paths)
        def attempted():
            with self.channel_guard(self.db):
                self.fail("Changed discovery paths bypassed fresh confirmation")
        with patch.object(mutations, "preflight_relationship_graph", changed), \
                patch.object(media, "lock_media_paths", locked):
            self.assert_conflict(attempted)
        self.assertEqual(phases, [False, True])
        self.assertEqual(batches, [{p, q}])
        self.db.rollback()

    def test_phase_stage_cannot_be_changed_to_bypass_end_confirmation(self):
        captured = []
        def attempted():
            with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                captured.append(graph)
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                graph.media_validation_locked = False
                # No subsequent lookup: END must independently reject the flip.
        try:
            self.assert_conflict(attempted)
        finally:
            if captured:
                captured[0].media_validation_locked = True

    def test_owner_cache_hit_does_not_repeat_the_locked_chain_walk(self):
        with builtin_semantic_phase(self) as (graph, ref, context, adapters):
            memo = graph._validation_memo
            with patch.object(memo, "owner_chain_locked", wraps=memo.owner_chain_locked) as chains:
                for _ in range(3):
                    self.assertEqual(graph.resolve_owner(ref).client_id, self.ids["A"]["client"])
                subject_calls = [call for call in chains.call_args_list if call.args[1] == ref]
                self.assertEqual(len(subject_calls), 1)

    def test_gate_binding_changes_are_rejected_at_end_without_another_lookup(self):
        for field in ("active", "token", "graph", "transaction"):
            with self.subTest(field=field):
                captured = []
                def attempted():
                    with builtin_semantic_phase(self) as (graph, ref, context, adapters):
                        self.assertEqual(graph.resolve_owner(ref).client_id, self.ids["A"]["client"])
                        gate = graph._validation_memo.gate
                        captured.append((gate, getattr(gate, field)))
                        setattr(gate, field, False if field == "active" else object())
                try:
                    self.assert_conflict(attempted)
                finally:
                    if captured and field != "active":
                        setattr(captured[0][0], field, captured[0][1])
                with self.channel_guard(self.db):
                    pass
                self.db.rollback()

    def test_scalar_builtin_catalog_does_not_resolve_an_unused_owner(self):
        graph = mutations._WriteGraph(self.db)
        context = context_for_actor(self.db, self.db.get(User, self.ids["A"]["user"]))
        scalar_only = ("Channel", "DataSource", "DatasetColumn", "DatasetRow",
                       "PlayerDevice", "PlayerHeartbeat", "PlayerPlaybackEvent", "User")
        with patch.object(graph, "resolve_owner", side_effect=AssertionError("Unused scalar owner lookup")) as owners:
            for model in scalar_only:
                with self.subTest(model=model):
                    self.assertIs(mutations._write_semantics(graph, RowRef(model, "R4-synthetic-scalar"), context, {}), True)
            owners.assert_not_called()

    def test_dataset_import_builtin_replay_uses_the_same_narrow_boundary(self):
        source = DataSource(client_id=self.ids["A"]["client"], name="R4 CSV", source_type=DataSourceType.FILE_UPLOAD)
        self.db.add(source);self.db.flush()
        dataset = Dataset(client_id=self.ids["A"]["client"], data_source_id=source.id, name="R4 CSV", slug="r4-csv")
        self.db.add(dataset);self.db.flush()
        stored = f"datasets/{self.ids['A']['client']}/{dataset.id}/fixture.csv"
        physical = self.root / stored
        physical.parent.mkdir(parents=True);physical.write_bytes(b"value\nsynthetic\n")
        imported = DatasetImport(dataset_id=dataset.id, source_filename="fixture.csv", storage_path=stored)
        self.db.add(imported);self.db.commit()
        ref = RowRef("DatasetImport", imported.id)
        with patch.object(mutations, "_write_semantics", wraps=mutations._write_semantics) as validator:
            with builtin_semantic_phase(self, ref=ref, path=physical) as (graph, ref, context, adapters):
                mutations._semantics(graph, ref, context, adapters)
                mutations._semantics(graph, ref, context, adapters)
                self.assertEqual(validator.call_count, 1)
            self.assertEqual(validator.call_count, 2)

    def test_guard_preserves_both_passes_and_dependency_proofs(self):
        self.history(2)
        phases = []
        real = mutations.preflight_relationship_graph
        def observed(gate, *args, **kwargs):
            plan = real(gate, *args, **kwargs)
            phases.append((gate.graph.media_validation_locked, plan.checked, plan.dependencies))
            return plan
        with patch.object(mutations, "preflight_relationship_graph", observed), \
                patch.object(media, "media_reference_owners", wraps=media.media_reference_owners) as builds:
            with self.channel_guard(self.db):
                pass
            self.assertEqual([phase[0] for phase in phases], [False, True])
            self.assertEqual(phases[0][1:], phases[1][1:])
            self.assertGreater(builds.call_count, 0)
            self.assertLessEqual(builds.call_count, 2)
        self.db.rollback()

    def test_foreign_incoming_relation_still_denies_before_mutation(self):
        a, b = self.ids["A"], self.ids["B"]
        self.db.add(PlayerHeartbeat(player_device_id=b["device"], channel_id=a["channel"],
            client_id=b["client"], branch_id=b["branch"], current_content_id=b["content"]))
        self.db.commit()
        def attempt():
            with self.channel_guard(self.db):
                self.fail("Foreign incoming relation reached the writer")
        self.assert_conflict(attempt)
        self.db.rollback()
        self.assertEqual(self.db.get(Channel, a["channel"]).name, "A")


@unittest.skipUnless(os.environ.get("SECURITY_R4_TEST_DATABASE_URL"), "Isolated local PostgreSQL R4 URL required")
class TenantGatePostgresTests(GateFixture):
    def make_engine(self):
        url = make_url(os.environ["SECURITY_R4_TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or url.database != "security_r4_qa" or url.query.get("options")):
            raise RuntimeError("R4 requires its explicit isolated localhost test database")
        expected = os.environ.get("SECURITY_R4_PG_SYSTEM_IDENTIFIER")
        marker = os.environ.get("QA_EXPECT_PG_MARKER")
        if not expected and not marker:
            raise RuntimeError("R4 requires the owned fixture system identifier or worker marker")
        schema = "r4_" + uuid.uuid4().hex
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True)
        with engine.begin() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_database()")), url.database)
            if expected:
                self.assertEqual(connection.scalar(text("SELECT system_identifier::text FROM pg_control_system()")), expected)
            if marker:
                self.assertEqual(connection.scalar(text("SELECT current_setting('vwflex.qa_worker',true)")), marker)
            connection.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            engine.dispose()
        self.addCleanup(cleanup)
        return engine

    def test_postgres_prepopulated_unlocked_snapshot_declines_graph_fact_reuse(self):
        OwnershipIndexPhaseTests.test_prepopulated_unlocked_snapshot_declines_graph_fact_reuse(self)

    def test_postgres_unlocked_ancestor_declines_phase_for_locked_subject(self):
        OwnershipIndexPhaseTests.test_unlocked_ancestor_declines_the_phase_even_for_a_locked_subject(self)

    def test_postgres_canonical_context_snapshot_reuse_and_custom_snapshot_safety(self):
        OwnershipIndexPhaseTests.test_canonical_context_snapshot_reuses_but_custom_snapshot_never_does(self)

    def test_postgres_semantic_replay_rejects_file_changed_to_directory(self):
        OwnershipIndexPhaseTests.test_semantic_replay_rejects_file_changed_to_directory(self)

    def test_postgres_decline_keeps_coalesced_semantic_obligation(self):
        OwnershipIndexPhaseTests.test_decline_cannot_discard_a_coalesced_semantic_obligation(self)

    @unittest.skipIf(os.name == "nt", "POSIX contained-symlink canonicalization control")
    def test_postgres_fresh_confirmation_rejects_a_changed_discovery_path_set(self):
        OwnershipIndexPhaseTests.test_fresh_confirmation_rejects_a_changed_discovery_path_set(self)

    def test_postgres_owner_cache_hit_does_not_repeat_the_locked_chain_walk(self):
        OwnershipIndexPhaseTests.test_owner_cache_hit_does_not_repeat_the_locked_chain_walk(self)

    def test_same_tenant_heartbeat_and_login_finish_without_recomputation_timeout(self):
        """Same fixture/cost against old source must reproduce real 55P03/409."""
        self.history(48)
        first_build = threading.Event()
        calls, unlocked_calls, sqlstates = [], [], []
        raw_builder = media.media_reference_owners
        def controlled_build(db, root, *, strict=False):
            state = db.info.get("media_reference_operation")
            if state is not None and state.get("paths"):
                # The existing coordinator is entered only after the tenant
                # gate and discovery pass. Read projections cannot trigger it.
                calls.append(threading.get_ident())
                first_build.set()
                # Real latency per redundant protected build; no fake HTTP/lock.
                db.execute(select(func.pg_sleep(0.3)))
            else:
                unlocked_calls.append(threading.get_ident())
            return raw_builder(db, root, strict=strict)
        def database_error(context):
            state = getattr(context.original_exception, "sqlstate", None)
            if state:
                sqlstates.append(state)
        event.listen(self.engine, "handle_error", database_error)
        started = time.monotonic()
        try:
            with patch.object(media, "media_reference_owners", controlled_build), ThreadPoolExecutor(max_workers=2) as pool:
                heartbeat = pool.submit(self.heartbeat)
                self.assertTrue(first_build.wait(15), "Fixture never reached ownership validation")
                login_started = time.monotonic()
                login = pool.submit(self.login)
                login_response = login.result(timeout=20)
                login_ms = (time.monotonic()-login_started)*1000
                heartbeat_response = heartbeat.result(timeout=90)
        finally:
            event.remove(self.engine, "handle_error", database_error)
        observation = dict(case="controlled_recomputation", heartbeat_status=heartbeat_response.status_code,
            login_status=login_response.status_code, ownership_builds=len(calls), SQLSTATEs=sqlstates,
            unlocked_ownership_builds=len(unlocked_calls),login_latency_ms=login_ms,
            build_cost_ms=300, history_rows=48, elapsed_ms=(time.monotonic()-started)*1000)
        R4_GATE_OBSERVATIONS.append(observation)
        self.assertEqual(heartbeat_response.status_code, 200)
        self.assertEqual(login_response.status_code, 200)
        self.assertNotIn("55P03", sqlstates)
        self.assertLessEqual(len(calls), 2)

    def test_real_tenant_lock_held_for_ten_seconds_still_returns_409(self):
        key = int.from_bytes(hashlib.sha256(("tecno.tenant.write.v1:" + self.ids["A"]["client"]).encode()).digest()[:8], "big", signed=True)
        states = []
        def error(context):
            state = getattr(context.original_exception, "sqlstate", None)
            if state:
                states.append(state)
        event.listen(self.engine, "handle_error", error)
        try:
            with Session(self.engine) as holder:
                holder.execute(select(func.pg_advisory_xact_lock(key)))
                started = time.monotonic();response = self.login();elapsed = time.monotonic()-started
                holder.rollback()
        finally:
            event.remove(self.engine, "handle_error", error)
        R4_GATE_OBSERVATIONS.append(dict(case="real_held_lock", status=response.status_code,
            elapsed_ms=elapsed*1000, SQLSTATEs=states))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json().get("detail"), "TENANT_RELATION_CONFLICT")
        self.assertIn("55P03", states)
        self.assertGreaterEqual(elapsed, 9.5)
        self.assertLess(elapsed, 15)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(User, self.ids["A"]["user"]).last_login_at)

    def test_relevant_row_lock_is_waited_for_without_false_conflict(self):
        ready, release, attempted = threading.Event(), threading.Event(), threading.Event()
        branch_id = self.ids["A"]["branch"]
        snapshot = mutations._WriteGraph.snapshot
        def observed(graph, row):
            ref = graph.reference(row)
            if ref == RowRef("Branch", branch_id):
                attempted.set()
            return snapshot(graph, row)
        def blocker():
            with Session(self.engine) as db:
                db.execute(select(Branch).where(Branch.id == branch_id).with_for_update())
                ready.set()
                if not release.wait(8):
                    raise AssertionError("Controller did not release the row lock")
                db.rollback()
        def legitimate():
            with Session(self.engine) as db:
                with self.channel_guard(db):
                    db.get(Channel, self.ids["A"]["channel"]).name = "R4 legitimate"
                    db.commit()
            return "ALLOWED"
        with patch.object(mutations._WriteGraph, "snapshot", observed), ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(blocker)
            self.assertTrue(ready.wait(5))
            writer = pool.submit(legitimate)
            try:
                self.assertTrue(attempted.wait(5))
                self.assertFalse(writer.done(), "Relevant row lock was skipped")
            finally:
                release.set()
            self.assertEqual(writer.result(timeout=8), "ALLOWED")
            holder.result(timeout=5)
        self.db.expire_all()
        self.assertEqual(self.db.get(Channel, self.ids["A"]["channel"]).name, "R4 legitimate")

    def test_concurrent_foreign_tenant_attempt_is_rejected(self):
        def foreign():
            with Session(self.engine) as db:
                with self.assertRaises(HTTPException) as caught:
                    with self.channel_guard(db, actor_side="B"):
                        self.fail("Foreign actor reached an A writer")
                self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                 (409, "TENANT_RELATION_CONFLICT"))
                db.rollback()
                return "DENIED"
        with self.channel_guard(self.db), ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(foreign).result(timeout=5), "DENIED")
        self.db.rollback()
        self.assertEqual(self.db.get(Channel, self.ids["A"]["channel"]).name, "A")

    def test_new_authoritative_ambiguity_between_passes_cannot_use_stale_index(self):
        self.history(1)
        real = mutations.preflight_relationship_graph
        real_lock = media.lock_media_paths
        phases, alias_ids = [], []
        def observed(gate, *args, **kwargs):
            phases.append(gate.graph.media_validation_locked)
            return real(gate, *args, **kwargs)
        def between(db, paths):
            if not alias_ids:
                # An independent, deliberately uncooperative writer commits a
                # new ambiguous reference after phase1 and before media locks.
                # It does not update an already row-locked owner/ancestor.
                with Session(self.engine) as other:
                    alias = ContentItem(client_id=self.ids["B"]["client"], name="R4 foreign alias",
                        type=ContentType.IMAGE, file_path=self.rows["A"]["content"].file_path)
                    other.add(alias);other.commit();alias_ids.append(alias.id)
            return real_lock(db, paths)
        def attempted():
            with self.channel_guard(self.db):
                self.fail("Ambiguous authoritative media reached the writer")
        with patch.object(mutations, "preflight_relationship_graph", observed), \
                patch.object(media, "lock_media_paths", between):
            self.assert_conflict(attempted)
        self.assertEqual(phases, [False, True])
        self.assertEqual(len(alias_ids), 1)
        self.db.rollback()
        self.assertFalse(self.owned())
        self.assertEqual(self.db.get(ContentItem, self.ids["B"]["content"]).file_path,
                         f"/media/{self.ids['B']['client']}/owned.png")

    def blocked_external_writer(self, action):
        def contender():
            with Session(self.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    action(db)
                    db.commit()
                except OperationalError as error:
                    self.assertEqual(getattr(error.orig, "sqlstate", None), "55P03")
                    db.rollback()
                    return "BLOCKED"
                self.fail("Concurrent writer bypassed the real row/FK lock")
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(contender).result(timeout=5), "BLOCKED")

    def test_delete_update_race_keeps_locked_target_and_legitimate_update(self):
        with self.channel_guard(self.db):
            self.blocked_external_writer(lambda db: db.execute(delete(Channel)
                .where(Channel.id == self.ids["A"]["channel"])))
            self.db.get(Channel, self.ids["A"]["channel"]).name = "R4 update won"
            self.db.commit()
        self.db.expire_all()
        channel = self.db.get(Channel, self.ids["A"]["channel"])
        self.assertIsNotNone(channel)
        self.assertEqual(channel.name, "R4 update won")

    def test_incoming_foreign_fk_change_is_blocked_by_the_parent_lock(self):
        b = self.ids["B"]
        heartbeat = PlayerHeartbeat(player_device_id=b["device"], channel_id=b["channel"],
            client_id=b["client"], branch_id=b["branch"], current_content_id=b["content"])
        self.db.add(heartbeat); self.db.commit()
        heartbeat_id = heartbeat.id
        with self.channel_guard(self.db):
            self.blocked_external_writer(lambda db: db.execute(update(PlayerHeartbeat)
                .where(PlayerHeartbeat.id == heartbeat_id).values(channel_id=self.ids["A"]["channel"])))
        self.db.rollback()
        self.assertEqual(self.db.get(PlayerHeartbeat, heartbeat_id).channel_id, b["channel"])

    def test_invalid_credentials_and_disabled_user_remain_rejected(self):
        self.assertEqual(self.login(password="R4-incorrect-synthetic").status_code, 401)
        self.db.execute(update(User).where(User.id == self.ids["A"]["user"])
                        .values(is_active=False, status="suspended"))
        self.db.commit()
        self.assertEqual(self.login().status_code, 403)

    def test_twenty_sequential_logins_have_no_false_409(self):
        latencies, statuses = [], []
        for _ in range(20):
            started = time.monotonic();response = self.login()
            latencies.append((time.monotonic()-started)*1000)
            statuses.append(response.status_code)
        ordered = sorted(latencies)
        R4_GATE_OBSERVATIONS.append(dict(case="twenty_local_logins", statuses=statuses,
            min_ms=ordered[0], median_ms=(ordered[9]+ordered[10])/2,
            p95_ms=ordered[18], max_ms=ordered[-1]))
        self.assertEqual(Counter(statuses), {200: 20})
