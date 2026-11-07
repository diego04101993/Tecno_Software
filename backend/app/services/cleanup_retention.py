from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select
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
    VideowallPublication,
    VideowallPublicationAsset,
)
from app.services.content_cleanup import cleanup_files, delete_content_items, preflight_content_files
from app.services.media_references import lock_media_paths, media_reference_operation, approve_media_cleanup
from app.services.media_storage import MediaPathError
from app.services.tenant_mutations import delete_guard
from app.services.tenant_relations import conflict


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


def _content_has_live_references(content: ContentItem, db: Session, removing_campaign_ids=()) -> bool:
    # Publication history is durable authority, including FAILED/old ACTIVE
    # generations. No status/tenant filter may hide a surviving reference.
    if db.scalar(select(VideowallPublicationAsset.id).where(or_(
            VideowallPublicationAsset.master_content_id == content.id,
            VideowallPublicationAsset.derived_content_id == content.id)).limit(1)):
        return True
    if db.scalar(select(CampaignSequenceItem.id).where(CampaignSequenceItem.content_id == content.id, CampaignSequenceItem.campaign_id.not_in(removing_campaign_ids)).limit(1)):
        return True
    if db.scalar(select(CampaignPlaylistItem.id).where(CampaignPlaylistItem.content_id == content.id, CampaignPlaylistItem.campaign_id.not_in(removing_campaign_ids)).limit(1)):
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
    if db.scalar(select(VideowallPublication.id).where(
            VideowallPublication.campaign_id == campaign.id).limit(1)):
        return True
    if db.scalar(select(CampaignChannelAssignment.id).where(CampaignChannelAssignment.campaign_id == campaign.id).limit(1)):
        return True
    if db.scalar(select(Schedule.id).where(Schedule.campaign_id == campaign.id).limit(1)):
        return True
    if db.scalar(select(PlayerHeartbeat.id).where(PlayerHeartbeat.current_campaign_id == campaign.id).limit(1)):
        return True
    return False


def _campaign_candidates(db, cutoff):
    with db.no_autoflush:
        return [row for row in db.scalars(select(Campaign).where(Campaign.created_at <= cutoff)
                                          .order_by(Campaign.created_at, Campaign.id))
                if not _campaign_has_live_references(row, db)]


def _content_candidates(db, cutoff, removing_campaign_ids=()):
    with db.no_autoflush:
        return [row for row in db.scalars(select(ContentItem).where(ContentItem.created_at <= cutoff)
                                          .order_by(ContentItem.created_at, ContentItem.id))
                if not _content_has_live_references(row, db, removing_campaign_ids)]


def _folder_candidates(db, cutoff, removing_content_ids=()):
    # Simulate the existing leaf-first loop without clearing FKs or flushing.
    with db.no_autoflush:
        candidates = list(db.scalars(select(ContentFolder).where(ContentFolder.created_at <= cutoff)
                                     .order_by(ContentFolder.created_at, ContentFolder.id)))
        selected, selected_ids = [], set()
        while True:
            leaves = [row for row in candidates if row.id not in selected_ids
                      and not db.scalar(select(ContentItem.id).where(ContentItem.folder_id == row.id,
                          ContentItem.id.not_in(removing_content_ids)).limit(1))
                      and not db.scalar(select(ContentFolder.id).where(ContentFolder.parent_id == row.id,
                          ContentFolder.id.not_in(selected_ids)).limit(1))]
            if not leaves:
                return selected
            selected.extend(leaves)
            selected_ids.update(row.id for row in leaves)


def _ids(rows):
    return tuple(row.id for row in rows)


def cleanup_unused_campaigns(db: Session, *, days=DEFAULT_CAMPAIGN_RETENTION_DAYS, summary=None):
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    candidates = _campaign_candidates(db, cutoff)
    expected = _ids(candidates)
    with delete_guard(db, candidates):
        if expected != _ids(_campaign_candidates(db, cutoff)):
            raise conflict()
        for campaign in candidates:
            db.delete(campaign)
        db.flush()
        working_summary.campaigns_deleted += len(candidates)
    return working_summary


