"""Exhaustive owner-only cleanup proofs over isolated SQLite history.

No application startup, Player activation, server or external database is used.
"""
from datetime import UTC, datetime
import os
from unittest import TestCase
from uuid import uuid4

if os.environ.get("DATABASE_URL") != "sqlite://" or os.environ.get("AUTO_SEED") != "false":
    raise RuntimeError("Only the isolated SQLite memory test configuration is allowed")

from fastapi import HTTPException
from sqlalchemy import create_engine, delete, event, func, insert, literal, select, union_all
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.channels import preflight_channel_delete
from app.api.routes.campaigns import preflight_campaign_delete
from app.models.base import Base
from app.models.entities import (
    Branch, Campaign, Channel, Client, ContentItem, ContentType,
    PlayerDevice, PlayerHeartbeat, PlayerPlaybackEvent,
)
from app.services.content_cleanup import preflight_content_relationships
from app.services.tenancy import assert_related_clients_query


HISTORY_SIZE = 513
OLD_TIME = datetime(2000, 1, 1, tzinfo=UTC)
MODELS = (PlayerHeartbeat, PlayerPlaybackEvent)


class TelemetryCleanupSafetyTests(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.assertEqual(self.engine.url.get_backend_name(), "sqlite")
        self.assertIn(self.engine.url.database, (None, "", ":memory:"))
        with self.engine.connect() as connection:
            self.assertTrue(all(row[2] == "" for row in connection.exec_driver_sql("PRAGMA database_list")))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False, expire_on_commit=False)
        self.client = Client(name="Cleanup A", slug=f"cleanup-a-{uuid4().hex}")
        self.foreign_client = Client(name="Cleanup B", slug=f"cleanup-b-{uuid4().hex}")
        self.db.add_all((self.client, self.foreign_client))
        self.db.flush()
        self.branch = Branch(client_id=self.client.id, name="A", code="A")
        self.foreign_branch = Branch(client_id=self.foreign_client.id, name="B", code="B")
        self.db.add_all((self.branch, self.foreign_branch))
        self.db.flush()
        self.channel = Channel(client_id=self.client.id, branch_id=self.branch.id, name="A")
        self.foreign_channel = Channel(client_id=self.foreign_client.id, branch_id=self.foreign_branch.id, name="B")
        self.campaign = Campaign(client_id=self.client.id, name="A")
        self.foreign_campaign = Campaign(client_id=self.foreign_client.id, name="B")
        self.content = ContentItem(client_id=self.client.id, name="A", type=ContentType.IMAGE)
        self.foreign_content = ContentItem(client_id=self.foreign_client.id, name="B", type=ContentType.IMAGE)
        self.db.add_all((self.channel, self.foreign_channel, self.campaign,
                         self.foreign_campaign, self.content, self.foreign_content))
        self.db.flush()
        self.device = PlayerDevice(channel_id=self.channel.id, client_id=self.client.id,
                                   branch_id=self.branch.id, hardware_id="synthetic-a",
                                   device_name="A", player_token_hash=f"synthetic-{uuid4().hex}")
        self.foreign_device = PlayerDevice(channel_id=self.foreign_channel.id, client_id=self.foreign_client.id,
                                           branch_id=self.foreign_branch.id, hardware_id="synthetic-b",
                                           device_name="B", player_token_hash=f"synthetic-{uuid4().hex}")
        self.db.add_all((self.device, self.foreign_device))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def seed(self, model, count, *, bad_index=None, unrelated=False, device_only=False):
        owner = self.foreign_client if unrelated else self.client
        branch = self.foreign_branch if unrelated else self.branch
        channel = self.foreign_channel if unrelated or device_only else self.channel
        device = self.foreign_device if unrelated else self.device
        campaign = self.foreign_campaign if unrelated else self.campaign
        content = self.foreign_content if unrelated else self.content
        values = []
        for index in range(count):
            row = dict(id=str(uuid4()), client_id=self.foreign_client.id if index == bad_index else owner.id,
                       branch_id=branch.id, channel_id=channel.id, player_device_id=device.id,
                       created_at=OLD_TIME, updated_at=OLD_TIME)
            if model is PlayerHeartbeat:
                row.update(current_campaign_id=campaign.id, current_content_id=content.id, received_at=OLD_TIME)
            else:
                row.update(campaign_id=campaign.id, content_id=content.id, started_at=OLD_TIME)
            values.append(row)
        self.db.execute(insert(model), values)
        self.db.commit()

    def clear_history(self):
        for model in MODELS:
            self.db.execute(delete(model))
        self.db.commit()

    def invoke(self, surface):
        if surface == "channel":
            preflight_channel_delete(self.db, self.channel)
        elif surface == "campaign":
            preflight_campaign_delete(self.db, self.campaign)
        else:
            self.assertEqual(surface, "content")
            preflight_content_relationships([self.content], self.db)

    def history_state(self):
        return tuple((self.db.scalar(select(func.count()).select_from(model)),
                      self.db.scalar(select(func.count()).select_from(model)
                                     .where(model.client_id == self.foreign_client.id))) for model in MODELS)

    def check_legitimate(self, surface):
        for model in MODELS:
            self.seed(model, HISTORY_SIZE)
            self.seed(model, 3, unrelated=True)
        before = self.history_state()
        queries = []

        def capture(_, __, statement, ___, ____, _____):
            text = statement.lower()
            if "from player_heartbeats" in text or "from player_playback_events" in text:
                queries.append(text)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.invoke(surface)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(queries), 2, queries)
        for query in queries:
            self.assertIn("exists (", query)
            self.assertNotIn("limit", query)
        self.assertEqual(self.history_state(), before)
        self.assertFalse(any(isinstance(row, MODELS) for row in self.db.identity_map.values()))

    def check_deep_foreign(self, surface):
        for model in MODELS:
            for position in (0, HISTORY_SIZE // 2, HISTORY_SIZE - 1):
                with self.subTest(model=model.__name__, position=position):
                    self.clear_history()
                    self.seed(model, HISTORY_SIZE, bad_index=position)
                    other = PlayerPlaybackEvent if model is PlayerHeartbeat else PlayerHeartbeat
                    self.seed(other, HISTORY_SIZE)
                    before = self.history_state()
                    with self.assertRaises(HTTPException) as caught:
                        self.invoke(surface)
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")
                    self.assertEqual(self.history_state(), before)

    def test_channel_legitimate_history_uses_scalar_exists(self):
        self.check_legitimate("channel")

    def test_campaign_legitimate_history_uses_scalar_exists(self):
        self.check_legitimate("campaign")

    def test_content_legitimate_history_uses_scalar_exists(self):
        self.check_legitimate("content")

    def test_channel_foreign_first_middle_and_last_old_rows_block(self):
        self.check_deep_foreign("channel")

    def test_campaign_foreign_first_middle_and_last_old_rows_block(self):
        self.check_deep_foreign("campaign")

    def test_content_foreign_first_middle_and_last_old_rows_block(self):
        self.check_deep_foreign("content")

    def test_channel_device_only_foreign_reference_still_blocks(self):
        # The OR arm through PlayerDevice must not disappear in the SQL rewrite.
        for model in MODELS:
            with self.subTest(model=model.__name__):
                self.clear_history()
                self.seed(model, HISTORY_SIZE, bad_index=HISTORY_SIZE - 1, device_only=True)
                with self.assertRaises(HTTPException) as caught:
                    self.invoke("channel")
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_owner_query_rejects_null_and_foreign_without_filtering_them(self):
        for bad_owner in (None, self.foreign_client.id):
            with self.subTest(owner_is_null=bad_owner is None):
                statement = union_all(select(literal(self.client.id).label("client_id")),
                                      select(literal(bad_owner).label("client_id")))
                with self.assertRaises(HTTPException) as caught:
                    assert_related_clients_query(self.client.id, self.db, statement)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail, "TENANT_RELATION_CONFLICT")

    def test_owner_query_accepts_empty_or_matching_rows_and_rejects_empty_scope(self):
        statement = select(PlayerHeartbeat.client_id).where(PlayerHeartbeat.channel_id == self.channel.id)
        assert_related_clients_query(self.client.id, self.db, statement)
        assert_related_clients_query(self.client.id, self.db, select(literal(self.client.id)))
        with self.assertRaises(HTTPException) as caught:
            assert_related_clients_query("", self.db, statement)
        self.assertEqual(caught.exception.status_code, 409)

    def test_owner_query_rejects_a_non_owner_projection(self):
        with self.assertRaises(HTTPException) as caught:
            assert_related_clients_query(self.client.id, self.db,
                                         select(PlayerHeartbeat.client_id, PlayerHeartbeat.channel_id))
        self.assertEqual(caught.exception.status_code, 409)
