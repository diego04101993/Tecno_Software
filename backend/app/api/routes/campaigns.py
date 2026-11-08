from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.public_urls import build_public_base_url
from app.models.entities import (
    Campaign,
    CampaignChannelAssignment,
    CampaignPlaybackMode,
    CampaignPlaylistItem,
    ContentItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    Channel,
    Layout,
    PlayerHeartbeat,
    PlayerPlaybackEvent,
    Schedule,
    User,
)
from app.schemas.domain import (
    CampaignAssignmentCreate,
    CampaignAssignmentResult,
    CampaignCreate,
    CampaignPlaybackModeUpdate,
    CampaignPlaylistItemCreate,
    CampaignPlaylistItemRead,
    CampaignPlaylistItemUpdate,
    CampaignPlaylistReorderRequest,
    CampaignRead,
    CampaignSequenceItemCreate,
    CampaignSequenceItemRead,
    CampaignSequenceItemUpdate,
    CampaignSequenceReorderRequest,
)
from app.services.campaign_sequence import (
    edit_campaign_items,
    get_legacy_playlist_item_or_404,
    get_ordered_legacy_playlist_items,
    get_ordered_sequence_items,
    get_sequence_item_or_404,
    normalize_playback_mode,
    resequence_legacy_playlist_items,
    resequence_sequence_items,
    resolve_effective_sequence_items,
    sync_legacy_playlist_from_sequence,
    sync_sequence_from_legacy,
    validate_sequence_item_target,
)
from app.services.content_cleanup import content_supports_manual_duration, resolve_sequence_item_duration
from app.services.player_runtime import build_campaign_sequence_preview_payload
from app.services.tenancy import (
    optional_relation_id,
    assert_related_clients,
    assert_related_clients_query,
    related_client_matches,
    require_same_client_resource,
    apply_client_filter,
    assert_client_exists,
    branch_campaign_ids_query,
    can_write_client_scope,
    get_channel_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


from app.services.tenant_mutations import delete_guard, write_guard, MutationIntent
from app.services.tenant_ownership import RowRef


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()


def get_campaign_in_scope(campaign_id: str, db: Session, current_user: User) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    require_client_scope(current_user, campaign.client_id)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        visible_campaign = db.scalar(
            select(Campaign.id).where(
                Campaign.id == campaign_id,
                Campaign.id.in_(branch_campaign_ids_query(branch_id)),
            )
        )
        if not visible_campaign:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    return campaign


def get_sequence_target_content(content_id: str | None, db: Session) -> ContentItem | None:
    if not content_id:
        return None
    return db.get(ContentItem, content_id)


def safe_campaign_read(campaign: Campaign, db: Session) -> CampaignRead:
    layout_id = optional_relation_id(db, Layout, campaign.layout_id, campaign.client_id)
    return CampaignRead.model_validate(campaign).model_copy(deep=True, update={"layout_id": layout_id})


@router.get("/", response_model=list[CampaignRead])
@read_snapshot(response_model=list[CampaignRead])
def list_campaigns(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Campaign]:
    query = apply_client_filter(readable_query(select(Campaign), Campaign, db=db).order_by(Campaign.created_at.desc()), Campaign, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Campaign.client_id == target_client_id)
    if is_branch_scoped(current_user):
        query = query.where(Campaign.id.in_(branch_campaign_ids_query(require_branch_assignment(current_user))))
    return [safe_campaign_read(campaign, db) for campaign in db.scalars(query)]


@router.post("/", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Campaign:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create campaigns")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    require_same_client_resource(db, Layout, payload.layout_id, client_id)
    values = dict(
        client_id=client_id,
        layout_id=payload.layout_id,
        name=payload.name,
        description=payload.description,
        default_duration_seconds=payload.default_duration_seconds,
        is_active=payload.is_active,
        loop_enabled=payload.loop_enabled,
        playback_mode=normalize_playback_mode(payload.playback_mode),
    )
    ref = RowRef("Campaign", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_campaign", intents=(intent,)):
        campaign = Campaign(id=ref.identity, **values)
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete campaigns")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    with delete_guard(db, (campaign,), actor=current_user, operation="delete_campaign"):
        preflight_campaign_delete(db, campaign)
        db.delete(campaign)
        db.commit()


def preflight_campaign_delete(db: Session, campaign: Campaign) -> None:
    # Playlist/sequence rows have this campaign as their sole owner. Schedules,
    # channel assignments and SET NULL telemetry have independent owners.
    with db.no_autoflush:
        assert_related_clients(campaign.client_id, db.scalars(
            select(Schedule.client_id).where(Schedule.campaign_id == campaign.id)))
        assert_related_clients(campaign.client_id, db.scalars(
            select(Channel.client_id).select_from(CampaignChannelAssignment)
            .outerjoin(Channel, Channel.id == CampaignChannelAssignment.channel_id)
            .where(CampaignChannelAssignment.campaign_id == campaign.id)))
        for model, campaign_field in ((PlayerHeartbeat, PlayerHeartbeat.current_campaign_id),
                                      (PlayerPlaybackEvent, PlayerPlaybackEvent.campaign_id)):
            assert_related_clients_query(campaign.client_id, db,
                select(model.client_id).where(campaign_field == campaign.id))


@router.patch("/{campaign_id}/playback-mode", response_model=CampaignRead)
def update_campaign_playback_mode(
    campaign_id: str,
    payload: CampaignPlaybackModeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Campaign:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaigns")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    mode = normalize_playback_mode(payload.playback_mode)
    intent = MutationIntent("UPDATE", RowRef("Campaign", campaign.id), (("playback_mode", mode),))
    with write_guard(db, actor=current_user, client_id=campaign.client_id,
                     operation="update_campaign_playback_mode", intents=(intent,)):
        campaign.playback_mode = mode
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return safe_campaign_read(campaign, db)


@router.get("/{campaign_id}/playlist", response_model=list[CampaignPlaylistItemRead])
@read_snapshot(response_model=list[CampaignPlaylistItemRead])
def get_playlist(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list:
    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return list(db.scalars(readable_query(select(CampaignPlaylistItem), CampaignPlaylistItem, db=db).where(
        CampaignPlaylistItem.campaign_id == campaign.id,
        related_client_matches(ContentItem, CampaignPlaylistItem.content_id, campaign.client_id)
    ).order_by(CampaignPlaylistItem.sort_order.asc(), CampaignPlaylistItem.created_at.asc())))


@router.post("/{campaign_id}/playlist", response_model=CampaignPlaylistItemRead, status_code=status.HTTP_201_CREATED)
def add_playlist_item(
    campaign_id: str,
    payload: CampaignPlaylistItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign playlists")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="add_playlist_item",
                               side="playlist", action="add", payload=payload)


@router.patch("/{campaign_id}/playlist/{playlist_item_id}", response_model=CampaignPlaylistItemRead)
def update_playlist_item(
    campaign_id: str,
    playlist_item_id: str,
    payload: CampaignPlaylistItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign playlists")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="update_playlist_item",
                               side="playlist", action="update", payload=payload, item_id=playlist_item_id)


@router.post("/{campaign_id}/playlist/reorder", response_model=list[CampaignPlaylistItemRead])
def reorder_playlist(
    campaign_id: str,
    payload: CampaignPlaylistReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign playlists")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="reorder_playlist",
                               side="playlist", action="reorder", payload=payload)


@router.post("/{campaign_id}/playlist/{playlist_item_id}/duplicate", response_model=CampaignPlaylistItemRead, status_code=status.HTTP_201_CREATED)
def duplicate_playlist_item(
    campaign_id: str,
    playlist_item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign playlists")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="duplicate_playlist_item",
                               side="playlist", action="duplicate", item_id=playlist_item_id)


@router.delete("/{campaign_id}/playlist/{playlist_item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist_item(
    campaign_id: str,
    playlist_item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign playlists")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    playlist_item = get_legacy_playlist_item_or_404(campaign.id, playlist_item_id, db)
    with delete_guard(db, (campaign,), actor=current_user, operation="delete_playlist_item"):
        db.delete(playlist_item)
        db.flush()
        items = get_ordered_legacy_playlist_items(campaign.id, db)
        resequence_legacy_playlist_items(items, db)
        sync_sequence_from_legacy(campaign.id, db)
        db.commit()


def _read_sequence_items(campaign_id: str, db: Session) -> list[CampaignSequenceItem]:
    sequence_items = get_ordered_sequence_items(campaign_id, db)
    if sequence_items:
        return sequence_items
    # Read-only representation: legacy IDs/timestamps remain stable across GETs.
    return [CampaignSequenceItem(
        id=item.id, campaign_id=item.campaign_id, content_id=item.content_id,
        item_type=CampaignSequenceItemType.CONTENT, sort_order=item.sort_order,
        duration_seconds=item.duration_seconds, options_json={"zone_key": item.zone_key},
        is_enabled=True, created_at=item.created_at, updated_at=item.updated_at,
    ) for item in get_ordered_legacy_playlist_items(campaign_id, db)]


@router.get("/{campaign_id}/sequence", response_model=list[CampaignSequenceItemRead])
@read_snapshot(response_model=list[CampaignSequenceItemRead])
def get_sequence(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CampaignSequenceItem]:
    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    # Filter only the read representation; write paths must still see corruption.
    items = _read_sequence_items(campaign_id, db)
    return [item for item in items if campaign_item_read_allowed(item, campaign, db)]


def campaign_item_read_allowed(item: CampaignSequenceItem, campaign: Campaign, db: Session) -> bool:
    for model, target_id in ((ContentItem, item.content_id), (Layout, item.layout_id)):
        if target_id is not None:
            target = db.get(model, target_id)
            if target is None or target.client_id != campaign.client_id:
                return False
    return bool(item.content_id if item.item_type == CampaignSequenceItemType.CONTENT else item.layout_id)


@router.get("/{campaign_id}/sequence-preview")
@read_snapshot()
def get_sequence_preview(
    campaign_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return build_campaign_sequence_preview_payload(campaign, db, build_public_base_url(request))


@router.post("/{campaign_id}/sequence", response_model=CampaignSequenceItemRead, status_code=status.HTTP_201_CREATED)
def add_sequence_item(
    campaign_id: str,
    payload: CampaignSequenceItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CampaignSequenceItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign sequences")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="add_sequence_item",
                               side="sequence", action="add", payload=payload)


@router.patch("/{campaign_id}/sequence/{sequence_item_id}", response_model=CampaignSequenceItemRead)
def update_sequence_item(
    campaign_id: str,
    sequence_item_id: str,
    payload: CampaignSequenceItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CampaignSequenceItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign sequences")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="update_sequence_item",
                               side="sequence", action="update", payload=payload, item_id=sequence_item_id)


@router.post("/{campaign_id}/sequence/reorder", response_model=list[CampaignSequenceItemRead])
def reorder_sequence(
    campaign_id: str,
    payload: CampaignSequenceReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CampaignSequenceItem]:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign sequences")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="reorder_sequence",
                               side="sequence", action="reorder", payload=payload)


@router.post("/{campaign_id}/sequence/{sequence_item_id}/duplicate", response_model=CampaignSequenceItemRead, status_code=status.HTTP_201_CREATED)
def duplicate_sequence_item(
    campaign_id: str,
    sequence_item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CampaignSequenceItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign sequences")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    return edit_campaign_items(campaign, db, current_user, operation="duplicate_sequence_item",
                               side="sequence", action="duplicate", item_id=sequence_item_id)


@router.delete("/{campaign_id}/sequence/{sequence_item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sequence_item(
    campaign_id: str,
    sequence_item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify campaign sequences")

    campaign = get_campaign_in_scope(campaign_id, db, current_user)
    with delete_guard(db, (campaign,), actor=current_user, operation="delete_sequence_item"):
        if get_ordered_sequence_items(campaign.id, db):
            sequence_item = get_sequence_item_or_404(campaign.id, sequence_item_id, db)
            db.delete(sequence_item)
            db.flush()
            items = get_ordered_sequence_items(campaign.id, db)
            resequence_sequence_items(items, db)
        else:
            # Legacy rows were locked/proved by the campaign plan. Materialize
            # only survivors; never INSERT then DELETE a target outside that
            # immutable plan. Keep their public sequence IDs and final order.
            legacy = _read_sequence_items(campaign.id, db)
            if not any(item.id == sequence_item_id for item in legacy):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sequence item not found")
            survivors = [item for item in legacy if item.id != sequence_item_id]
            for index, item in enumerate(survivors, start=1):
                require_same_client_resource(db, ContentItem, item.content_id, campaign.client_id)
                item.sort_order = index
            db.add_all(survivors)
            db.flush()
        sync_legacy_playlist_from_sequence(campaign.id, db)
        db.commit()


@router.post("/{campaign_id}/assignments", response_model=CampaignAssignmentResult)
def assign_campaign_to_channel(
    campaign_id: str,
    payload: CampaignAssignmentCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CampaignAssignmentResult:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to assign campaigns")

    campaign = db.get(Campaign, campaign_id)
    channel = get_channel_in_scope(db, payload.channel_id, current_user)
    if not campaign or channel.client_id != campaign.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign or channel not found")
    require_client_scope(current_user, campaign.client_id)

    existing_assignment = db.scalar(
        select(CampaignChannelAssignment).where(
            CampaignChannelAssignment.campaign_id == campaign_id,
            CampaignChannelAssignment.channel_id == payload.channel_id,
        )
    )
    values = dict(campaign_id=campaign_id, channel_id=payload.channel_id, priority=payload.priority)
    ref = RowRef("CampaignChannelAssignment", existing_assignment.id if existing_assignment else str(uuid4()))
    intents = (
        MutationIntent("UPDATE", RowRef("Campaign", campaign.id), ()),
        MutationIntent("UPDATE", RowRef("Channel", channel.id), ()),
        MutationIntent("UPDATE" if existing_assignment else "CREATE", ref,
                       () if existing_assignment else tuple(values.items())),
    )
    with write_guard(db, actor=current_user, client_id=campaign.client_id,
                     operation="assign_campaign_to_channel", intents=intents):
        current = db.scalar(
            select(CampaignChannelAssignment).where(
                CampaignChannelAssignment.campaign_id == campaign_id,
                CampaignChannelAssignment.channel_id == payload.channel_id,
            )
        )
        # A concurrent insert changes the prepared batch; retry must prepare and
        # prove that row instead of returning an unvalidated post-rollback row.
        if (current.id if current else None) != (existing_assignment.id if existing_assignment else None):
            raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
        if current:
            return CampaignAssignmentResult(
                assignment_id=current.id,
                campaign_id=campaign_id,
                channel_id=payload.channel_id,
                assignment_status="existing",
                detail="La campaña ya estaba publicada en este canal",
            )
        assignment = CampaignChannelAssignment(id=ref.identity, **values)
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
    response.status_code = status.HTTP_201_CREATED
    return CampaignAssignmentResult(
        assignment_id=assignment.id,
        campaign_id=campaign_id,
        channel_id=payload.channel_id,
        assignment_status="created",
        detail="Campaña publicada",
    )

