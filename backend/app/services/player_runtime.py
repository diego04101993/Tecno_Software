from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.api.routes.touch import serialize_runtime_config as serialize_touch_runtime_config
from app.core.config import get_settings
from app.core.public_urls import build_absolute_public_url
from app.models.entities import (
    AudioAssignment,
    AudioPlaylist,
    AudioPlaylistItem,
    Branch,
    Campaign,
    CampaignChannelAssignment,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    Channel,
    ChannelMode,
    Client,
    ContentItem,
    Layout,
    LayoutDataBinding,
    LayoutRevision,
    Schedule,
    TouchExperience,
    TouchExperienceAssignment,
    Videowall,
    VideowallNode,
)
from app.services.campaign_sequence import normalize_playback_mode, resolve_effective_sequence_items, sequence_item_zone_key
from app.services.layout_bindings import build_layout_data_preview
from app.services.layout_editor import build_layout_preview_state, get_current_draft_revision, get_current_published_revision
from app.services.output_mapping import normalize_output_mapping
from app.services.presence import build_presence_snapshot
from app.services.videowall_geometry import compute_videowall_cell_by_row_col


settings = get_settings()
RUNTIME_VERSION = 2
SYNC_LEAD_MS = 5000


def now_utc() -> datetime:
    return datetime.now(UTC)


def build_server_time_payload() -> dict[str, object]:
    current_time = now_utc()
    return {
        "server_time": current_time.isoformat(),
        "unix_timestamp": int(current_time.timestamp()),
        "timezone": "UTC",
    }


def _absolute_url(base_url: str, path: str | None) -> str | None:
    return build_absolute_public_url(base_url, path)


def _resolve_media_size(file_path: str | None) -> int | None:
    if not file_path or not file_path.startswith("/media/"):
        return None

    relative_path = file_path.removeprefix("/media/")
    absolute_path = settings.MEDIA_ROOT / Path(relative_path)
    if not absolute_path.exists():
        return None
    return absolute_path.stat().st_size


def serialize_client(client: Client) -> dict[str, object]:
    return {
        "id": client.id,
        "name": client.name,
        "slug": client.slug,
        "brand_name": client.brand_name,
        "status": client.status,
        "contact_email": client.contact_email,
    }


def serialize_branch(branch: Branch) -> dict[str, object]:
    return {
        "id": branch.id,
        "client_id": branch.client_id,
        "name": branch.name,
        "code": branch.code,
        "address": branch.address,
        "timezone": branch.timezone,
        "is_active": branch.is_active,
    }


def serialize_channel(channel: Channel) -> dict[str, object]:
    presence = build_presence_snapshot(channel.last_heartbeat_at)
    return {
        "id": channel.id,
        "client_id": channel.client_id,
        "branch_id": channel.branch_id,
        "name": channel.name,
        "channel_code": channel.channel_code,
        "resolution_width": channel.resolution_width,
        "resolution_height": channel.resolution_height,
        "orientation": channel.orientation.value,
        "mode": channel.mode.value,
        "screen_count": channel.screen_count,
        "status": presence["status"],
        "is_online": presence["is_online"],
        "heartbeat_age_seconds": presence["heartbeat_age_seconds"],
        "current_playback": channel.current_playback,
        "hardware_identifier": channel.hardware_identifier,
        "expanded_outputs": channel.expanded_outputs,
        "output_mapping_json": normalize_output_mapping(
            channel.output_mapping_json,
            resolution_width=channel.resolution_width,
            resolution_height=channel.resolution_height,
        ),
        "last_ping_at": channel.last_heartbeat_at.isoformat() if channel.last_heartbeat_at else None,
        "last_heartbeat_at": channel.last_heartbeat_at.isoformat() if channel.last_heartbeat_at else None,
        "notes": channel.notes,
    }