def cleanup_unused_contents(db: Session, *, days=DEFAULT_CONTENT_RETENTION_DAYS, summary=None):
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    candidates = _content_candidates(db, cutoff)
    expected = _ids(candidates)
    with delete_guard(db, candidates):
        if expected != _ids(_content_candidates(db, cutoff)):
            raise conflict()
        working_summary.pending_file_paths.extend(delete_content_items(candidates, db))
        working_summary.contents_deleted += len(candidates)
    return working_summary


def cleanup_unused_folders(db: Session, *, days=DEFAULT_FOLDER_RETENTION_DAYS, summary=None):
    working_summary = summary or CleanupRetentionSummary()
    cutoff = _build_cutoff(days)
    candidates = _folder_candidates(db, cutoff)
    expected = _ids(candidates)
    with delete_guard(db, candidates):
        if expected != _ids(_folder_candidates(db, cutoff)):
            raise conflict()
        for folder in candidates:
            db.delete(folder)
            db.flush()
        working_summary.folders_deleted += len(candidates)
    return working_summary


def run_retention_cleanup(
    db: Session, *, content_days=DEFAULT_CONTENT_RETENTION_DAYS,
    campaign_days=DEFAULT_CAMPAIGN_RETENTION_DAYS, folder_days=DEFAULT_FOLDER_RETENTION_DAYS,
    actor=None,
) -> dict[str, object]:
    summary = CleanupRetentionSummary()
    committed = False
    campaign_cutoff, content_cutoff, folder_cutoff = (
        _build_cutoff(campaign_days), _build_cutoff(content_days), _build_cutoff(folder_days))
    try:
        campaigns = _campaign_candidates(db, campaign_cutoff)
        contents = _content_candidates(db, content_cutoff, _ids(campaigns))
        folders = _folder_candidates(db, folder_cutoff, _ids(contents))
        expected = (_ids(campaigns), _ids(contents), _ids(folders))
        with delete_guard(db, (*campaigns, *contents, *folders), actor=actor), media_reference_operation(db):
            # Recheck eligibility in the fresh locked snapshot before any DML.
            current_campaigns = _campaign_candidates(db, campaign_cutoff)
            current_contents = _content_candidates(db, content_cutoff, _ids(current_campaigns))
            current_folders = _folder_candidates(db, folder_cutoff, _ids(current_contents))
            if expected != (_ids(current_campaigns), _ids(current_contents), _ids(current_folders)):
                raise conflict()
            # Preserve the existing path preflight, including referenced old files.
            file_paths = preflight_content_files(list(db.scalars(select(ContentItem).where(
                ContentItem.created_at <= content_cutoff))))
            lock_media_paths(db, file_paths)
            approve_media_cleanup(db, contents)
            for campaign in campaigns:
                db.delete(campaign)
            db.flush()
            summary.campaigns_deleted = len(campaigns)
            summary.pending_file_paths.extend(delete_content_items(contents, db))
            summary.contents_deleted = len(contents)
            for folder in folders:
                db.delete(folder)
                db.flush()
            summary.folders_deleted = len(folders)
            db.commit()
            committed = True
            summary.files_deleted = cleanup_files(summary.pending_file_paths, db)
            summary.pending_file_paths.clear()
    except HTTPException:
        db.rollback()
        raise  # Integrity rejection is atomic, uniform and never logged with row details.
    except MediaPathError as exc:
        db.rollback()
        summary.pending_file_paths.clear()
        _record_error(summary, exc.code)
        if not committed:
            summary.contents_deleted = summary.campaigns_deleted = summary.folders_deleted = 0
    except Exception:
        db.rollback()
        summary.pending_file_paths.clear()
        if not committed:
            summary.contents_deleted = summary.campaigns_deleted = summary.folders_deleted = 0
        _record_error(summary, "Error committing cleanup")
    logger.info("Retention cleanup finished summary=%s", summary.to_dict())
    return summary.to_dict()
