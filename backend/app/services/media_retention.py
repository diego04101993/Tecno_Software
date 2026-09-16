from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.public_urls import build_absolute_public_url
from app.models.entities import (
    Branch,
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaylistItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    Channel,
    ChannelMode,
    ContentItem,
    Schedule,
)


SCHEMA_VERSION = 1


class RetentionIncomplete(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stamp(value: datetime | None) -> str | None:
    return _utc(value).isoformat().replace("+00:00", "Z") if value else None


def incomplete_retention(channel_id: str, generated_at: datetime, reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": False,
        "channel_id": channel_id,
        "revision": None,
        "generated_at": _stamp(generated_at),
        "protected_campaign_ids": [],
        "assets": [],
        "reason": reason,
    }


def _has_hidden_media(value: object) -> bool:
    if isinstance(value, str):
        return "media/" in value
    if isinstance(value, dict):
        return any(_has_hidden_media(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_hidden_media(item) for item in value)
    return False


def _media_path(path: str, client_id: str) -> bool:
    prefix = f"/media/{client_id}/"
    if not path.startswith(prefix) or "?" in path or "#" in path or "\\" in path or "%" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path[len(prefix):].split("/"))


def _schedule_rule(schedule: Schedule) -> dict[str, object]:
    return {
        "id": schedule.id,
        "campaign_id": schedule.campaign_id,
        "channel_id": schedule.channel_id,
        "branch_id": schedule.branch_id,
        "layout_id": schedule.layout_id,
        "recurrence": schedule.recurrence.value if hasattr(schedule.recurrence, "value") else schedule.recurrence,
        "days_of_week": sorted(schedule.days_of_week or []),
        "starts_on": schedule.starts_on.isoformat() if schedule.starts_on else None,
        "ends_on": schedule.ends_on.isoformat() if schedule.ends_on else None,
        "start_time": schedule.start_time.isoformat() if schedule.start_time else None,
        "end_time": schedule.end_time.isoformat() if schedule.end_time else None,
        "timezone": schedule.timezone,
        "priority": schedule.priority,
        "is_active": schedule.is_active,
        "is_looping": schedule.is_looping,
    }


def _assignment_rule(assignment: CampaignChannelAssignment) -> dict[str, object]:
    return {
        "id": assignment.id,
        "campaign_id": assignment.campaign_id,
        "channel_id": assignment.channel_id,
        "priority": assignment.priority,
        "active_from": _stamp(assignment.active_from),
        "active_until": _stamp(assignment.active_until),
    }


def build_media_retention(
    channel: Channel,
    db: Session,
    base_url: str,
    selected_campaign: Campaign | None,
    generated_at: datetime,
    *,
    unknown_runtime_media: bool = False,
) -> dict[str, object]:
    """Enumerate retained local media from the same database snapshot as the runtime."""
    if unknown_runtime_media or channel.mode in {ChannelMode.AUDIO, ChannelMode.TOUCH}:
        raise RetentionIncomplete("unknown_media_reference")

    branch = db.get(Branch, channel.branch_id)
    if not branch or branch.client_id != channel.client_id:
        raise RetentionIncomplete("cross_tenant_reference")

    schedules = list(db.scalars(select(Schedule).where(
        or_(
            Schedule.channel_id == channel.id,
            and_(Schedule.branch_id == channel.branch_id, Schedule.channel_id.is_(None)),
        ),
    )))
    assignments = list(db.scalars(select(CampaignChannelAssignment).where(
        CampaignChannelAssignment.channel_id == channel.id,
    )))

    protected_ids: set[str] = set()
    schedule_rules: list[dict[str, object]] = []
    assignment_rules: list[dict[str, object]] = []
    for schedule in schedules:
        if schedule.client_id != channel.client_id:
            raise RetentionIncomplete("cross_tenant_reference")
        if not schedule.is_active:
            continue
        if schedule.ends_on:
            try:
                local_date = _utc(generated_at).astimezone(ZoneInfo(schedule.timezone or "UTC")).date()
            except (ZoneInfoNotFoundError, ValueError):
                raise RetentionIncomplete("invalid_timezone") from None
            if schedule.ends_on < local_date:
                continue
        if schedule.layout_id:
            raise RetentionIncomplete("unknown_media_reference")
        protected_ids.add(schedule.campaign_id)
        schedule_rules.append(_schedule_rule(schedule))

    for assignment in assignments:
        if assignment.active_until and _utc(assignment.active_until) < _utc(generated_at):
            continue
        protected_ids.add(assignment.campaign_id)
        assignment_rules.append(_assignment_rule(assignment))

    if selected_campaign:
        protected_ids.add(selected_campaign.id)
    campaign_ids = sorted(protected_ids)
    campaigns = {item.id: item for item in db.scalars(select(Campaign).where(Campaign.id.in_(campaign_ids)))} if campaign_ids else {}
    if len(campaigns) != len(campaign_ids) or any(item.client_id != channel.client_id for item in campaigns.values()):
        raise RetentionIncomplete("cross_tenant_reference")
    if any(item.layout_id for item in campaigns.values()):
        raise RetentionIncomplete("unknown_media_reference")

    sequence_items = list(db.scalars(select(CampaignSequenceItem).where(
        CampaignSequenceItem.campaign_id.in_(campaign_ids),
    ))) if campaign_ids else []
    sequence_by_campaign: dict[str, list[CampaignSequenceItem]] = {}
    for item in sequence_items:
        sequence_by_campaign.setdefault(item.campaign_id, []).append(item)
    legacy_ids = [campaign_id for campaign_id in campaign_ids if campaign_id not in sequence_by_campaign]
    legacy_items = list(db.scalars(select(CampaignPlaylistItem).where(
        CampaignPlaylistItem.campaign_id.in_(legacy_ids),
    ))) if legacy_ids else []

    content_ids: set[str] = set()
    effective_references: list[dict[str, object]] = []
    for item in sequence_items:
        if not item.is_enabled:
            continue
        if item.item_type != CampaignSequenceItemType.CONTENT or item.layout_id:
            raise RetentionIncomplete("unknown_media_reference")
        if not item.content_id:
            raise RetentionIncomplete("missing_content")
        if _has_hidden_media(item.options_json):
            raise RetentionIncomplete("unknown_media_reference")
        content_ids.add(item.content_id)
        effective_references.append({"campaign_id": item.campaign_id, "content_id": item.content_id, "sort_order": item.sort_order})
    for item in legacy_items:
        content_ids.add(item.content_id)
        effective_references.append({"campaign_id": item.campaign_id, "content_id": item.content_id, "sort_order": item.sort_order})

    contents = {item.id: item for item in db.scalars(select(ContentItem).where(ContentItem.id.in_(sorted(content_ids))))} if content_ids else {}
    if len(contents) != len(content_ids) or any(item.client_id != channel.client_id for item in contents.values()):
        raise RetentionIncomplete("cross_tenant_reference")

    assets: list[dict[str, str]] = []
    content_rules: list[dict[str, object]] = []
    for content_id in sorted(content_ids):
        content = contents[content_id]
        if _has_hidden_media(content.source_url) or _has_hidden_media(content.html_content) or _has_hidden_media(content.text_content) or _has_hidden_media(content.metadata_json):
            raise RetentionIncomplete("unknown_media_reference")
        path = content.file_path
        if path:
            if not _media_path(path, channel.client_id):
                raise RetentionIncomplete("invalid_media_path")
            url = build_absolute_public_url(base_url, path)
            if not url:
                raise RetentionIncomplete("invalid_media_path")
            assets.append({"content_id": content_id, "file_path": path, "download_url": url})
        content_rules.append({
            "content_id": content_id,
            "file_path": path,
            "type": content.type.value if hasattr(content.type, "value") else content.type,
        })

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "channel_id": channel.id,
        "protected_campaign_ids": campaign_ids,
        "schedule_rules": sorted(schedule_rules, key=lambda item: item["id"]),
        "assignment_rules": sorted(assignment_rules, key=lambda item: item["id"]),
        "effective_references": sorted(effective_references, key=lambda item: (item["campaign_id"], item["sort_order"], item["content_id"])),
        "content_rules": content_rules,
    }
    revision = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "channel_id": channel.id,
        "revision": revision,
        "generated_at": _stamp(generated_at),
        "protected_campaign_ids": campaign_ids,
        "assets": assets,
    }
