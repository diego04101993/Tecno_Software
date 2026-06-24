from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    AudioPlaylistItem,
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaylistItem,
    CampaignSequenceItem,
    ContentFolder,
    ContentItem,
    KioskScreen,
    Layout,
    LayoutRevision,
    PlayerHeartbeat,
    Schedule,
    TouchLocation,
    TouchMap,
)
from app.services.content_cleanup import cleanup_files, delete_content_items


logger = logging.getLogger(__name__)

DEFAULT_CONTENT_RETENTION_DAYS = 5
DEFAULT_CAMPAIGN_RETENTION_DAYS = 15
DEFAULT_FOLDER_RETENTION_DAYS = 15


@dataclass
class CleanupRetentionSummary:
    contents_deleted: int = 0
    campaigns_deleted: int = 0
    folders_deleted: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    pending_file_paths: list[Path] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "contents_deleted": self.contents_deleted,
            "campaigns_deleted": self.campaigns_deleted,
            "folders_deleted": self.folders_deleted,
            "files_deleted": self.files_deleted,
            "errors": list(self.errors),
        }


def now_utc() -> datetime:
    return datetime.now(UTC)


def _build_cutoff(days: int) -> datetime:
    return now_utc() - timedelta(days=max(1, int(days)))


def _record_error(summary: CleanupRetentionSummary, message: str, exc: Exception | None = None) -> None:
    detail = f"{message}: {exc}" if exc else message
    logger.exception(detail) if exc else logger.error(detail)
    summary.errors.append(detail)


def _matches_reference_value(
    value: object,
    *,
    exact_tokens: set[str],
    contains_tokens: set[str],
) -> bool:
    if isinstance(value, dict):
        return any(
            _matches_reference_value(child, exact_tokens=exact_tokens, contains_tokens=contains_tokens)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _matches_reference_value(child, exact_tokens=exact_tokens, contains_tokens=contains_tokens)
            for child in value
        )
    if not isinstance(value, str):
        return False

    normalized = value.strip()
    if not normalized:
        return False
    if normalized in exact_tokens:
        return True
    return any(token and token in normalized for token in contains_tokens)


def _content_reference_tokens(content: ContentItem) -> tuple[set[str], set[str]]:
    metadata = content.metadata_json or {}
    exact_tokens = {
        token.strip()
        for token in [
            content.id,
            content.file_path,
            content.source_url,
            metadata.get("filename"),
        ]
        if isinstance(token, str) and token.strip()
    }
    contains_tokens = {
        token.strip()
        for token in [
            content.id,
            content.file_path,
            content.source_url,
        ]
        if isinstance(token, str) and token and len(token.strip()) >= 8
    }

    if isinstance(content.html_content, str) and content.html_content.strip():
        exact_tokens.add(content.html_content.strip())
    if isinstance(content.text_content, str) and content.text_content.strip():
        exact_tokens.add(content.text_content.strip())

    return exact_tokens, contains_tokens


def _content_is_referenced_in_layouts(content: ContentItem, db: Session) -> bool:
    exact_tokens, contains_tokens = _content_reference_tokens(content)
    if not exact_tokens and not contains_tokens:
        return False

    revisions = list(
        db.scalars(
            select(LayoutRevision)
            .join(Layout, LayoutRevision.layout_id == Layout.id)
            .where(Layout.client_id == content.client_id)
        )
    )
    for revision in revisions:
        if _matches_reference_value(revision.editor_state_json, exact_tokens=exact_tokens, contains_tokens=contains_tokens):
            logger.info("Cleanup skip content %s because it is referenced in layout revision %s", content.id, revision.id)
            return True
    return False


def _content_is_referenced_in_kiosk(content: ContentItem, db: Session) -> bool:
    exact_tokens, contains_tokens = _content_reference_tokens(content)
    if not exact_tokens and not contains_tokens:
        return False

    kiosk_screens = list(
        db.scalars(
            select(KioskScreen).where(KioskScreen.client_id == content.client_id)
        )
    )
    for screen in kiosk_screens:
        if _matches_reference_value(
            {
                "background_url": screen.background_url,
                "attract_media_url": screen.attract_media_url,
                "metadata_json": screen.metadata_json,
            },
            exact_tokens=exact_tokens,
            contains_tokens=contains_tokens,
        ):
            logger.info("Cleanup skip content %s because it is referenced in kiosk screen %s", content.id, screen.id)
            return True

    touch_locations = list(
        db.scalars(
            select(TouchLocation).where(TouchLocation.experience.has(client_id=content.client_id))
        )
    )
    for location in touch_locations:
        if _matches_reference_value(
            {"image_url": location.image_url, "metadata_json": location.metadata_json},
            exact_tokens=exact_tokens,
            contains_tokens=contains_tokens,
        ):
            logger.info("Cleanup skip content %s because it is referenced in touch location %s", content.id, location.id)
            return True

    touch_maps = list(
        db.scalars(
            select(TouchMap).where(TouchMap.experience.has(client_id=content.client_id))
        )
    )
    for touch_map in touch_maps:
        if _matches_reference_value(
            {
                "background_url": touch_map.background_url,
                "overlay_url": touch_map.overlay_url,
                "metadata_json": touch_map.metadata_json,
            },
            exact_tokens=exact_tokens,
            contains_tokens=contains_tokens,
        ):
            logger.info("Cleanup skip content %s because it is referenced in touch map %s", content.id, touch_map.id)
            return True

    return False


