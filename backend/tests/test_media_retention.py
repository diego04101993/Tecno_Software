from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import os
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.api.routes.player import get_runtime_config
from app.models.base import Base
from app.models.entities import (
    Branch,
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaylistItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    Channel,
    Client,
    ContentItem,
    ContentType,
    Layout,
    Schedule,
)
from app.services.media_retention import RetentionIncomplete, build_media_retention, incomplete_retention
from app.services.player_runtime import build_player_runtime_config, build_player_runtime_from_snapshot


NOW = datetime(2026, 6, 16, 18, 0, tzinfo=UTC)
BASE = "https://api.example.test/"


class ReverseScalars:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, *args):
        return self.db.get(*args)

    def scalars(self, statement):
        return reversed(list(self.db.scalars(statement)))


class MediaRetentionTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.client = Client(name="Test", slug="retention-test")
        self.db.add(self.client)
        self.db.flush()
        self.branch = Branch(client_id=self.client.id, name="Branch", code="B1")
        self.db.add(self.branch)
        self.db.flush()
        self.channel = Channel(client_id=self.client.id, branch_id=self.branch.id, name="Screen")
        self.db.add(self.channel)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def _campaign(self, name: str, files: list[str] | None = None) -> Campaign:
        campaign = Campaign(client_id=self.client.id, name=name)
        self.db.add(campaign)
        self.db.flush()
        for order, filename in enumerate(files or [], 1):
            content = ContentItem(
                client_id=self.client.id,
                name=filename,
                type=ContentType.IMAGE,
                file_path=f"/media/{self.client.id}/{filename}",
            )
            self.db.add(content)
            self.db.flush()
            self.db.add(CampaignSequenceItem(
                campaign_id=campaign.id,
                item_type=CampaignSequenceItemType.CONTENT,
                content_id=content.id,
                sort_order=order,
                is_enabled=True,
            ))
        self.db.flush()
        return campaign

    def _retention(self, selected: Campaign | None = None, at: datetime = NOW):
        return build_media_retention(self.channel, self.db, BASE, selected, at)

    def test_empty_channel_is_complete_and_empty(self) -> None:
        result = self._retention()
        self.assertTrue(result["complete"])
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["protected_campaign_ids"], [])

    def test_selected_campaign_keeps_all_images(self) -> None:
        campaign = self._campaign("Selected", ["a.jpg", "b.jpg", "c.jpg"])
        result = self._retention(campaign)
        self.assertEqual(result["protected_campaign_ids"], [campaign.id])
        self.assertEqual(len(result["assets"]), 3)
        self.assertTrue(all(asset["download_url"].startswith(BASE + "media/") for asset in result["assets"]))

    def test_future_and_lower_priority_schedules_are_protected(self) -> None:
        current = self._campaign("Current", ["current.jpg"])
        future = self._campaign("Future", ["future.jpg"])
        lower = self._campaign("Lower", ["lower.jpg"])
        self.db.add_all([
            Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=current.id, title="Now", priority=200),
            Schedule(client_id=self.client.id, branch_id=self.branch.id, campaign_id=future.id, title="Future", starts_on=date(2026, 12, 1)),
            Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=lower.id, title="Low", priority=1),
        ])
        self.db.flush()
        result = self._retention(current)
        self.assertEqual(set(result["protected_campaign_ids"]), {current.id, future.id, lower.id})
        self.assertEqual(len(result["assets"]), 3)

    def test_future_assignment_and_fallback_after_schedule(self) -> None:
        scheduled = self._campaign("Scheduled", ["scheduled.jpg"])
        base = self._campaign("Base", ["base.jpg"])
        later = self._campaign("Later", ["later.jpg"])
        self.db.add_all([
            Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=scheduled.id, title="Today", ends_on=date(2026, 6, 16)),
            CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=base.id),
            CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=later.id, active_from=NOW + timedelta(days=2)),
        ])
        self.db.flush()
        result = self._retention(scheduled)
        self.assertEqual(set(result["protected_campaign_ids"]), {scheduled.id, base.id, later.id})

    def test_cancelled_and_expired_schedule_not_protected(self) -> None:
        cancelled = self._campaign("Cancelled", ["cancelled.jpg"])
        expired = self._campaign("Expired", ["expired.jpg"])
        self.db.add_all([
            Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=cancelled.id, title="Cancelled", is_active=False),
            Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=expired.id, title="Expired", ends_on=date(2026, 6, 1)),
        ])
        self.db.flush()
        self.assertEqual(self._retention()["assets"], [])
        self.db.add(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=cancelled.id))
        self.db.flush()
        self.assertEqual(self._retention()["protected_campaign_ids"], [cancelled.id])

    def test_previous_assignment_remains_eligible_when_another_is_appended(self) -> None:
        first = self._campaign("A", ["a.jpg"])
        second = self._campaign("B", ["b.jpg"])
        self.db.add(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=first.id))
        self.db.flush()
        self.db.add(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=second.id))
        self.db.flush()
        self.assertEqual(set(self._retention()["protected_campaign_ids"]), {first.id, second.id})
        self.assertEqual(len(self._retention()["assets"]), 2)

    def test_shared_file_keeps_content_id_aliases(self) -> None:
        first = self._campaign("A", ["shared.jpg"])
        second = self._campaign("B", ["shared.jpg"])
        self.db.add_all([
            CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=first.id),
            CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=second.id),
        ])
        self.db.flush()
        assets = self._retention()["assets"]
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["file_path"], assets[1]["file_path"])
        self.assertNotEqual(assets[0]["content_id"], assets[1]["content_id"])

    def test_legacy_playlist_fallback(self) -> None:
        campaign = Campaign(client_id=self.client.id, name="Legacy")
        content = ContentItem(client_id=self.client.id, name="old", type=ContentType.IMAGE, file_path=f"/media/{self.client.id}/old.jpg")
        self.db.add_all([campaign, content])
        self.db.flush()
        self.db.add(CampaignPlaylistItem(campaign_id=campaign.id, content_id=content.id, sort_order=1))
        self.db.flush()
        self.assertEqual(self._retention(campaign)["assets"][0]["content_id"], content.id)

    def test_unknown_layout_or_hidden_media_is_incomplete(self) -> None:
        campaign = self._campaign("Layout", ["a.jpg"])
        layout = Layout(client_id=self.client.id, name="Background")
        self.db.add(layout)
        self.db.flush()
        campaign.layout_id = layout.id
        self.db.flush()
        with self.assertRaises(RetentionIncomplete) as error:
            self._retention(campaign)
        self.assertEqual(error.exception.reason, "unknown_media_reference")

    def test_audio_or_touch_assignment_on_normal_channel_is_incomplete(self) -> None:
        with self.assertRaises(RetentionIncomplete) as error:
            build_media_retention(self.channel, self.db, BASE, None, NOW, unknown_runtime_media=True)
        self.assertEqual(error.exception.reason, "unknown_media_reference")

    def test_hidden_media_in_content_metadata_is_incomplete(self) -> None:
        campaign = self._campaign("Hidden", ["a.jpg"])
        item = self.db.scalar(select(CampaignSequenceItem).where(CampaignSequenceItem.campaign_id == campaign.id))
        content = self.db.get(ContentItem, item.content_id)
        content.metadata_json = {"background": f"/media/{self.client.id}/unlisted.jpg"}
        self.db.flush()
        with self.assertRaises(RetentionIncomplete) as error:
            self._retention(campaign)
        self.assertEqual(error.exception.reason, "unknown_media_reference")
        content.metadata_json = {"background": "media/unlisted.jpg"}
        self.db.flush()
        with self.assertRaises(RetentionIncomplete):
            self._retention(campaign)

    def test_cross_tenant_reference_fails_without_leak(self) -> None:
        other = Client(name="Other", slug="other")
        self.db.add(other)
        self.db.flush()
        campaign = Campaign(client_id=other.id, name="Secret")
        self.db.add(campaign)
        self.db.flush()
        self.db.add(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=campaign.id))
        self.db.flush()
        with self.assertRaises(RetentionIncomplete) as error:
            self._retention()
        self.assertEqual(error.exception.reason, "cross_tenant_reference")

    def test_player_channel_scope_mismatch_is_rejected(self) -> None:
        other = Client(name="Other", slug="other-player")
        self.db.add(other)
        self.db.flush()
        fake_device = SimpleNamespace(channel_id=self.channel.id, client_id=other.id, branch_id=self.branch.id)
        request = Request({"type": "http", "scheme": "http", "server": ("localhost", 8000), "path": "/api/player/runtime-config", "headers": []})
        with self.assertRaises(HTTPException) as error:
            get_runtime_config(request, player_device=fake_device, db=self.db)
        self.assertEqual(error.exception.status_code, 404)

    def test_revision_is_order_and_request_time_independent(self) -> None:
        campaign = self._campaign("Stable", ["a.jpg", "b.jpg"])
        first = self._retention(campaign)
        second = build_media_retention(self.channel, ReverseScalars(self.db), "https://another-host.test/", campaign, NOW + timedelta(seconds=1))
        self.assertEqual(first["revision"], second["revision"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])

    def test_revision_changes_when_rule_changes(self) -> None:
        campaign = self._campaign("Rule", ["a.jpg"])
        schedule = Schedule(client_id=self.client.id, channel_id=self.channel.id, campaign_id=campaign.id, title="Rule", priority=1)
        self.db.add(schedule)
        self.db.flush()
        original = self._retention()["revision"]
        schedule.priority = 2
        self.db.flush()
        self.assertNotEqual(original, self._retention()["revision"])

    def test_internal_error_never_returns_complete_empty(self) -> None:
        with patch("app.services.player_runtime.build_media_retention", side_effect=RuntimeError("internal")):
            runtime = build_player_runtime_config(self.channel, self.db, BASE, snapshot_coherent=True)
        retention = runtime["media_retention"]
        self.assertFalse(retention["complete"])
        self.assertIsNone(retention["revision"])
        self.assertEqual(retention["assets"], [])
        self.assertEqual(retention["reason"], "internal_error")
        self.assertEqual(runtime["playlist"], [])

    def test_existing_runtime_fields_unchanged_by_extension(self) -> None:
        with patch("app.services.player_runtime.now_utc", return_value=NOW):
            previous = build_player_runtime_config(self.channel, self.db, BASE)
            complete = build_player_runtime_config(self.channel, self.db, BASE, snapshot_coherent=True)
        previous.pop("media_retention")
        complete.pop("media_retention")
        self.assertEqual(previous, complete)

    def test_three_image_runtime_playlist_and_contents_are_unchanged(self) -> None:
        campaign = self._campaign("Three", ["a.jpg", "b.jpg", "c.jpg"])
        self.db.add(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=campaign.id))
        self.db.flush()
        with patch("app.services.player_runtime.now_utc", return_value=NOW):
            previous = build_player_runtime_config(self.channel, self.db, BASE)
            complete = build_player_runtime_config(self.channel, self.db, BASE, snapshot_coherent=True)
        self.assertEqual(len(previous["playlist"]), 3)
        self.assertEqual(len(previous["contents"]), 3)
        self.assertEqual(len(complete["media_retention"]["assets"]), 3)
        self.assertEqual(complete["media_retention"]["protected_campaign_ids"], [campaign.id])
        previous.pop("media_retention")
        complete.pop("media_retention")
        self.assertEqual(previous, complete)

    def test_incomplete_schema_is_safe(self) -> None:
        result = incomplete_retention(self.channel.id, NOW, "snapshot_unavailable")
        self.assertFalse(result["complete"])
        self.assertEqual(result["protected_campaign_ids"], [])
        self.assertIsNone(result["revision"])

    def test_batched_enumeration_metrics(self) -> None:
        campaigns = [self._campaign(f"Bulk-{index}", [f"{index}-{item}.jpg" for item in range(50)]) for index in range(3)]
        self.db.add_all(CampaignChannelAssignment(channel_id=self.channel.id, campaign_id=campaign.id) for campaign in campaigns)
        self.db.flush()
        query_count = 0

        def count_query(*_args) -> None:
            nonlocal query_count
            query_count += 1

        event.listen(self.engine, "before_cursor_execute", count_query)
        try:
            started = perf_counter()
            result = self._retention()
            elapsed_ms = (perf_counter() - started) * 1000
        finally:
            event.remove(self.engine, "before_cursor_execute", count_query)
        response_bytes = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(len(result["assets"]), 150)
        self.assertLessEqual(query_count, 10)
        print(f"retention fixture: assets=150 queries={query_count} bytes={response_bytes} elapsed_ms={elapsed_ms:.1f}")


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to an isolated local PostgreSQL test database")
class PostgreSQLSnapshotTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = make_url(TEST_DATABASE_URL)
        if url.get_backend_name() != "postgresql" or url.database != "tecno_control_retention_test" or url.host not in {"localhost", "127.0.0.1", "postgres"}:
            raise RuntimeError("Refusing to run transaction test outside local tecno_control_retention_test")
        cls.engine = create_engine(TEST_DATABASE_URL)
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_runtime_wrapper_opens_complete_postgres_snapshot(self) -> None:
        suffix = os.urandom(6).hex()
        with Session(self.engine) as db:
            client = Client(name="Wrapper", slug=f"wrapper-{suffix}")
            db.add(client)
            db.flush()
            branch = Branch(client_id=client.id, name="Wrapper", code="B")
            db.add(branch)
            db.flush()
            channel = Channel(client_id=client.id, branch_id=branch.id, name="Wrapper")
            db.add(channel)
            db.commit()
            runtime = build_player_runtime_from_snapshot(channel, db, BASE)
            self.assertEqual(runtime["runtime_version"], 3)
            self.assertTrue(runtime["media_retention"]["complete"])
            self.assertEqual(runtime["media_retention"]["assets"], [])

    def test_publication_after_snapshot_start_is_not_mixed(self) -> None:
        suffix = os.urandom(6).hex()
        with Session(self.engine) as writer:
            client = Client(name="Snapshot", slug=f"snapshot-{suffix}")
            writer.add(client)
            writer.flush()
            branch = Branch(client_id=client.id, name="Snapshot", code="B")
            writer.add(branch)
            writer.flush()
            channel = Channel(client_id=client.id, branch_id=branch.id, name="Snapshot")
            campaign_a = Campaign(client_id=client.id, name="A")
            campaign_b = Campaign(client_id=client.id, name="B")
            writer.add_all([channel, campaign_a, campaign_b])
            writer.flush()
            writer.add(CampaignChannelAssignment(channel_id=channel.id, campaign_id=campaign_a.id))
            writer.commit()
            channel_id, a_id, b_id = channel.id, campaign_a.id, campaign_b.id

        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with Session(bind=connection, autoflush=False) as reader:
                channel = reader.get(Channel, channel_id)
                self.assertIsNotNone(channel)
                # The first query establishes the MVCC snapshot before publication B commits.
                self.assertEqual(len(list(reader.scalars(select(CampaignChannelAssignment).where(CampaignChannelAssignment.channel_id == channel_id)))), 1)
                with Session(self.engine) as writer:
                    writer.add(CampaignChannelAssignment(channel_id=channel_id, campaign_id=b_id))
                    writer.commit()
                runtime = build_player_runtime_config(channel, reader, BASE, snapshot_coherent=True)
                self.assertTrue(runtime["media_retention"]["complete"])
                self.assertEqual(runtime["media_retention"]["protected_campaign_ids"], [a_id])
                self.assertEqual(runtime["campaigns"][0]["campaign_id"], a_id)
