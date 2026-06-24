from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import (
    AudioAssignment,
    AudioNormalizationStatus,
    AudioPlaybackEntryKind,
    AudioPlaybackEvent,
    AudioPlaylist,
    AudioPlaylistItem,
    AudioPlaylistKind,
    AudioSpotRotationMode,
    Branch,
    Channel,
    ChannelMode,
    ContentItem,
    ContentType,
    User,
)
from app.services.presence import build_presence_snapshot
from app.schemas.domain import (
    AudioAssignmentCreate,
    AudioAssignmentRead,
    AudioPlaybackEventCreate,
    AudioPlaybackEventRead,
    AudioPlaylistCreate,
    AudioPlaylistItemCreate,
    AudioPlaylistRead,
)
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    branch_channel_ids_query,
    can_write_branch_scope,
    can_write_client_scope,
    get_branch_in_scope,
    get_channel_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


router = APIRouter()


def serialize_audio_content(content: ContentItem) -> dict:
    metadata = content.metadata_json or {}
    return {
        "id": content.id,
        "name": content.name,
        "client_id": content.client_id,
        "file_path": content.file_path,
        "duration_seconds": content.duration_seconds,
        "audio_kind": metadata.get("audio_kind", "music"),
        "normalization_status": metadata.get("normalization_status", "pending"),
        "target_lufs": metadata.get("target_lufs", -14),
        "volume_normalized": bool(metadata.get("volume_normalized", False)),
        "metadata_json": metadata,
    }


def serialize_playlist_item(item: AudioPlaylistItem, content: ContentItem | None) -> dict:
    return {
        "id": item.id,
        "playlist_id": item.playlist_id,
        "content_id": item.content_id,
        "sort_order": item.sort_order,
        "is_enabled": item.is_enabled,
        "content": serialize_audio_content(content) if content else None,
    }


def serialize_assignment(assignment: AudioAssignment) -> dict:
    return AudioAssignmentRead.model_validate(assignment).model_dump()


def get_playlist_in_scope(playlist_id: str, db: Session, current_user: User) -> AudioPlaylist:
    playlist = db.get(AudioPlaylist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio playlist not found")

    require_client_scope(current_user, playlist.client_id)
    return playlist


def get_audio_content_in_scope(content_id: str, db: Session, current_user: User) -> ContentItem:
    content = db.get(ContentItem, content_id)
    if not content or content.type != ContentType.AUDIO:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio content not found")

    require_client_scope(current_user, content.client_id)
    return content


def get_assignment_query_for_user(current_user: User):
    query = apply_client_filter(select(AudioAssignment).order_by(AudioAssignment.created_at.desc()), AudioAssignment, current_user)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        query = query.where(
            or_(
                AudioAssignment.branch_id == branch_id,
                AudioAssignment.channel_id.in_(branch_channel_ids_query(branch_id)),
            )
        )
    return query


def resolve_assignment_playlists(
    assignment: AudioAssignment | None,
    db: Session,
) -> tuple[AudioPlaylist | None, AudioPlaylist | None]:
    if not assignment:
        return None, None

    music_playlist = db.get(AudioPlaylist, assignment.music_playlist_id) if assignment.music_playlist_id else None
    spot_playlist = db.get(AudioPlaylist, assignment.spot_playlist_id) if assignment.spot_playlist_id else None
    return music_playlist, spot_playlist


def build_playlist_payload(playlist: AudioPlaylist | None, db: Session) -> dict | None:
    if not playlist:
        return None

    items = list(
        db.scalars(
            select(AudioPlaylistItem)
            .where(AudioPlaylistItem.playlist_id == playlist.id)
            .order_by(AudioPlaylistItem.sort_order.asc())
        )
    )
    items = [item for item in items if item.is_enabled]
    contents = {item.content_id: db.get(ContentItem, item.content_id) for item in items}

    return {
        **AudioPlaylistRead.model_validate(playlist).model_dump(),
        "items": [serialize_playlist_item(item, contents.get(item.content_id)) for item in items],
    }


def validate_playlist_kind(playlist: AudioPlaylist, expected_kind: AudioPlaylistKind, label: str):
    if playlist.kind != expected_kind:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} playlist must be of kind {expected_kind.value}")