def serialize_content(content: ContentItem, base_url: str) -> dict[str, object]:
    metadata = content.metadata_json or {}
    return {
        "id": content.id,
        "name": content.name,
        "type": content.type.value,
        "file_path": content.file_path,
        "download_url": _absolute_url(base_url, content.file_path),
        "source_url": content.source_url,
        "html_content": content.html_content,
        "text_content": content.text_content,
        "mime_type": metadata.get("content_type"),
        "duration_seconds": content.duration_seconds,
        "checksum": metadata.get("checksum"),
        "size": metadata.get("size") or metadata.get("size_bytes") or _resolve_media_size(content.file_path),
        "metadata_json": metadata,
    }


def serialize_schedule(schedule: Schedule | None) -> dict[str, object] | None:
    if not schedule:
        return None

    return {
        "id": schedule.id,
        "campaign_id": schedule.campaign_id,
        "channel_id": schedule.channel_id,
        "branch_id": schedule.branch_id,
        "layout_id": schedule.layout_id,
        "title": schedule.title,
        "recurrence": schedule.recurrence.value,
        "days_of_week": schedule.days_of_week,
        "starts_on": schedule.starts_on.isoformat() if schedule.starts_on else None,
        "ends_on": schedule.ends_on.isoformat() if schedule.ends_on else None,
        "start_time": schedule.start_time.isoformat() if schedule.start_time else None,
        "end_time": schedule.end_time.isoformat() if schedule.end_time else None,
        "is_active": schedule.is_active,
        "is_looping": schedule.is_looping,
        "timezone": schedule.timezone,
        "priority": schedule.priority,
    }


def _is_schedule_active(schedule: Schedule, current_time: datetime) -> bool:
    if not schedule.is_active:
        return False
    try:
        local_now = current_time.astimezone(ZoneInfo(schedule.timezone or "UTC"))
    except Exception:
        local_now = current_time.astimezone(UTC)
    current_date = local_now.date()
    current_day = local_now.isoweekday()

    if schedule.starts_on and current_date < schedule.starts_on:
        return False
    if schedule.ends_on and current_date > schedule.ends_on:
        return False
    if schedule.days_of_week and current_day not in schedule.days_of_week:
        return False
    if schedule.start_time and local_now.time() < schedule.start_time:
        return False
    if schedule.end_time and local_now.time() > schedule.end_time:
        return False
    return True


def _resolve_active_schedule(channel: Channel, db: Session) -> Schedule | None:
    candidate_schedules = list(
        db.scalars(
            select(Schedule)
            .where(
                Schedule.client_id == channel.client_id,
                or_(
                    Schedule.channel_id == channel.id,
                    and_(Schedule.branch_id == channel.branch_id, Schedule.channel_id.is_(None)),
                ),
            )
            .order_by(Schedule.priority.desc(), Schedule.created_at.desc())
        )
    )
    active_schedules = [schedule for schedule in candidate_schedules if _is_schedule_active(schedule, now_utc())]
    if not active_schedules:
        return None

    active_schedules.sort(
        key=lambda item: (
            1 if item.channel_id == channel.id else 0,
            item.priority,
            item.created_at.timestamp() if item.created_at else 0,
        ),
        reverse=True,
    )
    return active_schedules[0]


def _resolve_active_assignments(channel: Channel, db: Session) -> list[CampaignChannelAssignment]:
    current_time = now_utc()
    assignments = list(
        db.scalars(
            select(CampaignChannelAssignment)
            .where(CampaignChannelAssignment.channel_id == channel.id)
            .order_by(CampaignChannelAssignment.priority.desc(), CampaignChannelAssignment.created_at.desc())
        )
    )
    resolved: list[CampaignChannelAssignment] = []
    for assignment in assignments:
        if assignment.active_from and assignment.active_from > current_time:
            continue
        if assignment.active_until and assignment.active_until < current_time:
            continue
        resolved.append(assignment)
    return resolved


