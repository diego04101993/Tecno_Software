from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.public_urls import build_public_base_url
from app.models.entities import (
    AudioAssignment,
    Campaign,
    CampaignChannelAssignment,
    Channel,
    ChannelConnectionStatus,
    PlayerDevice,
    PlayerHeartbeat,
    PlayerPlaybackEvent,
    Schedule,
    TouchExperienceAssignment,
    User,
    Videowall,
    VideowallNode,
)
from app.schemas.domain import ChannelCreate, ChannelRead, ChannelUpdate
from app.schemas.domain import CampaignAssignmentResult, ChannelCampaignAssignmentReplaceRequest
from app.services.output_mapping import normalize_output_mapping
from app.services.presence import is_player_online
from app.services.player_runtime import build_player_runtime_config, resolve_runtime_campaign_context
from app.services.realtime import manager
from app.services.tenancy import (
    apply_client_filter,
    can_write_branch_scope,
    get_branch_in_scope,
    get_channel_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_branch_write_access,
    require_client_scope,
)
from app.services.videowall_geometry import compute_videowall_cell_by_row_col


router = APIRouter()


class HeartbeatPayload(BaseModel):
    current_playback: str | None = None
    status: ChannelConnectionStatus = ChannelConnectionStatus.ONLINE


def join_human_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} y {items[1]}"
    return f"{', '.join(items[:-1])} y {items[-1]}"


def build_delete_block_message(subject: str, dependency_errors: list[str]) -> str:
    if not dependency_errors:
        return f"{subject} puede eliminarse."
    return f"{subject} tiene {join_human_list(dependency_errors)} y no puede eliminarse."


def raise_channel_conflict(exc: IntegrityError) -> None:
    detail = str(exc.orig).lower()
    if "uq_channel_client_name" in detail or ("channels.client_id" in detail and "channels.name" in detail):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe otra pantalla con ese nombre para este cliente") from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se pudo guardar la pantalla por un conflicto de datos") from exc


def get_channel_dependency_errors(
    db: Session,
    channel: Channel,
    *,
    include_videowall_membership: bool = True,
) -> list[str]:
    dependency_errors: list[str] = []
    if db.scalar(select(CampaignChannelAssignment.id).where(CampaignChannelAssignment.channel_id == channel.id).limit(1)):
        dependency_errors.append("campañas asignadas")
    if db.scalar(select(Schedule.id).where(Schedule.channel_id == channel.id).limit(1)):
        dependency_errors.append("programación activa")
    if include_videowall_membership and db.scalar(select(VideowallNode.id).where(VideowallNode.channel_id == channel.id).limit(1)):
        dependency_errors.append("relación con un videowall")
    if db.scalar(select(AudioAssignment.id).where(AudioAssignment.channel_id == channel.id).limit(1)):
        dependency_errors.append("configuración de audio")
    if db.scalar(select(TouchExperienceAssignment.id).where(TouchExperienceAssignment.channel_id == channel.id).limit(1)):
        dependency_errors.append("experiencias touch asignadas")
    if db.scalar(select(PlayerDevice.id).where(PlayerDevice.channel_id == channel.id).limit(1)):
        dependency_errors.append("un player activado")
    return dependency_errors


def get_channel_delete_block_message(
    db: Session,
    channel: Channel,
    *,
    include_videowall_membership: bool = True,
) -> str | None:
    if is_player_online(channel.last_heartbeat_at):
        return "Esta pantalla est\u00e1 en l\u00ednea y no puede eliminarse. Solo puedes eliminar pantallas offline."
    if include_videowall_membership and db.scalar(select(VideowallNode.id).where(VideowallNode.channel_id == channel.id).limit(1)):
        return "Esta pantalla pertenece a un videowall. Elim\u00ednala desde el administrador del videowall."
    return None


def cleanup_channel_delete_dependencies(db: Session, channel: Channel) -> None:
    player_device_ids = list(db.scalars(select(PlayerDevice.id).where(PlayerDevice.channel_id == channel.id)))

    db.execute(delete(CampaignChannelAssignment).where(CampaignChannelAssignment.channel_id == channel.id))
    db.execute(delete(Schedule).where(Schedule.channel_id == channel.id))
    db.execute(delete(AudioAssignment).where(AudioAssignment.channel_id == channel.id))
    db.execute(delete(TouchExperienceAssignment).where(TouchExperienceAssignment.channel_id == channel.id))

    if player_device_ids:
        db.execute(delete(PlayerHeartbeat).where(PlayerHeartbeat.player_device_id.in_(player_device_ids)))
        db.execute(delete(PlayerPlaybackEvent).where(PlayerPlaybackEvent.player_device_id.in_(player_device_ids)))

    db.execute(delete(PlayerHeartbeat).where(PlayerHeartbeat.channel_id == channel.id))
    db.execute(delete(PlayerPlaybackEvent).where(PlayerPlaybackEvent.channel_id == channel.id))
    db.execute(delete(PlayerDevice).where(PlayerDevice.channel_id == channel.id))