def _content_has_live_references(content: ContentItem, db: Session) -> bool:
    if db.scalar(select(CampaignSequenceItem.id).where(CampaignSequenceItem.content_id == content.id).limit(1)):
        return True
    if db.scalar(select(CampaignPlaylistItem.id).where(CampaignPlaylistItem.content_id == content.id).limit(1)):
        return True
    if db.scalar(select(AudioPlaylistItem.id).where(AudioPlaylistItem.content_id == content.id).limit(1)):
        return True
    if db.scalar(select(PlayerHeartbeat.id).where(PlayerHeartbeat.current_content_id == content.id).limit(1)):
        return True
    if _content_is_referenced_in_layouts(content, db):
        return True
    if _content_is_referenced_in_kiosk(content, db):
        return True
    return False


def _campaign_has_live_references(campaign: Campaign, db: Session) -> bool:
    if db.scalar(select(CampaignChannelAssignment.id).where(CampaignChannelAssignment.campaign_id == campaign.id).limit(1)):
        return True
    if db.scalar(select(Schedule.id).where(Schedule.campaign_id == campaign.id).limit(1)):
        return True
    if db.scalar(select(PlayerHeartbeat.id).where(PlayerHeartbeat.current_campaign_id == campaign.id).limit(1)):
        return True
    return False


def cleanup_unused_campaigns(
    db: Session,
    *,
    days: int = DEFAULT_CAMPAIGN_RETENTION_DAYS,
    summary: CleanupRetentionSummary | None = None,
) -> CleanupRetentionSummary:
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    logger.info("Starting cleanup_unused_campaigns cutoff=%s", cutoff.isoformat())

    candidates = list(
        db.scalars(
            select(Campaign)
            .where(Campaign.created_at <= cutoff)
            .order_by(Campaign.created_at.asc())
        )
    )

    for campaign in candidates:
        try:
            if _campaign_has_live_references(campaign, db):
                continue
            with db.begin_nested():
                logger.info("Deleting unused campaign id=%s name=%s", campaign.id, campaign.name)
                db.delete(campaign)
                db.flush()
            working_summary.campaigns_deleted += 1
        except Exception as exc:
            _record_error(working_summary, f"Error deleting campaign {campaign.id}", exc)

    return working_summary


def cleanup_unused_contents(
    db: Session,
    *,
    days: int = DEFAULT_CONTENT_RETENTION_DAYS,
    summary: CleanupRetentionSummary | None = None,
) -> CleanupRetentionSummary:
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    logger.info("Starting cleanup_unused_contents cutoff=%s", cutoff.isoformat())

    candidates = list(
        db.scalars(
            select(ContentItem)
            .where(ContentItem.created_at <= cutoff)
            .order_by(ContentItem.created_at.asc())
        )
    )

    for content in candidates:
        try:
            if _content_has_live_references(content, db):
                continue
            with db.begin_nested():
                logger.info("Deleting unused content id=%s name=%s", content.id, content.name)
                working_summary.pending_file_paths.extend(delete_content_items([content], db))
            working_summary.contents_deleted += 1
        except Exception as exc:
            _record_error(working_summary, f"Error deleting content {content.id}", exc)

    return working_summary


def cleanup_unused_folders(
    db: Session,
    *,
    days: int = DEFAULT_FOLDER_RETENTION_DAYS,
    summary: CleanupRetentionSummary | None = None,
) -> CleanupRetentionSummary:
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    logger.info("Starting cleanup_unused_folders cutoff=%s", cutoff.isoformat())

    while True:
        deleted_this_pass = 0
        candidates = list(
            db.scalars(
                select(ContentFolder)
                .where(ContentFolder.created_at <= cutoff)
                .order_by(ContentFolder.created_at.asc())
            )
        )
        for folder in candidates:
            try:
                has_contents = db.scalar(select(ContentItem.id).where(ContentItem.folder_id == folder.id).limit(1))
                has_children = db.scalar(select(ContentFolder.id).where(ContentFolder.parent_id == folder.id).limit(1))
                if has_contents or has_children:
                    continue
                with db.begin_nested():
                    logger.info("Deleting unused folder id=%s name=%s", folder.id, folder.name)
                    db.delete(folder)
                    db.flush()
                working_summary.folders_deleted += 1
                deleted_this_pass += 1
            except Exception as exc:
                _record_error(working_summary, f"Error deleting folder {folder.id}", exc)
        if deleted_this_pass == 0:
            break

    return working_summary


def run_retention_cleanup(
    db: Session,
    *,
    content_days: int = DEFAULT_CONTENT_RETENTION_DAYS,
    campaign_days: int = DEFAULT_CAMPAIGN_RETENTION_DAYS,
    folder_days: int = DEFAULT_FOLDER_RETENTION_DAYS,
) -> dict[str, object]:
    summary = CleanupRetentionSummary()
    logger.info(
        "Starting retention cleanup content_days=%s campaign_days=%s folder_days=%s",
        content_days,
        campaign_days,
        folder_days,
    )
    try:
        cleanup_unused_campaigns(db, days=campaign_days, summary=summary)
        db.commit()
    except Exception as exc:
        db.rollback()
        _record_error(summary, "Error committing unused campaign cleanup", exc)

    try:
        cleanup_unused_contents(db, days=content_days, summary=summary)
        db.commit()
        if summary.pending_file_paths:
            deleted_files = cleanup_files(summary.pending_file_paths)
            summary.files_deleted += deleted_files
            logger.info("Deleted %s physical media files for unused contents", deleted_files)
            summary.pending_file_paths.clear()
    except Exception as exc:
        db.rollback()
        summary.pending_file_paths.clear()
        _record_error(summary, "Error committing unused content cleanup", exc)

    try:
        cleanup_unused_folders(db, days=folder_days, summary=summary)
        db.commit()
    except Exception as exc:
        db.rollback()
        _record_error(summary, "Error committing unused folder cleanup", exc)

    logger.info("Retention cleanup finished summary=%s", summary.to_dict())
    return summary.to_dict()