def _resolve_runtime_campaigns(
    channel: Channel,
    db: Session,
) -> tuple[Schedule | None, list[dict[str, object]], Campaign | None]:
    active_schedule = _resolve_active_schedule(channel, db)
    active_assignments = _resolve_active_assignments(channel, db)

    campaigns_payload: list[dict[str, object]] = []
    seen_campaign_ids: set[str] = set()
    primary_campaign: Campaign | None = None

    if active_schedule:
        schedule_campaign = db.get(Campaign, active_schedule.campaign_id)
        if schedule_campaign:
            primary_campaign = schedule_campaign
            seen_campaign_ids.add(schedule_campaign.id)
            playlist_count = len(resolve_effective_sequence_items(schedule_campaign.id, db))
            campaigns_payload.append(
                {
                    "campaign_id": schedule_campaign.id,
                    "name": schedule_campaign.name,
                    "description": schedule_campaign.description,
                    "layout_id": schedule_campaign.layout_id,
                    "source": "schedule",
                    "priority": active_schedule.priority,
                    "playlist_items_count": playlist_count,
                    "schedule_id": active_schedule.id,
                }
            )

    for assignment in active_assignments:
        campaign = db.get(Campaign, assignment.campaign_id)
        if not campaign or campaign.id in seen_campaign_ids:
            continue
        if primary_campaign is None:
            primary_campaign = campaign
        seen_campaign_ids.add(campaign.id)
        playlist_count = len(resolve_effective_sequence_items(campaign.id, db))
        campaigns_payload.append(
            {
                "campaign_id": campaign.id,
                "name": campaign.name,
                "description": campaign.description,
                "layout_id": campaign.layout_id,
                "source": "assignment",
                "priority": assignment.priority,
                "playlist_items_count": playlist_count,
                "assignment_id": assignment.id,
            }
        )

    return active_schedule, campaigns_payload, primary_campaign


def _resolve_output_mapping(channel: Channel) -> dict[str, object]:
    return normalize_output_mapping(
        channel.output_mapping_json,
        resolution_width=channel.resolution_width,
        resolution_height=channel.resolution_height,
    )


def resolve_runtime_campaign_context(
    channel: Channel,
    db: Session,
) -> tuple[Schedule | None, list[dict[str, object]], Campaign | None]:
    return _resolve_runtime_campaigns(channel, db)


def _serialize_runtime_layout_item(layout: Layout | None) -> dict[str, object] | None:
    if not layout:
        return None

    return {
        "id": layout.id,
        "name": layout.name,
        "template": layout.template.value,
        "canvas_width": layout.canvas_width,
        "canvas_height": layout.canvas_height,
        "zones": layout.zones,
        "is_default": layout.is_default,
    }


