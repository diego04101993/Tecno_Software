from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
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
MEDIA_ARTIFACT_DIR_NAMES = ("cache", ".cache", "thumb", "thumbs", "thumbnail", "thumbnails", "preview", "previews")


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
    if not content.file_path or not content.file_path.startswith("/media/"):
        return None
    relative_path = content.file_path.removeprefix("/media/")
    return settings.MEDIA_ROOT / relative_path


def _resolve_related_media_paths(file_path: Path) -> list[Path]:
    resolved: list[Path] = []
    seen_paths: set[Path] = set()

    def remember(path: Path) -> None:
        if path in seen_paths:
            return
        seen_paths.add(path)
        resolved.append(path)

    remember(file_path)

    if not file_path.exists():
        return resolved

    parent = file_path.parent
    stem = file_path.stem
    suffix = file_path.suffix

    for child in parent.iterdir():
        if not child.is_file() or child == file_path:
            continue
        if child.stem == stem and child.suffix != suffix:
            remember(child)

    media_root = settings.MEDIA_ROOT.resolve()
    for artifact_dir_name in MEDIA_ARTIFACT_DIR_NAMES:
        artifact_dir = parent / artifact_dir_name
        if artifact_dir.exists() and artifact_dir.is_dir():
            for child in artifact_dir.iterdir():
                if child.is_file() and (child.stem == stem or child.name.startswith(f"{stem}.")):
                    remember(child)

        client_artifact_dir = media_root / artifact_dir_name
        if client_artifact_dir.exists() and client_artifact_dir.is_dir():
            for child in client_artifact_dir.rglob("*"):
                if child.is_file() and (child.stem == stem or child.name.startswith(f"{stem}.")):
                    remember(child)

    return resolved


def _cleanup_empty_parent_directories(path: Path) -> None:
    media_root = settings.MEDIA_ROOT.resolve()
    current_dir = path.parent.resolve()
    while current_dir != media_root and media_root in current_dir.parents:
        try:
            next(current_dir.iterdir())
            break
        except StopIteration:
            try:
                current_dir.rmdir()
            except OSError:
                break
            current_dir = current_dir.parent.resolve()
        except OSError:
            break


def cleanup_files(file_paths: list[Path]) -> int:
    deleted_files = 0
    cleaned_targets: set[Path] = set()

    for file_path in file_paths:
        for related_path in _resolve_related_media_paths(file_path):
            if related_path in cleaned_targets:
                continue
            cleaned_targets.add(related_path)
            try:
                if related_path.exists() and related_path.is_file():
                    related_path.unlink()
                    deleted_files += 1
                _cleanup_empty_parent_directories(related_path)
            except OSError:
                continue

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
    file_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for content in contents:
        file_path = resolve_media_file_path(content)
        if file_path and file_path not in seen_paths:
            file_paths.append(file_path)
            seen_paths.add(file_path)
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
