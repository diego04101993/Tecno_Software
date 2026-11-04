from __future__ import annotations

from app.services.tenant_mutations import delete_guard

from pathlib import Path
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.media_references import media_reference_owners, media_owned_by, media_protected_paths
from app.services.media_storage import MediaPathError, resolve_stored_media_path, validate_media_file
from app.services.media_references import (
    lock_media_paths, media_reference_operation, require_media_commit,
    approve_media_cleanup, approved_media_cleanup_paths,
)
from app.services.tenancy import assert_related_clients
from app.models.entities import (
    AudioPlaylistItem,
    AudioPlaylist,
    AudioPlaybackEvent,
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaylistItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    ContentFolder,
    ContentItem,
    ContentType,
    PlayerHeartbeat,
    PlayerPlaybackEvent,
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
    try:
        return media_protected_paths(db, settings.MEDIA_ROOT)
    except MediaPathError as exc:
        logger.warning("UNRESOLVED_SURVIVING_REFERENCE cause=%s", exc.code)
        raise MediaPathError("UNRESOLVED_SURVIVING_REFERENCE") from exc


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
    approved = approved_media_cleanup_paths(db)
    deleted_files = 0
    for target in targets:
        if target in referenced or target not in approved:
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


def collect_folder_descendants(folder: ContentFolder, db: Session, *, read_only=False) -> list[ContentFolder]:
    from app.services.tenant_queries import readable_query
    query = select(ContentFolder)
    if read_only:
        query = readable_query(query, ContentFolder, db=db)
    ordered_folders = list(
        db.scalars(
            query
            .where(ContentFolder.client_id == folder.client_id)
            .order_by(ContentFolder.created_at.asc())
        )
    )
    children_by_parent: dict[str | None, list[ContentFolder]] = {}
    for current_folder in ordered_folders:
        children_by_parent.setdefault(current_folder.parent_id, []).append(current_folder)

    descendants: list[ContentFolder] = []

    def visit(folder_id: str) -> None:
        if any(item.id == folder_id for item in descendants):
            return
        current_folder = next((item for item in ordered_folders if item.id == folder_id), None)
        if not current_folder:
            return
        descendants.append(current_folder)
        for child in children_by_parent.get(folder_id, []):
            visit(child.id)

    visit(folder.id)
    return descendants


def collect_folder_contents(folder_ids: list[str], db: Session, *, client_id: str, read_only=False) -> list[ContentItem]:
    if not folder_ids:
        return []
    from app.services.tenant_queries import readable_query
    query = select(ContentItem)
    if read_only:
        query = readable_query(query, ContentItem, db=db)
    return list(
        db.scalars(
            query
            .where(ContentItem.folder_id.in_(folder_ids), ContentItem.client_id == client_id)
            .order_by(ContentItem.created_at.asc())
        )
    )


def build_folder_delete_impact(folder: ContentFolder, db: Session) -> dict[str, Any]:
    from app.services.tenant_queries import readable_query
    descendants = collect_folder_descendants(folder, db, read_only=True)
    folder_ids = [item.id for item in descendants]
    contents = collect_folder_contents(folder_ids, db, client_id=folder.client_id, read_only=True)
    content_ids = [item.id for item in contents]

    referenced_campaign_ids: set[str] = set()
    published_campaign_ids: set[str] = set()

    if content_ids:
        referenced_campaign_ids.update(
            db.scalars(readable_query(select(CampaignSequenceItem.campaign_id), CampaignSequenceItem, db=db,
                                      client_id=folder.client_id).where(CampaignSequenceItem.content_id.in_(content_ids)))
        )
        referenced_campaign_ids.update(
            db.scalars(readable_query(select(CampaignPlaylistItem.campaign_id), CampaignPlaylistItem, db=db,
                                      client_id=folder.client_id).where(CampaignPlaylistItem.content_id.in_(content_ids)))
        )

    if referenced_campaign_ids:
        published_campaign_ids.update(
            db.scalars(
                readable_query(select(Campaign.id), Campaign, db=db, client_id=folder.client_id)
                .join(CampaignChannelAssignment, CampaignChannelAssignment.campaign_id == Campaign.id)
                .where(Campaign.id.in_(referenced_campaign_ids), CampaignChannelAssignment.id.in_(
                    readable_query(select(CampaignChannelAssignment.id), CampaignChannelAssignment, db=db)))
            )
        )
        published_campaign_ids.update(
            db.scalars(
                readable_query(select(Campaign.id), Campaign, db=db, client_id=folder.client_id)
                .join(Schedule, Schedule.campaign_id == Campaign.id)
                .where(Campaign.id.in_(referenced_campaign_ids), Schedule.id.in_(
                    readable_query(select(Schedule.id), Schedule, db=db)))
            )
        )

    published_campaign_names = list(
        db.scalars(select(Campaign.name).where(Campaign.id.in_(published_campaign_ids), Campaign.client_id == folder.client_id).order_by(Campaign.name.asc()))
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


def preflight_content_relationships(contents: list[ContentItem], db: Session) -> None:
    # Validate the entire batch before locks, ORM changes or physical cleanup.
    # OUTER JOIN keeps an orphan parent visible as an unresolved owner (None).
    with db.no_autoflush:
        for content in contents:
            for model, parent, parent_key in (
                (CampaignSequenceItem, Campaign, CampaignSequenceItem.campaign_id),
                (CampaignPlaylistItem, Campaign, CampaignPlaylistItem.campaign_id),
                (AudioPlaylistItem, AudioPlaylist, AudioPlaylistItem.playlist_id),
            ):
                assert_related_clients(content.client_id, db.scalars(
                    select(parent.client_id).select_from(model).outerjoin(parent, parent.id == parent_key)
                    .where(model.content_id == content.id)))
            # These references survive deletion with a NULL FK, so also require
            # ownership before allowing that indirect modification.
            for model, content_key in (
                (AudioPlaybackEvent, AudioPlaybackEvent.content_id),
                (PlayerHeartbeat, PlayerHeartbeat.current_content_id),
                (PlayerPlaybackEvent, PlayerPlaybackEvent.content_id),
            ):
                assert_related_clients(content.client_id, db.scalars(
                    select(model.client_id).where(content_key == content.id)))


def delete_content_items(contents: list[ContentItem], db: Session) -> list[Path]:
    with delete_guard(db, contents, operation="delete_content"):
        preflight_content_relationships(contents, db)
        file_paths = preflight_content_files(contents)
        lock_media_paths(db, file_paths)
        # Revalidate after waiting; a concurrent deleter might have removed a file.
        # Also reject stale file_path updates rather than deleting a reference to
        # a different, unlocked target loaded before this operation acquired locks.
        for content in contents:
            db.refresh(content, attribute_names=["file_path"])
        lock_media_paths(db, preflight_content_files(contents))
        try:
            references = media_reference_owners(db, settings.MEDIA_ROOT)
        except MediaPathError:
            # Preserve the existing post-commit fail-closed cleanup response.
            # Its complete scan will reject this batch before any unlink.
            references = None
        if references is not None:
            eligible = {path for content in contents
                        if (path := resolve_media_file_path(content)) is not None
                        and media_owned_by(db, settings.MEDIA_ROOT, path, content.client_id, references)}
            file_paths = [path for path in file_paths if path in eligible]
        approve_media_cleanup(db, contents)
        for content in contents:
            db.execute(delete(CampaignSequenceItem).where(CampaignSequenceItem.content_id == content.id))
            db.execute(delete(CampaignPlaylistItem).where(CampaignPlaylistItem.content_id == content.id))
            db.execute(delete(AudioPlaylistItem).where(AudioPlaylistItem.content_id == content.id))
            db.delete(content)
        db.flush()
        return file_paths


def delete_folder_tree(folder: ContentFolder, db: Session) -> dict[str, Any]:
    with delete_guard(db, (folder,), operation="delete_content_folder"):
        impact = build_folder_delete_impact(folder, db)
        descendants = collect_folder_descendants(folder, db)
        folder_ids = [item.id for item in descendants]
        # Deleting the parent would SET NULL foreign content or alter foreign children.
        # Abort before any write; do not repair historical associations.
        if (db.scalar(select(ContentItem.id).where(ContentItem.folder_id.in_(folder_ids),
                                                  ContentItem.client_id != folder.client_id).limit(1))
                or db.scalar(select(ContentFolder.id).where(ContentFolder.parent_id.in_(folder_ids),
                                                           ContentFolder.client_id != folder.client_id).limit(1))):
            logger.warning("TENANT_RELATION_CONFLICT")
            raise MediaPathError("TENANT_RELATION_CONFLICT")
        contents = collect_folder_contents(folder_ids, db, client_id=folder.client_id)
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
