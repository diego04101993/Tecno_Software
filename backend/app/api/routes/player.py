import hashlib
import secrets
from uuid import uuid4

from app.services.tenant_mutations import player_write_guard, MutationIntent
from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import conflict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_player_device, get_db
from app.core.public_urls import build_public_base_url
from app.models.entities import (
    Campaign,
    Channel,
    ChannelConnectionStatus,
    ContentItem,
    Layout,
    PlayerDevice,
    PlayerHeartbeat,
    PlayerPlaybackEvent,
)
from app.schemas.domain import (
    PlayerActivationRequest,
    PlayerActivationResponse,
    PlayerHeartbeatAck,
    PlayerHeartbeatCreate,
    PlayerPlaybackEventAck,
    PlayerPlaybackEventCreate,
    PlayerTimeResponse,
)
from app.services.tenancy import require_active_channel_tenant, safe_tenant_resource
from app.services.player_runtime import build_player_runtime_from_snapshot, build_server_time_payload


router = APIRouter()


def _generate_player_token() -> str:
    return f"tpw_{secrets.token_urlsafe(32)}"


def _hash_player_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_reusable_player_token(player_device: PlayerDevice | None) -> str | None:
    if not player_device:
        return None

    metadata = player_device.metadata_json or {}
    issued_token = metadata.get("issued_player_token")
    if not isinstance(issued_token, str) or not issued_token.startswith("tpw_"):
        return None

    if _hash_player_token(issued_token) != player_device.player_token_hash:
        return None

    return issued_token


def _resolve_channel_by_code(db: Session, channel_code: str) -> Channel:
    channel = db.scalar(select(Channel).where(Channel.channel_code == channel_code.strip().upper()))
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel code not found")
    return channel


def _sanitize_telemetry(payload, db: Session, client_id: str, prefix: str = ""):
    updates = {}
    for name, model in (("content_id", ContentItem), ("campaign_id", Campaign), ("layout_id", Layout)):
        field = prefix + name
        resource = safe_tenant_resource(db, model, getattr(payload, field), client_id)
        updates[field] = resource.id if resource else None
    return payload.model_copy(update=updates)


def _resolve_playback_label(
    db: Session,
    *,
    content_id: str | None,
    campaign_id: str | None,
    layout_id: str | None,
) -> str | None:
    if content_id:
        content = db.get(ContentItem, content_id)
        if content:
            return content.name
    if campaign_id:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            return campaign.name
    if layout_id:
        layout = db.get(Layout, layout_id)
        if layout:
            return f"Layout {layout.name}"
    return None


@router.get("/time", response_model=PlayerTimeResponse)
def get_player_time() -> dict[str, object]:
    return build_server_time_payload()


@router.post("/activate", response_model=PlayerActivationResponse)
def activate_player(
    payload: PlayerActivationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> PlayerActivationResponse:
    return _activate_player(payload, db)


def _activate_player(payload: PlayerActivationRequest, db: Session) -> PlayerActivationResponse:
    channel = _resolve_channel_by_code(db, payload.channel_code)
    require_active_channel_tenant(channel, db)
    current_time = datetime.now(UTC)
    activation_status = "activation_new"

    player_device = db.scalar(select(PlayerDevice).where(PlayerDevice.channel_id == channel.id))
    if player_device is not None and not player_device.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Player is disabled")
    if player_device is not None and (player_device.client_id != channel.client_id
                                      or player_device.branch_id != channel.branch_id):
        raise HTTPException(status_code=403, detail="Invalid player scope")
    values = dict(hardware_id=payload.hardware_id, device_name=payload.device_name,
                  app_version=payload.app_version, last_seen_at=current_time, is_active=True)
    if not player_device:
        token = _generate_player_token()
        values.update(channel_id=channel.id, client_id=channel.client_id, branch_id=channel.branch_id,
                      player_token_hash=_hash_player_token(token),
                      metadata_json={"activation_count": 1, "issued_player_token": token,
                                     "issued_at": current_time.isoformat()})
    else:
        metadata = dict(player_device.metadata_json or {})
        reusable_token = _get_reusable_player_token(player_device) if player_device.hardware_id == payload.hardware_id else None
        if reusable_token:
            token = reusable_token
            activation_status = "activation_existing"
        else:
            token = _generate_player_token()
            values["player_token_hash"] = _hash_player_token(token)
            metadata["issued_player_token"] = token
            metadata["issued_at"] = current_time.isoformat()
        metadata["activation_count"] = int(metadata.get("activation_count", 0)) + 1
        values["metadata_json"] = metadata

    ref = RowRef("PlayerDevice", player_device.id if player_device else str(uuid4()))
    intents = (
        MutationIntent("UPDATE", RowRef("Channel", channel.id), (("hardware_identifier", payload.hardware_id),)),
        MutationIntent("UPDATE" if player_device else "CREATE", ref, tuple(values.items())),
    )
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="activate_player", intents=intents):
        current_id = db.scalar(select(PlayerDevice.id).where(PlayerDevice.channel_id == channel.id))
        if current_id != (player_device.id if player_device else None):
            raise conflict()
        if player_device is None:
            player_device = PlayerDevice(id=ref.identity, **values)
        else:
            for key, value in values.items():
                setattr(player_device, key, value)
        channel.hardware_identifier = payload.hardware_id
        db.add(player_device)
        db.add(channel)
        db.commit()
        db.refresh(player_device)

    return PlayerActivationResponse(
        player_token=token,
        channel_id=channel.id,
        client_id=channel.client_id,
        branch_id=channel.branch_id,
        activation_status=activation_status,
    )


