import hashlib
import logging
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
from app.core.player_diagnostics import (
    admission_acquired, admission_released, admission_wait_start, observed,
)
from app.core.player_admission import player_activity, playback_worker_activity, heartbeat_worker_activity
from app.core.playback_failure_diagnostics import handler_entered as playback_handler_entered
from app.core.request_pg_diagnostics import diagnostic_stage, runtime_handler_entered
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


# The expensive budget remains four. Playback acceptance uses its own small
# durable-ingress lane; its later materializer shares the expensive budget.
_PLAYER_ACTIVITY_SLOTS = 4


async def _admit_player_activity(request: Request):
    ticket = admission_wait_start()
    try:
        async with player_activity(request):
            admission_acquired(ticket)
            yield
    finally:
        admission_released(ticket)


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
        resource = safe_tenant_resource(db, model, getattr(payload, field), client_id,
                                        scope_media_to_resource=True)
        updates[field] = resource.id if resource else None
    return payload.model_copy(update=updates)


def _sanitize_heartbeat_telemetry(payload, db: Session, client_id: str):
    if db.get_bind().dialect.name != "postgresql":
        return _sanitize_telemetry(payload, db, client_id, "current_")
    from app.services.db_snapshots import readonly_snapshot_session
    from app.services.runtime_read import runtime_read_scope

    # Share only these optional read projections in one consistent snapshot.
    # Close it before the write guard obtains fresh, locked ownership proofs;
    # no read eligibility or media authority can escape onto the writer Session.
    from app.services.heartbeat_queries import heartbeat_query_scope, sanitize_heartbeat_references
    with readonly_snapshot_session(db) as snapshot:
        with runtime_read_scope(snapshot), heartbeat_query_scope(snapshot):
            return sanitize_heartbeat_references(payload, snapshot, client_id)


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


@router.get("/runtime-config", dependencies=[Depends(_admit_player_activity)])
@observed("HANDLER")
def get_runtime_config(
    request: Request,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    runtime_handler_entered(request)
    with diagnostic_stage("runtime_db", db=db):
        channel = db.get(Channel, player_device.channel_id)
        if not channel:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
        if channel.client_id != player_device.client_id or channel.branch_id != player_device.branch_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

        intent = MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), (("last_seen_at", datetime.now(UTC)),))
        with diagnostic_stage("runtime_tenant_guard", db=db):
            with player_write_guard(db, channel=channel, player_device=player_device,
                                    operation="get_runtime_config", intents=(intent,)) as plan:
                player_device.last_seen_at = next(p.values["last_seen_at"] for p in plan.proposed
                                                  if p.reference == intent.reference)
                db.add(player_device)
                db.commit()
    with diagnostic_stage("runtime_builder", db=db):
        return build_player_runtime_from_snapshot(channel, db, build_public_base_url(request))


@router.post("/heartbeat", response_model=PlayerHeartbeatAck, dependencies=[Depends(_admit_player_activity)])
@observed("HANDLER")
def create_player_heartbeat(
    payload: PlayerHeartbeatCreate,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
    request: Request = None,
) -> PlayerHeartbeatAck:
    from anyio import from_thread
    import time
    from app.core.player_admission import HEARTBEAT_REQUEST_BUDGET_NS
    from app.services.heartbeat_queries import take_heartbeat_channel
    from app.services.heartbeat_inbox import enqueue, ensure_worker

    channel = take_heartbeat_channel(db, player_device)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    deadline_ns = (request.scope.get("_player_admission_started_ns", time.monotonic_ns())
                   + HEARTBEAT_REQUEST_BUDGET_NS) if request is not None else None
    acknowledgment = enqueue(db, player_device, channel, payload, deadline_ns=deadline_ns)
    if request is not None:
        application, engine = request.app, db.get_bind()
        try:
            from_thread.run_sync(lambda: ensure_worker(
                application, engine, _materialize_player_heartbeat,
                activity=lambda: heartbeat_worker_activity(application)))
        except RuntimeError:
            # COMMIT, not this best-effort wake, is the acceptance boundary.
            logging.getLogger(__name__).warning("HEARTBEAT_INBOX_WAKE_DEFERRED")
    return acknowledgment


def _newer_materialized_heartbeat_label(db, device_id, accepted_at, event_id):
    """Locked Channel serializes this fence with both GREEN and legacy writers."""
    from sqlalchemy import and_, case, literal, or_
    legacy_label = case(
        (ContentItem.id.is_not(None), ContentItem.name),
        (Campaign.id.is_not(None), Campaign.name),
        (Layout.id.is_not(None), literal("Layout ") + Layout.name),
        else_=None,
    )
    # New reports record the label actually derived then; legacy BLUE rows have
    # no envelope and retain their original reference/name interpretation.
    recorded_label = PlayerHeartbeat.report_json["derived"]["playback_label"].as_string()
    is_new_report = PlayerHeartbeat.report_json["payload"]["status"].as_string().is_not(None)
    label = case((is_new_report, recorded_label), else_=legacy_label)
    return db.scalar(select(PlayerHeartbeat.id)
        .outerjoin(ContentItem, ContentItem.id == PlayerHeartbeat.current_content_id)
        .outerjoin(Campaign, Campaign.id == PlayerHeartbeat.current_campaign_id)
        .outerjoin(Layout, Layout.id == PlayerHeartbeat.current_layout_id)
        .where(PlayerHeartbeat.player_device_id == device_id,
               or_(PlayerHeartbeat.received_at > accepted_at,
                   and_(PlayerHeartbeat.received_at == accepted_at, PlayerHeartbeat.id > event_id)),
               label.is_not(None), label != "")
        .limit(1)) is not None