@router.get("/", response_model=list[ChannelRead])
def list_channels(
    client_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Channel]:
    query = apply_client_filter(select(Channel).order_by(Channel.created_at.desc()), Channel, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Channel.client_id == target_client_id)
    if branch_id:
        branch = get_branch_in_scope(db, branch_id, current_user)
        query = query.where(Channel.branch_id == branch.id)
    elif is_branch_scoped(current_user):
        query = query.where(Channel.branch_id == require_branch_assignment(current_user))
    return list(db.scalars(query))


@router.post("/", response_model=ChannelRead, status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Channel:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create channels")

    client_id = require_client_scope(current_user, payload.client_id)
    branch = get_branch_in_scope(db, payload.branch_id, current_user)
    if branch.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")

    channel = Channel(
        client_id=client_id,
        branch_id=payload.branch_id,
        name=payload.name.strip(),
        resolution_width=max(1, payload.resolution_width),
        resolution_height=max(1, payload.resolution_height),
        orientation=payload.orientation,
        mode=payload.mode,
        screen_count=max(1, payload.screen_count),
        hardware_identifier=payload.hardware_identifier.strip() if payload.hardware_identifier else None,
        expanded_outputs=payload.expanded_outputs,
        output_mapping_json=normalize_output_mapping(
            payload.output_mapping_json,
            resolution_width=max(1, payload.resolution_width),
            resolution_height=max(1, payload.resolution_height),
        ),
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(channel)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_channel_conflict(exc)
    db.refresh(channel)
    return channel


@router.patch("/{channel_id}", response_model=ChannelRead)
def update_channel(
    channel_id: str,
    payload: ChannelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Channel:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update channels")

    channel = get_channel_in_scope(db, channel_id, current_user)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] is not None:
        channel.name = changes["name"].strip()
    if "resolution_width" in changes and changes["resolution_width"] is not None:
        channel.resolution_width = max(1, changes["resolution_width"])
    if "resolution_height" in changes and changes["resolution_height"] is not None:
        channel.resolution_height = max(1, changes["resolution_height"])
    if "orientation" in changes and changes["orientation"] is not None:
        channel.orientation = changes["orientation"]
    if "screen_count" in changes and changes["screen_count"] is not None:
        channel.screen_count = min(3, max(2, changes["screen_count"])) if channel.mode == "expanded" else 1
    if "hardware_identifier" in changes:
        channel.hardware_identifier = changes["hardware_identifier"].strip() if changes["hardware_identifier"] else None
    if "expanded_outputs" in changes and changes["expanded_outputs"] is not None:
        channel.expanded_outputs = changes["expanded_outputs"]
    if "output_mapping_json" in changes and changes["output_mapping_json"] is not None:
        channel.output_mapping_json = normalize_output_mapping(
            changes["output_mapping_json"],
            resolution_width=channel.resolution_width,
            resolution_height=channel.resolution_height,
        )
    elif "resolution_width" in changes or "resolution_height" in changes:
        channel.output_mapping_json = normalize_output_mapping(
            channel.output_mapping_json,
            resolution_width=channel.resolution_width,
            resolution_height=channel.resolution_height,
        )
    if "notes" in changes:
        channel.notes = changes["notes"].strip() if changes["notes"] else None

    db.add(channel)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_channel_conflict(exc)
    db.refresh(channel)
    return channel


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete channels")

    channel = get_channel_in_scope(db, channel_id, current_user)
    delete_block_message = get_channel_delete_block_message(db, channel)
    if delete_block_message:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=delete_block_message,
        )

    cleanup_channel_delete_dependencies(db, channel)
    db.delete(channel)
    db.commit()


@router.put("/{channel_id}/campaign-assignment", response_model=CampaignAssignmentResult)
def replace_channel_campaign_assignment(
    channel_id: str,
    payload: ChannelCampaignAssignmentReplaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CampaignAssignmentResult:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to replace channel campaigns")

    channel = get_channel_in_scope(db, channel_id, current_user)
    campaign = db.get(Campaign, payload.campaign_id)
    if not campaign or campaign.client_id != channel.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign or channel not found")
    require_client_scope(current_user, campaign.client_id)

    existing_assignments = list(
        db.scalars(
            select(CampaignChannelAssignment)
            .where(CampaignChannelAssignment.channel_id == channel.id)
            .order_by(CampaignChannelAssignment.created_at.desc())
        )
    )
    target_assignment = next((assignment for assignment in existing_assignments if assignment.campaign_id == campaign.id), None)
    assignments_to_remove = [assignment for assignment in existing_assignments if assignment.campaign_id != campaign.id]

    assignment_status: str
    if target_assignment and not assignments_to_remove:
        assignment_status = "existing"
    elif existing_assignments:
        assignment_status = "replaced"
    else:
        assignment_status = "created"

    for assignment in assignments_to_remove:
        db.delete(assignment)

    if not target_assignment:
        target_assignment = CampaignChannelAssignment(
            campaign_id=campaign.id,
            channel_id=channel.id,
            priority=payload.priority,
        )
        db.add(target_assignment)
    else:
        target_assignment.priority = payload.priority
        db.add(target_assignment)

    db.commit()
    db.refresh(target_assignment)

    detail = (
        "La campaña ya estaba publicada en este canal"
        if assignment_status == "existing"
        else "Listo, campaña cambiada"
        if assignment_status == "replaced"
        else "Campaña publicada"
    )
    return CampaignAssignmentResult(
        assignment_id=target_assignment.id,
        campaign_id=campaign.id,
        channel_id=channel.id,
        assignment_status=assignment_status,
        detail=detail,
    )


@router.post("/{channel_id}/heartbeat", response_model=ChannelRead)
async def heartbeat(
    channel_id: str,
    payload: HeartbeatPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Channel:
    require_branch_write_access(current_user)
    channel = get_channel_in_scope(db, channel_id, current_user)
    channel.last_ping_at = datetime.now(UTC)
    channel.status = payload.status
    channel.current_playback = payload.current_playback
    db.add(channel)
    db.commit()
    db.refresh(channel)

    await manager.broadcast_to_client(
        channel.client_id,
        manager.heartbeat_payload(channel_id=channel.id, playback=channel.current_playback),
    )
    return channel


@router.get("/{channel_id}/player-config")
def get_player_config(
    channel_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    channel = get_channel_in_scope(db, channel_id, current_user)

    assignments = list(
        db.scalars(
            select(CampaignChannelAssignment).where(CampaignChannelAssignment.channel_id == channel_id)
        )
    )
    schedules = list(db.scalars(select(Schedule).where(Schedule.channel_id == channel_id)))
    campaigns = [db.get(Campaign, assignment.campaign_id) for assignment in assignments]
    videowall = db.scalar(select(VideowallNode).where(VideowallNode.channel_id == channel_id))

    active_schedule, runtime_campaigns, primary_campaign = resolve_runtime_campaign_context(channel, db)
    active_campaign_payload = runtime_campaigns[0] if runtime_campaigns else None

    videowall_segment = None
    if videowall:
        videowall_wall = db.get(Videowall, videowall.videowall_id)
        if videowall_wall:
            geometry = compute_videowall_cell_by_row_col(
                columns=videowall_wall.columns,
                rows=videowall_wall.rows,
                total_width=videowall_wall.total_width,
                total_height=videowall_wall.total_height,
                row_index=videowall.row_index,
                column_index=videowall.column_index,
            )
            videowall_segment = {
                "videowall_id": videowall.videowall_id,
                "position_index": geometry.position_index,
                "row_index": geometry.row_index,
                "column_index": geometry.column_index,
                "x": geometry.x,
                "y": geometry.y,
                "width": geometry.width,
                "height": geometry.height,
                "crop_x": geometry.x,
                "crop_y": geometry.y,
                "crop_width": geometry.width,
                "crop_height": geometry.height,
            }

    return {
        "channel": ChannelRead.model_validate(channel).model_dump(),
        "campaigns": [
            {
                "assignment_id": assignment.id,
                "priority": assignment.priority,
                "campaign_id": assignment.campaign_id,
                "name": campaign.name if campaign else "Unknown",
                "active_from": assignment.active_from,
                "active_until": assignment.active_until,
            }
            for assignment, campaign in zip(assignments, campaigns)
        ],
        "schedules": [schedule.id for schedule in schedules],
        "videowall_segment": videowall_segment,
        "active_campaign": {
            "campaign_id": primary_campaign.id,
            "name": primary_campaign.name,
            "source": active_campaign_payload.get("source") if active_campaign_payload else None,
            "priority": active_campaign_payload.get("priority") if active_campaign_payload else None,
            "schedule_id": active_campaign_payload.get("schedule_id") if active_campaign_payload else active_schedule.id if active_schedule else None,
            "assignment_id": active_campaign_payload.get("assignment_id") if active_campaign_payload else None,
        }
        if primary_campaign
        else None,
    }


@router.get("/{channel_id}/runtime-config")
def get_channel_runtime_config(
    channel_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    channel = get_channel_in_scope(db, channel_id, current_user)
    return build_player_runtime_config(channel, db, build_public_base_url(request))