@router.get("/library")
def list_audio_library(
    client_id: str | None = Query(default=None),
    kind: AudioPlaylistKind | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    query = apply_client_filter(select(ContentItem).order_by(ContentItem.created_at.desc()), ContentItem, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(ContentItem.client_id == target_client_id)
    query = query.where(ContentItem.type == ContentType.AUDIO)

    items = list(db.scalars(query))
    if kind:
        items = [item for item in items if (item.metadata_json or {}).get("audio_kind", "music") == kind.value]
    return [serialize_audio_content(item) for item in items]


@router.get("/playlists", response_model=list[AudioPlaylistRead])
def list_audio_playlists(
    client_id: str | None = Query(default=None),
    kind: AudioPlaylistKind | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AudioPlaylist]:
    query = apply_client_filter(select(AudioPlaylist).order_by(AudioPlaylist.created_at.desc()), AudioPlaylist, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(AudioPlaylist.client_id == target_client_id)
    if kind:
        query = query.where(AudioPlaylist.kind == kind)
    return list(db.scalars(query))


@router.post("/playlists", response_model=AudioPlaylistRead, status_code=status.HTTP_201_CREATED)
def create_audio_playlist(
    payload: AudioPlaylistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AudioPlaylist:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create audio playlists")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)

    playlist = AudioPlaylist(
        client_id=client_id,
        name=payload.name,
        kind=payload.kind,
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return playlist


@router.get("/playlists/{playlist_id}/items")
def list_audio_playlist_items(
    playlist_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    playlist = get_playlist_in_scope(playlist_id, db, current_user)
    items = list(
        db.scalars(
            select(AudioPlaylistItem)
            .where(AudioPlaylistItem.playlist_id == playlist.id)
            .order_by(AudioPlaylistItem.sort_order.asc())
        )
    )
    contents = {item.content_id: db.get(ContentItem, item.content_id) for item in items}
    return [serialize_playlist_item(item, contents.get(item.content_id)) for item in items]


@router.post("/playlists/{playlist_id}/items", status_code=status.HTTP_201_CREATED)
def add_audio_playlist_item(
    playlist_id: str,
    payload: AudioPlaylistItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify audio playlists")

    playlist = get_playlist_in_scope(playlist_id, db, current_user)
    content = get_audio_content_in_scope(payload.content_id, db, current_user)
    if content.client_id != playlist.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio content not found for this client")

    audio_kind = (content.metadata_json or {}).get("audio_kind")
    if audio_kind and audio_kind != playlist.kind.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio content kind '{audio_kind}' does not match playlist kind '{playlist.kind.value}'",
        )

    item = AudioPlaylistItem(
        playlist_id=playlist.id,
        content_id=content.id,
        sort_order=payload.sort_order,
        is_enabled=payload.is_enabled,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_playlist_item(item, content)


@router.delete("/playlists/{playlist_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_audio_playlist_item(
    playlist_id: str,
    item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify audio playlists")

    playlist = get_playlist_in_scope(playlist_id, db, current_user)
    item = db.get(AudioPlaylistItem, item_id)
    if not item or item.playlist_id != playlist.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio playlist item not found")

    db.delete(item)
    db.commit()


@router.get("/assignments", response_model=list[AudioAssignmentRead])
def list_audio_assignments(
    client_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    channel_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AudioAssignment]:
    query = get_assignment_query_for_user(current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(AudioAssignment.client_id == target_client_id)
    if branch_id:
        branch = get_branch_in_scope(db, branch_id, current_user)
        query = query.where(AudioAssignment.branch_id == branch.id)
    if channel_id:
        channel = get_channel_in_scope(db, channel_id, current_user)
        query = query.where(AudioAssignment.channel_id == channel.id)
    return list(db.scalars(query))


@router.post("/assignments", response_model=AudioAssignmentRead, status_code=status.HTTP_201_CREATED)
def upsert_audio_assignment(
    payload: AudioAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AudioAssignment:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to manage audio assignments")

    branch: Branch | None = None
    channel: Channel | None = None

    if payload.channel_id:
        channel = get_channel_in_scope(db, payload.channel_id, current_user)
        branch = db.get(Branch, channel.branch_id)
    elif payload.branch_id:
        branch = get_branch_in_scope(db, payload.branch_id, current_user)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_id or channel_id is required")

    resolved_client_id = payload.client_id or (branch.client_id if branch else channel.client_id)
    target_client_id = require_client_scope(current_user, resolved_client_id)
    if branch and branch.client_id != target_client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")
    if channel and channel.client_id != target_client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found for this client")

    music_playlist = db.get(AudioPlaylist, payload.music_playlist_id) if payload.music_playlist_id else None
    spot_playlist = db.get(AudioPlaylist, payload.spot_playlist_id) if payload.spot_playlist_id else None
    if music_playlist:
        require_client_scope(current_user, music_playlist.client_id)
        validate_playlist_kind(music_playlist, AudioPlaylistKind.MUSIC, "Music")
    if spot_playlist:
        require_client_scope(current_user, spot_playlist.client_id)
        validate_playlist_kind(spot_playlist, AudioPlaylistKind.SPOT, "Spot")
    if music_playlist and music_playlist.client_id != target_client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Music playlist not found for this client")
    if spot_playlist and spot_playlist.client_id != target_client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spot playlist not found for this client")

    existing = None
    if channel:
        existing = db.scalar(select(AudioAssignment).where(AudioAssignment.channel_id == channel.id))
    elif branch:
        existing = db.scalar(
            select(AudioAssignment).where(
                AudioAssignment.branch_id == branch.id,
                AudioAssignment.channel_id.is_(None),
            )
        )

    assignment = existing or AudioAssignment(client_id=target_client_id)
    assignment.client_id = target_client_id
    assignment.branch_id = branch.id if branch else None
    assignment.channel_id = channel.id if channel else None
    assignment.music_playlist_id = music_playlist.id if music_playlist else None
    assignment.spot_playlist_id = spot_playlist.id if spot_playlist else None
    assignment.songs_between_spots = max(1, payload.songs_between_spots)
    assignment.spots_per_break = max(1, payload.spots_per_break)
    assignment.spot_rotation_mode = payload.spot_rotation_mode
    assignment.avoid_consecutive_spots = payload.avoid_consecutive_spots
    assignment.volume_normalization_enabled = payload.volume_normalization_enabled
    assignment.volume_normalization_status = payload.volume_normalization_status
    assignment.target_lufs = payload.target_lufs

    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


@router.get("/reports/summary")
def get_audio_report_summary(
    client_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    channel_id: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    query = apply_client_filter(select(AudioPlaybackEvent).order_by(AudioPlaybackEvent.played_at.desc()), AudioPlaybackEvent, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(AudioPlaybackEvent.client_id == target_client_id)
    if branch_id:
        branch = get_branch_in_scope(db, branch_id, current_user)
        query = query.where(AudioPlaybackEvent.branch_id == branch.id)
    if channel_id:
        channel = get_channel_in_scope(db, channel_id, current_user)
        query = query.where(AudioPlaybackEvent.channel_id == channel.id)
    if is_branch_scoped(current_user) and not branch_id and not channel_id:
        scoped_branch_id = require_branch_assignment(current_user)
        query = query.where(AudioPlaybackEvent.branch_id == scoped_branch_id)

    cutoff = datetime.now(UTC) - timedelta(days=days)
    query = query.where(AudioPlaybackEvent.played_at >= cutoff)

    events = list(db.scalars(query))
    content_lookup = {
        content.id: content
        for content in db.scalars(
            select(ContentItem).where(ContentItem.id.in_([event.content_id for event in events if event.content_id]))
        )
    } if events else {}
    playlist_lookup = {
        playlist.id: playlist
        for playlist in db.scalars(
            select(AudioPlaylist).where(AudioPlaylist.id.in_([event.playlist_id for event in events if event.playlist_id]))
        )
    } if events else {}

    grouped: dict[str, dict[str, dict]] = {"music": {}, "spot": {}}
    for event in events:
        bucket = grouped[event.entry_kind.value]
        key = event.content_id or event.id
        if key not in bucket:
            content = content_lookup.get(event.content_id) if event.content_id else None
            bucket[key] = {
                "content_id": event.content_id,
                "content_name": content.name if content else "Desconocido",
                "playlist_id": event.playlist_id,
                "playlist_name": playlist_lookup.get(event.playlist_id).name if event.playlist_id and event.playlist_id in playlist_lookup else None,
                "play_count": 0,
                "last_played_at": None,
            }

        bucket[key]["play_count"] += 1
        if bucket[key]["last_played_at"] is None or event.played_at > bucket[key]["last_played_at"]:
            bucket[key]["last_played_at"] = event.played_at

    return {
        "music": list(grouped["music"].values()),
        "spots": list(grouped["spot"].values()),
        "recent_events": [
            {
                **AudioPlaybackEventRead.model_validate(event).model_dump(),
                "content_name": content_lookup.get(event.content_id).name if event.content_id and event.content_id in content_lookup else "Desconocido",
                "playlist_name": playlist_lookup.get(event.playlist_id).name if event.playlist_id and event.playlist_id in playlist_lookup else None,
            }
            for event in events[:20]
        ],
    }


@router.post("/events", response_model=AudioPlaybackEventRead, status_code=status.HTTP_201_CREATED)
def create_audio_playback_event(
    payload: AudioPlaybackEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AudioPlaybackEvent:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to register audio playback events")

    branch = get_branch_in_scope(db, payload.branch_id, current_user) if payload.branch_id else None
    channel = get_channel_in_scope(db, payload.channel_id, current_user) if payload.channel_id else None
    resolved_client_id = payload.client_id or (branch.client_id if branch else channel.client_id if channel else None)
    target_client_id = require_client_scope(current_user, resolved_client_id)
    if payload.branch_id:
        if branch.client_id != target_client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")
    if payload.channel_id:
        if channel.client_id != target_client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found for this client")

    event = AudioPlaybackEvent(
        client_id=target_client_id,
        branch_id=payload.branch_id,
        channel_id=payload.channel_id,
        playlist_id=payload.playlist_id,
        content_id=payload.content_id,
        entry_kind=payload.entry_kind,
        played_at=payload.played_at or datetime.now(UTC),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.get("/runtime-config")
def get_audio_runtime_config(
    channel_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    channel = get_channel_in_scope(db, channel_id, current_user)

    channel_assignment = db.scalar(select(AudioAssignment).where(AudioAssignment.channel_id == channel.id))
    branch_assignment = db.scalar(
        select(AudioAssignment).where(
            AudioAssignment.branch_id == channel.branch_id,
            AudioAssignment.channel_id.is_(None),
        )
    )
    assignment = channel_assignment if channel_assignment else branch_assignment
    assignment_scope = "channel" if channel_assignment else "branch" if branch_assignment else "none"

    music_playlist, spot_playlist = resolve_assignment_playlists(assignment, db)
    presence = build_presence_snapshot(channel.last_heartbeat_at)

    return {
        "channel": {
            "id": channel.id,
            "name": channel.name,
            "mode": channel.mode.value if isinstance(channel.mode, ChannelMode) else channel.mode,
            "branch_id": channel.branch_id,
            "status": presence["status"],
            "is_online": presence["is_online"],
            "heartbeat_age_seconds": presence["heartbeat_age_seconds"],
            "last_heartbeat_at": channel.last_heartbeat_at.isoformat() if channel.last_heartbeat_at else None,
        },
        "assignment_scope": assignment_scope,
        "audio_enabled": bool(assignment and (music_playlist or spot_playlist)),
        "music_playlist": build_playlist_payload(music_playlist, db),
        "spot_playlist": build_playlist_payload(spot_playlist, db),
        "rules": {
            "songs_between_spots": assignment.songs_between_spots if assignment else 3,
            "spots_per_break": assignment.spots_per_break if assignment else 1,
            "spot_rotation_mode": assignment.spot_rotation_mode.value if assignment else AudioSpotRotationMode.SEQUENTIAL.value,
            "avoid_consecutive_spots": assignment.avoid_consecutive_spots if assignment else True,
        },
        "normalization": {
            "enabled": assignment.volume_normalization_enabled if assignment else False,
            "status": assignment.volume_normalization_status.value if assignment else AudioNormalizationStatus.PENDING.value,
            "target_lufs": assignment.target_lufs if assignment else -14,
        },
    }