@router.get("/runtime-config")
def get_runtime_config(
    request: Request,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    channel = db.get(Channel, player_device.channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    if channel.client_id != player_device.client_id or channel.branch_id != player_device.branch_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    intent = MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), (("last_seen_at", datetime.now(UTC)),))
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="get_runtime_config", intents=(intent,)) as plan:
        player_device.last_seen_at = next(p.values["last_seen_at"] for p in plan.proposed
                                          if p.reference == intent.reference)
        db.add(player_device)
        db.commit()
    return build_player_runtime_from_snapshot(channel, db, build_public_base_url(request))


@router.post("/heartbeat", response_model=PlayerHeartbeatAck)
def create_player_heartbeat(
    payload: PlayerHeartbeatCreate,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
) -> PlayerHeartbeatAck:
    channel = db.get(Channel, player_device.channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    current_time = datetime.now(UTC)
    payload = _sanitize_telemetry(payload, db, channel.client_id, "current_")
    normalized_status = payload.status.lower()
    try:
        channel_status = ChannelConnectionStatus(normalized_status)
    except ValueError:
        channel_status = ChannelConnectionStatus.UNKNOWN
    channel_values = dict(status=channel_status, last_ping_at=current_time)
    playback_label = _resolve_playback_label(
        db,
        content_id=payload.current_content_id,
        campaign_id=payload.current_campaign_id,
        layout_id=payload.current_layout_id,
    )
    if playback_label:
        channel_values["current_playback"] = playback_label

    device_values = dict(last_seen_at=current_time)
    if payload.app_version:
        device_values["app_version"] = payload.app_version

    values = dict(
        player_device_id=player_device.id,
        channel_id=channel.id,
        client_id=channel.client_id,
        branch_id=channel.branch_id,
        status=normalized_status,
        current_content_id=payload.current_content_id,
        current_campaign_id=payload.current_campaign_id,
        current_layout_id=payload.current_layout_id,
        resolution_json=payload.resolution,
        mode=payload.mode,
        app_version=payload.app_version,
        cache_status_json=payload.cache_status,
        errors_json=payload.errors,
        local_time=payload.local_time,
        playback_position=payload.playback_position,
        received_at=current_time,
    )
    ref = RowRef("PlayerHeartbeat", str(uuid4()))
    intents = (
        MutationIntent("UPDATE", RowRef("Channel", channel.id), tuple(channel_values.items())),
        MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), tuple(device_values.items())),
        MutationIntent("CREATE", ref, tuple(values.items())),
    )
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="create_player_heartbeat", intents=intents) as plan:
        # Same-device telemetry is serialized at its authoritative rows. Use
        # the approved timestamps so an older request admitted later cannot
        # move last_seen_at/last_ping_at backwards.
        channel_values["last_ping_at"] = next(p.values["last_ping_at"] for p in plan.proposed
                                               if p.reference == intents[0].reference)
        device_values["last_seen_at"] = next(p.values["last_seen_at"] for p in plan.proposed
                                             if p.reference == intents[1].reference)
        for key, value in channel_values.items():
            setattr(channel, key, value)
        for key, value in device_values.items():
            setattr(player_device, key, value)
        heartbeat = PlayerHeartbeat(id=ref.identity, **values)
        db.add_all([channel, player_device, heartbeat])
        db.commit()
        db.refresh(heartbeat)

    return PlayerHeartbeatAck(ok=True, received_at=heartbeat.received_at, channel_id=channel.id)


@router.post("/playback-events", response_model=PlayerPlaybackEventAck, status_code=status.HTTP_201_CREATED)
def create_player_playback_event(
    payload: PlayerPlaybackEventCreate,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
) -> PlayerPlaybackEventAck:
    channel = db.get(Channel, player_device.channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    payload = _sanitize_telemetry(payload, db, channel.client_id)
    values = dict(
        player_device_id=player_device.id,
        channel_id=channel.id,
        client_id=channel.client_id,
        branch_id=channel.branch_id,
        content_id=payload.content_id,
        campaign_id=payload.campaign_id,
        layout_id=payload.layout_id,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        duration_seconds=payload.duration_seconds,
        status=payload.status,
        metadata_json=payload.metadata_json,
    )
    ref = RowRef("PlayerPlaybackEvent", str(uuid4()))
    intents = (MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), ()),
               MutationIntent("CREATE", ref, tuple(values.items())))
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="create_player_playback_event", intents=intents):
        event = PlayerPlaybackEvent(id=ref.identity, **values)
        db.add(event)
        db.commit()
        db.refresh(event)

    return PlayerPlaybackEventAck(event_id=event.id, recorded_at=event.created_at, channel_id=channel.id)
