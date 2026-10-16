from __future__ import annotations

from pathlib import Path
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.media_storage import MediaPathError, resolve_stored_media_path, validate_media_file
from app.services.media_references import lock_media_paths, media_reference_operation, require_media_commit
from app.models.entities import (
    AudioPlaylistItem,
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaylistItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    ContentFolder,
    ContentItem,
    ContentType,
    Schedule,
)


settings = get_settings()
STREAM_TOKENS = ("m3u8", "rtmp", "youtube.com/live", "youtu.be/live", "twitch.tv", ".mpd", "rtsp://", "udp://")
logger = logging.getLogger(__name__)


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric_value = int(round(float(value)))
        return numeric_value if numeric_value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            numeric_value = int(round(float(stripped)))
        except ValueError:
            return None
        return numeric_value if numeric_value > 0 else None
    return None


def resolve_content_duration_seconds(
    content_type: ContentType,
    requested_duration_seconds: int | None,
    metadata_json: dict[str, Any] | None = None,
) -> int:
    metadata = metadata_json or {}
    metadata_seconds = (
        _coerce_positive_int(metadata.get("duration_seconds"))
        or _coerce_positive_int(metadata.get("video_duration_seconds"))
        or _coerce_positive_int(metadata.get("duration"))
    )
    if metadata_seconds is None:
        duration_ms = _coerce_positive_int(metadata.get("duration_ms"))
        metadata_seconds = int(round(duration_ms / 1000)) if duration_ms else None

    requested_seconds = _coerce_positive_int(requested_duration_seconds)
    if content_type == ContentType.VIDEO:
        return metadata_seconds or requested_seconds or 15
    return requested_seconds or metadata_seconds or 15


def is_stream_content(content: ContentItem | None) -> bool:
    if not content or content.type != ContentType.URL:
        return False
    source = f"{content.source_url or ''} {content.file_path or ''}".lower()
    return any(token in source for token in STREAM_TOKENS)


def content_supports_manual_duration(content: ContentItem | None) -> bool:
    if not content:
        return True
    if content.type == ContentType.VIDEO:
        return False
    if is_stream_content(content):
        return False
    return content.type in {ContentType.IMAGE, ContentType.TEXT, ContentType.HTML, ContentType.URL}


def resolve_sequence_item_duration(
    *,
    item_type: CampaignSequenceItemType,
    content: ContentItem | None,
    campaign_default_duration_seconds: int,
    requested_duration_seconds: int | None,
) -> int:
    requested_seconds = _coerce_positive_int(requested_duration_seconds)
    default_seconds = _coerce_positive_int(campaign_default_duration_seconds) or 15

    if item_type == CampaignSequenceItemType.LAYOUT:
        return requested_seconds or default_seconds

    if content is None:
        return requested_seconds or default_seconds

    if content.type == ContentType.VIDEO:
        return resolve_content_duration_seconds(ContentType.VIDEO, content.duration_seconds, content.metadata_json or {})

    if is_stream_content(content):
        return _coerce_positive_int(content.duration_seconds) or requested_seconds or default_seconds

    return requested_seconds or _coerce_positive_int(content.duration_seconds) or default_seconds


def resolve_media_file_path(content: ContentItem) -> Path | None:
    return resolve_stored_media_path(settings.MEDIA_ROOT, content.file_path)


def preflight_content_files(contents: list[ContentItem]) -> list[Path]:
    # Complete the entire validation before mutating even the first row.
    return list(dict.fromkeys(path for content in contents if (path := resolve_media_file_path(content)) is not None))


def _referenced_media_paths(db: Session) -> set[Path]:
    referenced: set[Path] = set()
    for stored_path in db.scalars(select(ContentItem.file_path).where(ContentItem.file_path.is_not(None))):
        try:
            path = resolve_stored_media_path(settings.MEDIA_ROOT, stored_path, must_exist=False)
        except MediaPathError as exc:
            # An incomplete global scan cannot authorize ANY unlink in this batch.
            # Log only controlled codes, never stored paths, tenants or OS errors.
            logger.warning("UNRESOLVED_SURVIVING_REFERENCE cause=%s", exc.code)
            raise MediaPathError("UNRESOLVED_SURVIVING_REFERENCE") from exc
        if path is not None:
            referenced.add(path)
    return referenced


def cleanup_files(file_paths: list[Path], db: Session) -> int:
    # Reference mutations use an outer operation so the same locks remain held
    # across a confirmed DB commit. Reject accidental calls before that commit.
    with media_reference_operation(db):
        return _cleanup_files_locked(file_paths, db)


def _cleanup_files_locked(file_paths: list[Path], db: Session) -> int:
    """Only exact, preflighted files; keep every file still referenced in the DB.

    Called after a successful commit so DB failures cannot remove live media.
    Revalidate the full batch before the first unlink, then recheck each target.
    No name/stem heuristics, recursive deletion or removal of parent directories.
    """
    if not file_paths:
        return 0
    targets = list(dict.fromkeys(validate_media_file(settings.MEDIA_ROOT, path) for path in file_paths))
    lock_media_paths(db, targets)
    require_media_commit(db)
    referenced = _referenced_media_paths(db)
    deleted_files = 0
    for target in targets:
        if target in referenced:
            logger.info("FILE_STILL_REFERENCED")
            continue
        if validate_media_file(settings.MEDIA_ROOT, target) != target:
            raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT")
        lock_media_paths(db, [target])
        try:
            target.unlink()
        except OSError as exc:
            raise MediaPathError("MEDIA_DELETE_FAILED") from exc
        deleted_files += 1
    return deleted_files