def _materialize_player_heartbeat(payload, player_device, db, *, event_id, received_at,
                                  raw_report, before_commit):
    """Derived history/media only; never replay accepted liveness or version."""
    from app.services.heartbeat_queries import retain_heartbeat_write_keys
    channel = db.get(Channel, player_device.channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    # Keep the consumer's transaction/advisory claim intact. The full write
    # guard still repeats fresh media/ownership proof before canonical writes.
    payload = _sanitize_telemetry(payload, db, channel.client_id, "current_")
    playback_label = _resolve_playback_label(db, content_id=payload.current_content_id,
        campaign_id=payload.current_campaign_id, layout_id=payload.current_layout_id)
    channel_values = {"current_playback": playback_label} if playback_label else {}
    values = dict(player_device_id=player_device.id, channel_id=channel.id,
        client_id=channel.client_id, branch_id=channel.branch_id,
        status=payload.status.lower(), current_content_id=payload.current_content_id,
        current_campaign_id=payload.current_campaign_id, current_layout_id=payload.current_layout_id,
        resolution_json=payload.resolution, mode=payload.mode, app_version=payload.app_version,
        cache_status_json=payload.cache_status, errors_json=payload.errors,
        local_time=payload.local_time, playback_position=payload.playback_position,
        received_at=received_at,
        report_json={"payload": raw_report, "derived": {"playback_label": playback_label}})
    ref = RowRef("PlayerHeartbeat", event_id)
    intents = (
        MutationIntent("UPDATE", RowRef("Channel", channel.id), tuple(channel_values.items())),
        MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), ()),
        MutationIntent("CREATE", ref, tuple(values.items())),
    )
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="create_player_heartbeat", intents=intents) as plan:
        if db.get_bind().dialect.name == "postgresql":
            retain_heartbeat_write_keys(db, plan, channel, player_device)
        before_commit(plan)
        if playback_label and not _newer_materialized_heartbeat_label(
                db, values["player_device_id"], received_at, event_id):
            channel.current_playback = playback_label
        heartbeat = PlayerHeartbeat(id=event_id, **values)
        heartbeat.created_at = received_at
        db.add(heartbeat)
        db.commit()
    return PlayerHeartbeatAck(ok=True, received_at=received_at, channel_id=values["channel_id"])


@router.post("/playback-events", response_model=PlayerPlaybackEventAck, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_admit_player_activity)])
@observed("HANDLER")
def create_player_playback_event(
    payload: PlayerPlaybackEventCreate,
    request: Request,
    player_device: PlayerDevice = Depends(get_current_player_device),
    db: Session = Depends(get_db),
) -> PlayerPlaybackEventAck:
    playback_handler_entered(request)
    from anyio import from_thread
    from app.services.playback_inbox import enqueue, ensure_worker

    channel = db.get(Channel, player_device.channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    acknowledgment = enqueue(db, player_device, channel, payload)
    engine = db.get_bind()
    application = request.app
    # Do not propagate this HTTP diagnostic ContextVar into the consumer. Its
    # Session and transaction are independently owned inside the service.
    try:
        from_thread.run_sync(lambda: ensure_worker(
            application, engine, _record_player_playback_event,
            activity=lambda: playback_worker_activity(application)))
    except RuntimeError:
        # The event is already durable. A consumer wake is not its acceptance
        # boundary; startup/health recovery can resume it without a new POST.
        logging.getLogger(__name__).warning("PLAYBACK_INBOX_WAKE_DEFERRED")
    return acknowledgment


def _record_player_playback_event(
    payload: PlayerPlaybackEventCreate,
    player_device: PlayerDevice,
    db: Session,
    *,
    event_id: str | None = None,
    recorded_at: datetime | None = None,
    before_commit=None,
) -> PlayerPlaybackEventAck:
    channel = db.get(Channel, player_device.channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    receipt_channel_id = channel.id

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
    ref = RowRef("PlayerPlaybackEvent", event_id or str(uuid4()))
    intents = (MutationIntent("UPDATE", RowRef("PlayerDevice", player_device.id), ()),
               MutationIntent("CREATE", ref, tuple(values.items())))
    with player_write_guard(db, channel=channel, player_device=player_device,
                            operation="create_player_playback_event", intents=intents,
                            receipt_processing=before_commit is not None) as plan:
        if before_commit is not None:
            before_commit(plan)
        event = PlayerPlaybackEvent(id=ref.identity, **values)
        if recorded_at is not None:
            event.created_at = recorded_at
        db.add(event)
        db.commit()
        if recorded_at is None:
            db.refresh(event)

    return PlayerPlaybackEventAck(event_id=ref.identity,
        recorded_at=recorded_at if recorded_at is not None else event.created_at,
        channel_id=receipt_channel_id)