def _build_sequence_runtime(
    campaign: Campaign | None,
    db: Session,
    base_url: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    if not campaign:
        return [], [], []

    sequence_items = resolve_effective_sequence_items(campaign.id, db)
    contents = {item.content_id: db.get(ContentItem, item.content_id) for item in sequence_items if item.content_id}
    layouts = {item.layout_id: db.get(Layout, item.layout_id) for item in sequence_items if item.layout_id}
    serialized_contents: list[dict[str, object]] = []
    seen_content_ids: set[str] = set()

    sequence_payload = []
    playlist_payload = []
    for item in sequence_items:
        content = contents.get(item.content_id) if item.content_id else None
        layout = layouts.get(item.layout_id) if item.layout_id else None
        content_payload = serialize_content(content, base_url) if content else None
        layout_payload = _serialize_runtime_layout_item(layout)
        if content and content.id not in seen_content_ids:
            serialized_contents.append(content_payload)
            seen_content_ids.add(content.id)

        sequence_payload.append(
            {
                "id": item.id,
                "campaign_id": item.campaign_id,
                "item_type": item.item_type.value if hasattr(item.item_type, "value") else item.item_type,
                "content_id": item.content_id,
                "layout_id": item.layout_id,
                "sort_order": item.sort_order,
                "duration_seconds": item.duration_seconds,
                "options_json": item.options_json or {},
                "is_enabled": item.is_enabled,
                "content": content_payload,
                "layout": layout_payload,
            }
        )

        if item.is_enabled and item.item_type == CampaignSequenceItemType.CONTENT and content_payload is not None:
            playlist_payload.append(
                {
                    "id": item.id,
                    "campaign_id": item.campaign_id,
                    "content_id": item.content_id,
                    "sort_order": item.sort_order,
                    "duration_seconds": item.duration_seconds,
                    "zone_key": sequence_item_zone_key(item),
                    "content": content_payload,
                }
            )

    return sequence_payload, playlist_payload, serialized_contents


def _resolve_layout_runtime(layout: Layout | None, db: Session) -> tuple[dict[str, object] | None, dict[str, object] | None, dict[str, object] | None]:
    if not layout:
        return None, None, None

    published_revision = get_current_published_revision(db, layout)
    selected_revision = published_revision or get_current_draft_revision(db, layout)
    if not selected_revision:
        return {
            "id": layout.id,
            "name": layout.name,
            "template": layout.template.value,
            "canvas_width": layout.canvas_width,
            "canvas_height": layout.canvas_height,
            "zones": layout.zones,
            "is_default": layout.is_default,
        }, None, None

    preview_state = selected_revision.preview_state_json or build_layout_preview_state(layout, selected_revision.editor_state_json, db)
    bindings = list(
        db.scalars(
            select(LayoutDataBinding)
            .where(LayoutDataBinding.layout_id == layout.id, LayoutDataBinding.is_active.is_(True))
            .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
        )
    )
    runtime_data = build_layout_data_preview(layout, bindings, db) if bindings else None
    if runtime_data is not None:
        runtime_data["runtime_version"] = 1
        runtime_data["player_ready"] = False

    layout_payload = {
        "id": layout.id,
        "name": layout.name,
        "template": layout.template.value,
        "canvas_width": layout.canvas_width,
        "canvas_height": layout.canvas_height,
        "zones": layout.zones,
        "is_default": layout.is_default,
    }
    revision_payload = {
        "id": selected_revision.id,
        "revision_number": selected_revision.revision_number,
        "name": selected_revision.name,
        "status": selected_revision.status.value if hasattr(selected_revision.status, "value") else selected_revision.status,
        "published_at": selected_revision.published_at.isoformat() if selected_revision.published_at else None,
        "preview_state_json": preview_state,
    }
    return layout_payload, revision_payload, runtime_data


def _resolve_videowall_runtime(channel: Channel, db: Session) -> dict[str, object]:
    node = db.scalar(select(VideowallNode).where(VideowallNode.channel_id == channel.id))
    if not node:
        return {"enabled": False}

    videowall = db.get(Videowall, node.videowall_id)
    if not videowall:
        return {"enabled": False}

    sync_time = now_utc()
    play_at_timestamp = int((sync_time + timedelta(milliseconds=SYNC_LEAD_MS)).timestamp() * 1000)
    geometry = compute_videowall_cell_by_row_col(
        columns=videowall.columns,
        rows=videowall.rows,
        total_width=videowall.total_width,
        total_height=videowall.total_height,
        row_index=node.row_index,
        column_index=node.column_index,
    )

    return {
        "enabled": True,
        "wall_id": videowall.id,
        "videowall_id": videowall.id,
        "name": videowall.name,
        "columns": videowall.columns,
        "rows": videowall.rows,
        "total_width": videowall.total_width,
        "total_height": videowall.total_height,
        "sync_group": videowall.id,
        "sync_mode": videowall.sync_mode,
        "start_tolerance_ms": videowall.start_tolerance_ms,
        "server_time": sync_time.isoformat(),
        "play_at_timestamp": play_at_timestamp,
        "sync": {
            "mode": "wall_clock",
            "loop_start_epoch_ms": None,
            "tolerance_ms": videowall.start_tolerance_ms,
        },
        "node": {
            "position_index": geometry.position_index,
            "row": geometry.row_index,
            "column": geometry.column_index,
            "row_index": geometry.row_index,
            "column_index": geometry.column_index,
            "width": geometry.width,
            "height": geometry.height,
            "crop_x": geometry.x,
            "crop_y": geometry.y,
            "crop_width": geometry.width,
            "crop_height": geometry.height,
            "crop": {
                "x": geometry.x,
                "y": geometry.y,
                "width": geometry.width,
                "height": geometry.height,
            },
        },
    }


def _build_audio_playlist_payload(playlist: AudioPlaylist | None, db: Session, base_url: str) -> dict[str, object] | None:
    if not playlist:
        return None

    items = list(
        db.scalars(
            select(AudioPlaylistItem)
            .where(AudioPlaylistItem.playlist_id == playlist.id, AudioPlaylistItem.is_enabled.is_(True))
            .order_by(AudioPlaylistItem.sort_order.asc())
        )
    )
    contents = {item.content_id: db.get(ContentItem, item.content_id) for item in items}

    return {
        "id": playlist.id,
        "name": playlist.name,
        "kind": playlist.kind.value,
        "description": playlist.description,
        "is_active": playlist.is_active,
        "items": [
            {
                "id": item.id,
                "content_id": item.content_id,
                "sort_order": item.sort_order,
                "is_enabled": item.is_enabled,
                "content": serialize_content(contents[item.content_id], base_url) if contents.get(item.content_id) else None,
            }
            for item in items
        ],
    }


def _resolve_audio_runtime(channel: Channel, db: Session, base_url: str) -> dict[str, object] | None:
    channel_assignment = db.scalar(select(AudioAssignment).where(AudioAssignment.channel_id == channel.id))
    branch_assignment = db.scalar(
        select(AudioAssignment).where(
            AudioAssignment.branch_id == channel.branch_id,
            AudioAssignment.channel_id.is_(None),
        )
    )
    assignment = channel_assignment or branch_assignment
    if not assignment:
        return None

    music_playlist = db.get(AudioPlaylist, assignment.music_playlist_id) if assignment.music_playlist_id else None
    spot_playlist = db.get(AudioPlaylist, assignment.spot_playlist_id) if assignment.spot_playlist_id else None

    return {
        "assignment_scope": "channel" if channel_assignment else "branch",
        "audio_enabled": bool(music_playlist or spot_playlist),
        "music_playlist": _build_audio_playlist_payload(music_playlist, db, base_url),
        "spot_playlist": _build_audio_playlist_payload(spot_playlist, db, base_url),
        "rules": {
            "songs_between_spots": assignment.songs_between_spots,
            "spots_per_break": assignment.spots_per_break,
            "spot_rotation_mode": assignment.spot_rotation_mode.value,
            "avoid_consecutive_spots": assignment.avoid_consecutive_spots,
        },
        "normalization": {
            "enabled": assignment.volume_normalization_enabled,
            "status": assignment.volume_normalization_status.value,
            "target_lufs": assignment.target_lufs,
        },
    }


def _resolve_touch_runtime(channel: Channel, db: Session) -> dict[str, object] | None:
    assignment = db.scalar(
        select(TouchExperienceAssignment)
        .where(
            TouchExperienceAssignment.channel_id == channel.id,
            TouchExperienceAssignment.is_active.is_(True),
        )
        .order_by(TouchExperienceAssignment.sort_order.asc(), TouchExperienceAssignment.created_at.asc())
    )
    assignment_scope = "channel"
    if not assignment:
        assignment = db.scalar(
            select(TouchExperienceAssignment)
            .where(
                TouchExperienceAssignment.branch_id == channel.branch_id,
                TouchExperienceAssignment.channel_id.is_(None),
                TouchExperienceAssignment.is_active.is_(True),
            )
            .order_by(TouchExperienceAssignment.sort_order.asc(), TouchExperienceAssignment.created_at.asc())
        )
        assignment_scope = "branch"

    if not assignment:
        return None

    experience = db.get(TouchExperience, assignment.experience_id)
    if not experience:
        return None

    payload = serialize_touch_runtime_config(experience, db)
    payload["assignment_scope"] = assignment_scope
    return payload


def _fallback_payload(reason: str) -> dict[str, object]:
    return {
        "type": "black_screen",
        "reason": reason,
    }


def _build_runtime_signature(
    campaign: Campaign | None,
    playback_mode: str,
    sequence_payload: list[dict[str, object]],
) -> str:
    signature_source = {
        "campaign_id": campaign.id if campaign else None,
        "playback_mode": playback_mode,
        "loop_enabled": campaign.loop_enabled if campaign else True,
        "sequence": [
            {
                "id": item.get("id"),
                "item_type": item.get("item_type"),
                "content_id": item.get("content_id"),
                "layout_id": item.get("layout_id"),
                "sort_order": item.get("sort_order"),
                "duration_seconds": item.get("duration_seconds"),
                "is_enabled": item.get("is_enabled"),
                "zone_key": item.get("options_json", {}).get("zone_key") if isinstance(item.get("options_json"), dict) else None,
            }
            for item in sequence_payload
        ],
    }
    digest = hashlib.sha256(json.dumps(signature_source, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:24]


def build_campaign_sequence_preview_payload(
    campaign: Campaign,
    db: Session,
    base_url: str,
) -> dict[str, object]:
    generated_at = now_utc()
    playback_mode = normalize_playback_mode(campaign.playback_mode)
    sequence_models = resolve_effective_sequence_items(campaign.id, db)
    sequence_payload, playlist_payload, contents_payload = _build_sequence_runtime(campaign, db, base_url)
    campaign_layout = db.get(Layout, campaign.layout_id) if campaign.layout_id else None
    campaign_layout_payload = _serialize_runtime_layout_item(campaign_layout)
    sequence_model_by_id = {item.id: item for item in sequence_models}

    layout_by_id: dict[str, dict[str, object]] = {}
    if campaign_layout_payload:
        layout_by_id[campaign_layout_payload["id"]] = campaign_layout_payload
    for item in sequence_payload:
        layout_payload = item.get("layout")
        if isinstance(layout_payload, dict) and isinstance(layout_payload.get("id"), str):
            layout_by_id[layout_payload["id"]] = layout_payload

    resolved_items = []
    duration_total = 0
    for item in sequence_payload:
        model = sequence_model_by_id.get(item["id"]) if item.get("id") else None
        if item.get("is_enabled"):
            duration_total += int(item.get("duration_seconds") or 0)
        resolved_items.append(
            {
                **item,
                "zone_key": sequence_item_zone_key(model) if model else "main",
            }
        )

    return {
        "campaign": {
            "id": campaign.id,
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
            "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
            "client_id": campaign.client_id,
            "layout_id": campaign.layout_id,
            "name": campaign.name,
            "description": campaign.description,
            "default_duration_seconds": campaign.default_duration_seconds,
            "is_active": campaign.is_active,
            "loop_enabled": campaign.loop_enabled,
            "playback_mode": playback_mode,
        },
        "playback_mode": playback_mode,
        "loop_enabled": True,
        "sequence_items": [
            {
                "id": item.get("id"),
                "created_at": sequence_model_by_id[item["id"]].created_at.isoformat()
                if item.get("id") and sequence_model_by_id.get(item["id"]) and sequence_model_by_id[item["id"]].created_at
                else None,
                "updated_at": sequence_model_by_id[item["id"]].updated_at.isoformat()
                if item.get("id") and sequence_model_by_id.get(item["id"]) and sequence_model_by_id[item["id"]].updated_at
                else None,
                "campaign_id": item.get("campaign_id"),
                "item_type": item.get("item_type"),
                "content_id": item.get("content_id"),
                "layout_id": item.get("layout_id"),
                "sort_order": item.get("sort_order"),
                "duration_seconds": item.get("duration_seconds"),
                "options_json": item.get("options_json") or {},
                "is_enabled": item.get("is_enabled"),
            }
            for item in sequence_payload
        ],
        "resolved_items": resolved_items,
        "contents": contents_payload,
        "layouts": list(layout_by_id.values()),
        "campaign_layout": campaign_layout_payload,
        "duration_total": duration_total,
        "runtime_signature": _build_runtime_signature(campaign, playback_mode, sequence_payload),
        "generated_at": generated_at.isoformat(),
        "playlist_legacy": playlist_payload,
    }


def build_player_runtime_config(channel: Channel, db: Session, base_url: str) -> dict[str, object]:
    generated_at = now_utc()
    play_at_timestamp = int((generated_at + timedelta(milliseconds=SYNC_LEAD_MS)).timestamp() * 1000)

    client = db.get(Client, channel.client_id)
    branch = db.get(Branch, channel.branch_id)
    schedule_current, campaigns_payload, primary_campaign = _resolve_runtime_campaigns(channel, db)
    sequence_payload, playlist_payload, contents_payload = _build_sequence_runtime(primary_campaign, db, base_url)

    resolved_layout_id = (schedule_current.layout_id if schedule_current else None) or (primary_campaign.layout_id if primary_campaign else None)
    layout = db.get(Layout, resolved_layout_id) if resolved_layout_id else None
    layout_payload, layout_revision_payload, dataset_runtime_data = _resolve_layout_runtime(layout, db)

    videowall_payload = _resolve_videowall_runtime(channel, db)
    audio_payload = _resolve_audio_runtime(channel, db, base_url)
    touch_payload = _resolve_touch_runtime(channel, db)
    output_mapping_payload = _resolve_output_mapping(channel)

    effective_mode = channel.mode.value
    if channel.mode == ChannelMode.TOUCH:
        effective_mode = ChannelMode.TOUCH.value

    if effective_mode == ChannelMode.AUDIO.value and audio_payload:
        contents_payload = []
        for playlist in [audio_payload.get("music_playlist"), audio_payload.get("spot_playlist")]:
            if not playlist:
                continue
            for item in playlist.get("items", []):
                content = item.get("content")
                if isinstance(content, dict) and content.get("id") not in {entry["id"] for entry in contents_payload if isinstance(entry, dict) and "id" in entry}:
                    contents_payload.append(content)

    fallback_reason = "no_active_content"
    if touch_payload and effective_mode == ChannelMode.TOUCH.value:
        fallback_reason = "touch_runtime_pending"
    elif audio_payload and effective_mode == ChannelMode.AUDIO.value:
        fallback_reason = "audio_runtime_pending"
    elif playlist_payload:
        fallback_reason = ""

    return {
        "runtime_version": RUNTIME_VERSION,
        "runtime_generated_at": generated_at.isoformat(),
        "server_time": generated_at.isoformat(),
        "play_at_timestamp": play_at_timestamp,
        "channel": serialize_channel(channel),
        "branch": serialize_branch(branch) if branch else None,
        "client": serialize_client(client) if client else None,
        "mode": effective_mode,
        "playback_mode": normalize_playback_mode(primary_campaign.playback_mode if primary_campaign else None),
        "schedule": serialize_schedule(schedule_current),
        "campaigns": campaigns_payload,
        "sequence_items": sequence_payload,
        "playlist": playlist_payload,
        "contents": contents_payload,
        "layout": layout_payload,
        "layout_runtime_snapshot": layout_revision_payload,
        "dataset_runtime_data": dataset_runtime_data,
        "videowall": videowall_payload,
        "output_mapping": output_mapping_payload,
        "audio": audio_payload,
        "touch": touch_payload,
        "fallback": _fallback_payload(fallback_reason) if fallback_reason else None,
    }
