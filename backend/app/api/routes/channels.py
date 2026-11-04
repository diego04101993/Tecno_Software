from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.api.deps import get_current_user, get_db
from app.core.public_urls import build_public_base_url
from app.models.entities import (
    AudioAssignment,
    AudioPlaybackEvent,
    Campaign,
    CampaignChannelAssignment,
    Channel,
    ChannelConnectionStatus,
    ChannelMode,
    DeviceMode,
    DeviceOutput,
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
from app.services.device_outputs import normalize_channel_device_fields, sync_channel_device_outputs, plan_device_outputs
from app.services.output_mapping import normalize_output_mapping
from app.services.presence import is_player_online
from app.services.player_runtime import build_player_runtime_from_snapshot, resolve_runtime_campaign_context
from app.services.realtime import manager
from app.services.tenancy import (
    channel_read_integrity,
    assert_related_clients,
    safe_tenant_resource, require_active_channel_tenant,
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


from app.services.tenant_mutations import delete_guard, require_delete_coverage, write_guard, MutationIntent
from app.services.tenant_ownership import RowRef


from app.services.tenant_responses import read_snapshot, safe_model_read, playback_label_for_read
from app.services.tenant_queries import readable_query

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


def channel_belongs_to_videowall(db: Session, channel_id: str) -> bool:
    return bool(
        db.scalar(select(VideowallNode.id).where(VideowallNode.channel_id == channel_id).limit(1))
        or db.scalar(select(Videowall.id).where(Videowall.primary_channel_id == channel_id).limit(1))
    )


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
    if include_videowall_membership and channel_belongs_to_videowall(db, channel.id):
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
    if include_videowall_membership and channel_belongs_to_videowall(db, channel.id):
        return "Esta pantalla pertenece a un videowall. Elim\u00ednala desde el administrador del videowall."
    return None


def preflight_channel_videowall(db: Session, channel: Channel) -> None:
    with db.no_autoflush:
        assert_related_clients(channel.client_id, db.scalars(
            select(Videowall.client_id).where(Videowall.primary_channel_id == channel.id)))
        assert_related_clients(channel.client_id, db.scalars(
            select(Videowall.client_id).select_from(VideowallNode)
            .outerjoin(Videowall, Videowall.id == VideowallNode.videowall_id)
            .where(VideowallNode.channel_id == channel.id)))


def preflight_channel_delete(db: Session, channel: Channel) -> None:
    """Check explicit cleanup, cascades and SET NULL before touching any row."""
    with db.no_autoflush:
        for model in (AudioAssignment, TouchExperienceAssignment, Schedule, AudioPlaybackEvent, PlayerDevice):
            assert_related_clients(channel.client_id, db.scalars(
                select(model.client_id).where(model.channel_id == channel.id)))
        player_ids = select(PlayerDevice.id).where(PlayerDevice.channel_id == channel.id)
        for model in (PlayerHeartbeat, PlayerPlaybackEvent):
            assert_related_clients(channel.client_id, db.scalars(select(model.client_id).where(or_(
                model.channel_id == channel.id, model.player_device_id.in_(player_ids)))))
        for model in (CampaignChannelAssignment, Schedule):
            assert_related_clients(channel.client_id, db.scalars(
                select(Campaign.client_id).select_from(model)
                .outerjoin(Campaign, Campaign.id == model.campaign_id).where(model.channel_id == channel.id)))
        preflight_channel_videowall(db, channel)
        root, target = aliased(Channel), aliased(Channel)
        outputs = db.execute(select(root.client_id, target.client_id, DeviceOutput.channel_id)
            .select_from(DeviceOutput).outerjoin(root, root.id == DeviceOutput.root_channel_id)
            .outerjoin(target, target.id == DeviceOutput.channel_id).where(or_(
                DeviceOutput.root_channel_id == channel.id, DeviceOutput.channel_id == channel.id)))
        for root_owner, target_owner, target_id in outputs:
            assert_related_clients(channel.client_id, [root_owner])
            if target_id is not None:
                assert_related_clients(channel.client_id, [target_owner])


def cleanup_channel_delete_dependencies(db: Session, channel: Channel) -> None:
    require_delete_coverage(db, (channel,))
    preflight_channel_delete(db, channel)
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
@read_snapshot(response_model=list[ChannelRead])
def list_channels(
    client_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Channel]:
    query = apply_client_filter(readable_query(select(Channel), Channel, db=db).where(channel_read_integrity()).order_by(Channel.created_at.desc()), Channel, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Channel.client_id == target_client_id)
    if branch_id:
        branch = get_branch_in_scope(db, branch_id, current_user)
        query = query.where(Channel.branch_id == branch.id, Channel.client_id == branch.client_id)
    elif is_branch_scoped(current_user):
        query = query.where(Channel.branch_id == require_branch_assignment(current_user))
    return list(db.scalars(query))


def validate_rotation_scope(rotation: int | None, mode: ChannelMode, device_mode: DeviceMode) -> None:
    if rotation is not None and (mode != ChannelMode.NORMAL or device_mode != DeviceMode.SINGLE):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rotation can only be set on normal/single channels")


_OUTPUT_FIELDS = ("id", "root_channel_id", "name", "display_order", "display_uid", "channel_id",
                  "x", "y", "width", "height", "source_x", "source_y", "source_width", "source_height",
                  "rotation", "orientation", "sync_group", "enabled")
_DEVICE_FIELDS = ("device_mode", "screen_count", "canvas_width", "canvas_height", "expanded_outputs")


def _output_write_intents(channel_id, definitions, existing):
    if definitions is None:
        return ()
    previous = {output.id for output in existing}
    retained, intents = set(), []
    for definition in definitions:
        identity = definition["id"]
        retained.add(identity)
        values = {key: value for key, value in definition.items() if key != "id"}
        values["root_channel_id"] = channel_id
        intents.append(MutationIntent("UPDATE" if identity in previous else "CREATE",
                                      RowRef("DeviceOutput", identity), tuple(values.items())))
    intents.extend(MutationIntent("DELETE", RowRef("DeviceOutput", identity)) for identity in sorted(previous - retained))
    return tuple(intents)


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

    validate_rotation_scope(payload.rotation, payload.mode, payload.device_mode)
    values = dict(
        client_id=client_id,
        branch_id=payload.branch_id,
        name=payload.name.strip(),
        resolution_width=max(1, payload.resolution_width),
        resolution_height=max(1, payload.resolution_height),
        orientation=payload.orientation,
        mode=payload.mode,
        hardware_identifier=payload.hardware_identifier.strip() if payload.hardware_identifier else None,
        output_mapping_json=normalize_output_mapping(
            payload.output_mapping_json,
            resolution_width=max(1, payload.resolution_width),
            resolution_height=max(1, payload.resolution_height),
        ),
        notes=payload.notes.strip() if payload.notes else None,
    )
    ref = RowRef("Channel", str(uuid4()))
    proposed_channel = SimpleNamespace(id=ref.identity, device_outputs=[], device_mode=None,
        screen_count=None, canvas_width=None, canvas_height=None, **values)
    normalize_channel_device_fields(
        proposed_channel,
        requested_device_mode=payload.device_mode,
        requested_screen_count=payload.screen_count,
        requested_canvas_width=payload.canvas_width,
        requested_canvas_height=payload.canvas_height,
    )
    values.update({field: getattr(proposed_channel, field) for field in _DEVICE_FIELDS})
    definitions = plan_device_outputs(proposed_channel, rebuild=True, rotation=payload.rotation)
    intents = (MutationIntent("CREATE", ref, tuple(values.items())),) + _output_write_intents(ref.identity, definitions, ())
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_channel", intents=intents):
        channel = Channel(id=ref.identity, **values)
        sync_channel_device_outputs(channel, definitions=definitions)
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
    validate_rotation_scope(
        payload.rotation,
        changes.get("mode") or channel.mode,
        changes.get("device_mode") or channel.device_mode,
    )
    device_fields = {
        "resolution_width", "resolution_height", "orientation", "mode", "device_mode",
        "screen_count", "canvas_width", "canvas_height",
    }
    device_configuration_changed = any(
        field in changes and changes[field] is not None and changes[field] != getattr(channel, field)
        for field in device_fields
    )

    # Prepare on plain copies. No ORM setter runs while deriving the batch.
    copied_fields = ("id", "client_id", "branch_id", "name", "resolution_width", "resolution_height",
                     "orientation", "mode", "hardware_identifier", "output_mapping_json", "notes", *_DEVICE_FIELDS)
    previous = {field: getattr(channel, field) for field in copied_fields}
    old_outputs = [SimpleNamespace(**{field: getattr(output, field) for field in _OUTPUT_FIELDS})
                   for output in channel.device_outputs]
    proposed_channel = SimpleNamespace(**previous, device_outputs=old_outputs)
    if "name" in changes and changes["name"] is not None:
        proposed_channel.name = changes["name"].strip()
    if "resolution_width" in changes and changes["resolution_width"] is not None:
        proposed_channel.resolution_width = max(1, changes["resolution_width"])
    if "resolution_height" in changes and changes["resolution_height"] is not None:
        proposed_channel.resolution_height = max(1, changes["resolution_height"])
    if "orientation" in changes and changes["orientation"] is not None:
        proposed_channel.orientation = changes["orientation"]
    if "mode" in changes and changes["mode"] is not None:
        proposed_channel.mode = changes["mode"]
    if "hardware_identifier" in changes:
        proposed_channel.hardware_identifier = changes["hardware_identifier"].strip() if changes["hardware_identifier"] else None
    if "output_mapping_json" in changes and changes["output_mapping_json"] is not None:
        proposed_channel.output_mapping_json = normalize_output_mapping(
            changes["output_mapping_json"],
            resolution_width=proposed_channel.resolution_width,
            resolution_height=proposed_channel.resolution_height,
        )
    if "notes" in changes:
        proposed_channel.notes = changes["notes"].strip() if changes["notes"] else None

    # Metadata-only edits must not regenerate outputs or normalize historical dimensions.
    if device_configuration_changed:
        normalize_channel_device_fields(
            proposed_channel,
            requested_device_mode=changes.get("device_mode"),
            requested_screen_count=changes.get("screen_count"),
            requested_canvas_width=changes.get("canvas_width"),
            requested_canvas_height=changes.get("canvas_height"),
        )
    proposed = {field: getattr(proposed_channel, field) for field in copied_fields
                if field != "id" and getattr(proposed_channel, field) != previous[field]}
    definitions = plan_device_outputs(proposed_channel, rebuild=device_configuration_changed, rotation=payload.rotation)
    intents = (MutationIntent("UPDATE", RowRef("Channel", channel.id), tuple(proposed.items())),)
    intents += _output_write_intents(channel.id, definitions, old_outputs)
    with write_guard(db, actor=current_user, client_id=channel.client_id, operation="update_channel", intents=intents):
        for field, value in proposed.items():
            setattr(channel, field, value)
        if definitions is not None:
            sync_channel_device_outputs(channel, definitions=definitions)
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
    with delete_guard(db, (channel,), actor=current_user, operation="delete_channel"):
        preflight_channel_videowall(db, channel)
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

    preflight_channel_campaign_assignments(db, channel)
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

    values = dict(campaign_id=campaign.id, channel_id=channel.id, priority=payload.priority)
    ref = RowRef("CampaignChannelAssignment", target_assignment.id if target_assignment else str(uuid4()))
    intents = (MutationIntent("UPDATE", RowRef("Channel", channel.id)),
               MutationIntent("UPDATE" if target_assignment else "CREATE", ref, tuple(values.items())))
    intents += tuple(MutationIntent("DELETE", RowRef("CampaignChannelAssignment", item.id))
                     for item in assignments_to_remove)
    expected_ids = {item.id for item in existing_assignments}
    with write_guard(db, actor=current_user, client_id=channel.client_id,
                     operation="replace_channel_campaign_assignment", intents=intents):
        actual_ids = set(db.scalars(select(CampaignChannelAssignment.id).where(
            CampaignChannelAssignment.channel_id == channel.id)))
        if actual_ids != expected_ids:
            raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
        for assignment in assignments_to_remove:
            db.delete(assignment)
        if not target_assignment:
            target_assignment = CampaignChannelAssignment(id=ref.identity, **values)
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


def preflight_channel_campaign_assignments(db: Session, channel: Channel) -> None:
    with db.no_autoflush:
        assert_related_clients(channel.client_id, db.scalars(
            select(Campaign.client_id).select_from(CampaignChannelAssignment)
            .outerjoin(Campaign, Campaign.id == CampaignChannelAssignment.campaign_id)
            .where(CampaignChannelAssignment.channel_id == channel.id)))


@router.post("/{channel_id}/heartbeat", response_model=ChannelRead)
async def heartbeat(
    channel_id: str,
    payload: HeartbeatPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Channel:
    require_branch_write_access(current_user)
    channel = get_channel_in_scope(db, channel_id, current_user)
    received_at = datetime.now(UTC)
    values = dict(last_ping_at=received_at, status=payload.status, current_playback=payload.current_playback)
    intent = MutationIntent("UPDATE", RowRef("Channel", channel.id), tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=channel.client_id, operation="heartbeat", intents=(intent,)):
        channel.last_ping_at = received_at
        channel.status = payload.status
        channel.current_playback = payload.current_playback
        db.add(channel)
        db.commit()
        db.refresh(channel)

    await manager.broadcast_to_client(
        channel.client_id,
        manager.heartbeat_payload(channel_id=channel.id, playback=playback_label_for_read(channel, db)),
    )
    return channel


@router.get("/{channel_id}/player-config")
@read_snapshot()
def get_player_config(
    channel_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    channel = get_channel_in_scope(db, channel_id, current_user)

    require_active_channel_tenant(channel, db)
    assignments = list(
        db.scalars(
            readable_query(select(CampaignChannelAssignment), CampaignChannelAssignment, db=db).where(CampaignChannelAssignment.channel_id == channel_id)
        )
    )
    schedules = list(db.scalars(readable_query(select(Schedule), Schedule, db=db).where(Schedule.channel_id == channel_id, Schedule.client_id == channel.client_id, Schedule.is_active.is_(True))))
    campaigns = [safe_tenant_resource(db, Campaign, assignment.campaign_id, channel.client_id) for assignment in assignments]
    videowall = db.scalar(readable_query(select(VideowallNode), VideowallNode, db=db).where(VideowallNode.channel_id == channel_id))

    active_schedule, runtime_campaigns, primary_campaign = resolve_runtime_campaign_context(channel, db)
    active_campaign_payload = runtime_campaigns[0] if runtime_campaigns else None

    videowall_segment = None
    if videowall:
        videowall_wall = safe_tenant_resource(db, Videowall, videowall.videowall_id, channel.client_id)
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
        "channel": safe_model_read(channel, ChannelRead, db).model_dump(),
        "campaigns": [
            {
                "assignment_id": assignment.id,
                "priority": assignment.priority,
                "campaign_id": assignment.campaign_id,
                "name": campaign.name if campaign else "Unknown",
                "active_from": assignment.active_from,
                "active_until": assignment.active_until,
            }
            for assignment, campaign in zip(assignments, campaigns) if campaign
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
    return build_player_runtime_from_snapshot(channel, db, build_public_base_url(request))