def collect_folder_descendants(folder: ContentFolder, db: Session) -> list[ContentFolder]:
    ordered_folders = list(
        db.scalars(
            select(ContentFolder)
            .where(ContentFolder.client_id == folder.client_id)
            .order_by(ContentFolder.created_at.asc())
        )
    )
    children_by_parent: dict[str | None, list[ContentFolder]] = {}
    for current_folder in ordered_folders:
        children_by_parent.setdefault(current_folder.parent_id, []).append(current_folder)

    descendants: list[ContentFolder] = []

    def visit(folder_id: str) -> None:
        current_folder = next((item for item in ordered_folders if item.id == folder_id), None)
        if not current_folder:
            return
        descendants.append(current_folder)
        for child in children_by_parent.get(folder_id, []):
            visit(child.id)

    visit(folder.id)
    return descendants


def collect_folder_contents(folder_ids: list[str], db: Session) -> list[ContentItem]:
    if not folder_ids:
        return []
    return list(
        db.scalars(
            select(ContentItem)
            .where(ContentItem.folder_id.in_(folder_ids))
            .order_by(ContentItem.created_at.asc())
        )
    )


def build_folder_delete_impact(folder: ContentFolder, db: Session) -> dict[str, Any]:
    descendants = collect_folder_descendants(folder, db)
    folder_ids = [item.id for item in descendants]
    contents = collect_folder_contents(folder_ids, db)
    content_ids = [item.id for item in contents]

    referenced_campaign_ids: set[str] = set()
    published_campaign_ids: set[str] = set()

    if content_ids:
        referenced_campaign_ids.update(
            db.scalars(select(CampaignSequenceItem.campaign_id).where(CampaignSequenceItem.content_id.in_(content_ids)))
        )
        referenced_campaign_ids.update(
            db.scalars(select(CampaignPlaylistItem.campaign_id).where(CampaignPlaylistItem.content_id.in_(content_ids)))
        )

    if referenced_campaign_ids:
        published_campaign_ids.update(
            db.scalars(
                select(Campaign.id)
                .join(CampaignChannelAssignment, CampaignChannelAssignment.campaign_id == Campaign.id)
                .where(Campaign.id.in_(referenced_campaign_ids))
            )
        )
        published_campaign_ids.update(
            db.scalars(
                select(Campaign.id)
                .join(Schedule, Schedule.campaign_id == Campaign.id)
                .where(Campaign.id.in_(referenced_campaign_ids))
            )
        )

    published_campaign_names = list(
        db.scalars(select(Campaign.name).where(Campaign.id.in_(published_campaign_ids)).order_by(Campaign.name.asc()))
    )

    return {
        "folder_id": folder.id,
        "folder_name": folder.name,
        "folder_count": len(descendants),
        "subfolder_count": max(0, len(descendants) - 1),
        "content_count": len(contents),
        "referenced_campaign_count": len(referenced_campaign_ids),
        "published_campaign_count": len(published_campaign_ids),
        "published_campaign_names": published_campaign_names[:10],
        "has_published_content": bool(published_campaign_ids),
    }


def delete_content_items(contents: list[ContentItem], db: Session) -> list[Path]:
    file_paths = preflight_content_files(contents)
    lock_media_paths(db, file_paths)
    # Revalidate after waiting; a concurrent deleter might have removed a file.
    # Also reject stale file_path updates rather than deleting a reference to
    # a different, unlocked target loaded before this operation acquired locks.
    for content in contents:
        db.refresh(content, attribute_names=["file_path"])
    lock_media_paths(db, preflight_content_files(contents))
    for content in contents:
        db.execute(delete(CampaignSequenceItem).where(CampaignSequenceItem.content_id == content.id))
        db.execute(delete(CampaignPlaylistItem).where(CampaignPlaylistItem.content_id == content.id))
        db.execute(delete(AudioPlaylistItem).where(AudioPlaylistItem.content_id == content.id))
        db.delete(content)
    db.flush()
    return file_paths


def delete_folder_tree(folder: ContentFolder, db: Session) -> dict[str, Any]:
    impact = build_folder_delete_impact(folder, db)
    descendants = collect_folder_descendants(folder, db)
    folder_ids = [item.id for item in descendants]
    contents = collect_folder_contents(folder_ids, db)
    file_paths = delete_content_items(contents, db)

    depth_by_id = {item.id: 0 for item in descendants}
    folders_by_id = {item.id: item for item in descendants}
    for current_folder in descendants:
        depth = 0
        parent_id = current_folder.parent_id
        while parent_id and parent_id in folders_by_id:
            depth += 1
            parent_id = folders_by_id[parent_id].parent_id
        depth_by_id[current_folder.id] = depth

    ordered_folders = sorted(descendants, key=lambda item: depth_by_id[item.id], reverse=True)
    for current_folder in ordered_folders:
        db.delete(current_folder)
    db.flush()

    return {
        **impact,
        "deleted_content_ids": [content.id for content in contents],
        "deleted_folder_ids": [current_folder.id for current_folder in ordered_folders],
        "file_paths": file_paths,
    }
