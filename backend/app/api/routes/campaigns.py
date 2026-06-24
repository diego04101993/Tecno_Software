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
    ContentItem,
    CampaignSequenceItem,
    CampaignSequenceItemType,
    Channel,
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
    apply_client_filter,
    assert_client_exists,
    branch_campaign_ids_query,
    can_write_client_scope,
    get_channel_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


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


@router.get("/", response_model=list[CampaignRead])
def list_campaigns(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Campaign]:
    query = apply_client_filter(select(Campaign).order_by(Campaign.created_at.desc()), Campaign, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Campaign.client_id == target_client_id)
    if is_branch_scoped(current_user):
        query = query.where(Campaign.id.in_(branch_campaign_ids_query(require_branch_assignment(current_user))))
    return list(db.scalars(query))


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
    campaign = Campaign(
        client_id=client_id,
        layout_id=payload.layout_id,
        name=payload.name,
        description=payload.description,
        default_duration_seconds=payload.default_duration_seconds,
        is_active=payload.is_active,
        loop_enabled=payload.loop_enabled,
        playback_mode=normalize_playback_mode(payload.playback_mode),
    )
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
    db.delete(campaign)
    db.commit()


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
    campaign.playback_mode = normalize_playback_mode(payload.playback_mode)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}/playlist", response_model=list[CampaignPlaylistItemRead])
def get_playlist(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list:
    get_campaign_in_scope(campaign_id, db, current_user)
    return get_ordered_legacy_playlist_items(campaign_id, db)


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
    validate_sequence_item_target(
        campaign,
        item_type=CampaignSequenceItemType.CONTENT,
        content_id=payload.content_id,
        layout_id=None,
        db=db,
    )
    content = get_sequence_target_content(payload.content_id, db)

    playlist_item = CampaignSequenceItem(
        campaign_id=campaign.id,
        item_type=CampaignSequenceItemType.CONTENT,
        content_id=payload.content_id,
        layout_id=None,
        sort_order=max(1, payload.sort_order),
        duration_seconds=resolve_sequence_item_duration(
            item_type=CampaignSequenceItemType.CONTENT,
            content=content,
            campaign_default_duration_seconds=campaign.default_duration_seconds,
            requested_duration_seconds=payload.duration_seconds,
        ),
        options_json={"zone_key": payload.zone_key},
        is_enabled=True,
    )
    db.add(playlist_item)
    db.flush()
    sync_legacy_playlist_from_sequence(campaign.id, db)
    legacy_items = get_ordered_legacy_playlist_items(campaign.id, db)
    created_item = legacy_items[min(len(legacy_items), max(1, payload.sort_order)) - 1] if legacy_items else None
    db.commit()
    if not created_item:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No se pudo crear el item de playlist")
    db.refresh(created_item)
    return created_item


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
    playlist_item = get_legacy_playlist_item_or_404(campaign.id, playlist_item_id, db)
    playlist_content = db.get(ContentItem, playlist_item.content_id)

    if payload.duration_seconds is not None:
        playlist_item.duration_seconds = resolve_sequence_item_duration(
            item_type=CampaignSequenceItemType.CONTENT,
            content=playlist_content,
            campaign_default_duration_seconds=campaign.default_duration_seconds,
            requested_duration_seconds=(
                payload.duration_seconds
                if content_supports_manual_duration(playlist_content)
                else playlist_content.duration_seconds if playlist_content else payload.duration_seconds
            ),
        )
    if payload.zone_key is not None:
        playlist_item.zone_key = payload.zone_key.strip() or "main"

    if payload.sort_order is not None:
        items = get_ordered_legacy_playlist_items(campaign.id, db)
        items = [item for item in items if item.id != playlist_item.id]
        target_index = max(0, min(len(items), payload.sort_order - 1))
        items.insert(target_index, playlist_item)
        resequence_legacy_playlist_items(items, db)
    else:
        db.add(playlist_item)
        db.flush()

    sync_sequence_from_legacy(campaign.id, db)
    db.commit()
    db.refresh(playlist_item)
    return playlist_item


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
    items = get_ordered_legacy_playlist_items(campaign.id, db)
    items_by_id = {item.id: item for item in items}

    if sorted(payload.ordered_item_ids) != sorted(items_by_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ordered_item_ids must contain every playlist item exactly once")

    ordered_items = [items_by_id[item_id] for item_id in payload.ordered_item_ids]
    resequence_legacy_playlist_items(ordered_items, db)
    sync_sequence_from_legacy(campaign.id, db)
    db.commit()
    return ordered_items


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
    source_item = get_legacy_playlist_item_or_404(campaign.id, playlist_item_id, db)
    ordered_items = get_ordered_legacy_playlist_items(campaign.id, db)
    source_index = next((index for index, item in enumerate(ordered_items) if item.id == source_item.id), len(ordered_items) - 1)

    duplicate_item = CampaignSequenceItem(
        campaign_id=campaign.id,
        item_type=CampaignSequenceItemType.CONTENT,
        content_id=source_item.content_id,
        layout_id=None,
        sort_order=len(ordered_items) + 1000,
        duration_seconds=source_item.duration_seconds,
        options_json={"zone_key": source_item.zone_key},
        is_enabled=True,
    )
    db.add(duplicate_item)
    db.flush()
    sequence_items = get_ordered_sequence_items(campaign.id, db)
    source_sequence = next((item for item in sequence_items if item.content_id == source_item.content_id and item.duration_seconds == source_item.duration_seconds), duplicate_item)
    sequence_items = [item for item in sequence_items if item.id != duplicate_item.id]
    insert_index = next((index for index, item in enumerate(sequence_items) if item.id == source_sequence.id), source_index) + 1
    sequence_items.insert(insert_index, duplicate_item)
    resequence_sequence_items(sequence_items, db)
    legacy_items = sync_legacy_playlist_from_sequence(campaign.id, db)
    db.commit()
    created_item = legacy_items[min(insert_index, len(legacy_items) - 1)] if legacy_items else None
    if not created_item:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No se pudo duplicar el item de playlist")
    db.refresh(created_item)
    return created_item


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
    db.delete(playlist_item)
    db.flush()
    items = get_ordered_legacy_playlist_items(campaign.id, db)
    resequence_legacy_playlist_items(items, db)
    sync_sequence_from_legacy(campaign.id, db)
    db.commit()


@router.get("/{campaign_id}/sequence", response_model=list[CampaignSequenceItemRead])
def get_sequence(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CampaignSequenceItem]:
    get_campaign_in_scope(campaign_id, db, current_user)
    sequence_items = resolve_effective_sequence_items(campaign_id, db)
    db.commit()
    return sequence_items


@router.get("/{campaign_id}/sequence-preview")
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
    item_type = CampaignSequenceItemType(payload.item_type)
    content_id, layout_id = validate_sequence_item_target(
        campaign,
        item_type=item_type,
        content_id=payload.content_id,
        layout_id=payload.layout_id,
        db=db,
    )
    content = get_sequence_target_content(content_id, db)

    sequence_item = CampaignSequenceItem(
        campaign_id=campaign.id,
        item_type=item_type,
        content_id=content_id,
        layout_id=layout_id,
        sort_order=len(get_ordered_sequence_items(campaign.id, db)) + 1000,
        duration_seconds=resolve_sequence_item_duration(
            item_type=item_type,
            content=content,
            campaign_default_duration_seconds=campaign.default_duration_seconds,
            requested_duration_seconds=payload.duration_seconds,
        ),
        options_json=payload.options_json,
        is_enabled=payload.is_enabled,
    )
    db.add(sequence_item)
    db.flush()

    ordered_items = get_ordered_sequence_items(campaign.id, db)
    ordered_items = [item for item in ordered_items if item.id != sequence_item.id]
    target_index = max(0, min(len(ordered_items), payload.sort_order - 1))
    ordered_items.insert(target_index, sequence_item)
    resequence_sequence_items(ordered_items, db)
    sync_legacy_playlist_from_sequence(campaign.id, db)
    db.commit()
    db.refresh(sequence_item)
    return sequence_item


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
    sequence_item = get_sequence_item_or_404(campaign.id, sequence_item_id, db)
    sequence_content = get_sequence_target_content(sequence_item.content_id, db)

    if payload.duration_seconds is not None:
        sequence_item.duration_seconds = resolve_sequence_item_duration(
            item_type=sequence_item.item_type,
            content=sequence_content,
            campaign_default_duration_seconds=campaign.default_duration_seconds,
            requested_duration_seconds=(
                payload.duration_seconds
                if sequence_item.item_type == CampaignSequenceItemType.LAYOUT or content_supports_manual_duration(sequence_content)
                else sequence_content.duration_seconds if sequence_content else payload.duration_seconds
            ),
        )
    if payload.options_json is not None:
        sequence_item.options_json = payload.options_json
    if payload.is_enabled is not None:
        sequence_item.is_enabled = payload.is_enabled

    if payload.sort_order is not None:
        items = get_ordered_sequence_items(campaign.id, db)
        items = [item for item in items if item.id != sequence_item.id]
        target_index = max(0, min(len(items), payload.sort_order - 1))
        items.insert(target_index, sequence_item)
        resequence_sequence_items(items, db)
    else:
        db.add(sequence_item)
        db.flush()

    sync_legacy_playlist_from_sequence(campaign.id, db)
    db.commit()
    db.refresh(sequence_item)
    return sequence_item


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
    items = resolve_effective_sequence_items(campaign.id, db)
    items_by_id = {item.id: item for item in items}

    if sorted(payload.ordered_item_ids) != sorted(items_by_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ordered_item_ids must contain every sequence item exactly once")

    ordered_items = [items_by_id[item_id] for item_id in payload.ordered_item_ids]
    resequence_sequence_items(ordered_items, db)
    sync_legacy_playlist_from_sequence(campaign.id, db)
    db.commit()
    return ordered_items


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
    source_item = get_sequence_item_or_404(campaign.id, sequence_item_id, db)
    ordered_items = resolve_effective_sequence_items(campaign.id, db)
    source_index = next((index for index, item in enumerate(ordered_items) if item.id == source_item.id), len(ordered_items) - 1)

    duplicate_item = CampaignSequenceItem(
        campaign_id=campaign.id,
        item_type=source_item.item_type,
        content_id=source_item.content_id,
        layout_id=source_item.layout_id,
        sort_order=len(ordered_items) + 1000,
        duration_seconds=source_item.duration_seconds,
        options_json=source_item.options_json,
        is_enabled=source_item.is_enabled,
    )
    db.add(duplicate_item)
    db.flush()

    ordered_items.insert(source_index + 1, duplicate_item)
    resequence_sequence_items(ordered_items, db)
    sync_legacy_playlist_from_sequence(campaign.id, db)
    db.commit()
    db.refresh(duplicate_item)
    return duplicate_item


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
    sequence_item = get_sequence_item_or_404(campaign.id, sequence_item_id, db)
    db.delete(sequence_item)
    db.flush()
    items = get_ordered_sequence_items(campaign.id, db)
    resequence_sequence_items(items, db)
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
    if existing_assignment:
        return CampaignAssignmentResult(
            assignment_id=existing_assignment.id,
            campaign_id=campaign_id,
            channel_id=payload.channel_id,
            assignment_status="existing",
            detail="La campaña ya estaba publicada en este canal",
        )

    assignment = CampaignChannelAssignment(
        campaign_id=campaign_id,
        channel_id=payload.channel_id,
        priority=payload.priority,
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        duplicate_assignment = db.scalar(
            select(CampaignChannelAssignment).where(
                CampaignChannelAssignment.campaign_id == campaign_id,
                CampaignChannelAssignment.channel_id == payload.channel_id,
            )
        )
        if duplicate_assignment:
            return CampaignAssignmentResult(
                assignment_id=duplicate_assignment.id,
                campaign_id=campaign_id,
                channel_id=payload.channel_id,
                assignment_status="existing",
                detail="La campaña ya estaba publicada en este canal",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo publicar la campaña por un conflicto de asignación",
        ) from exc

    db.refresh(assignment)
    response.status_code = status.HTTP_201_CREATED
    return CampaignAssignmentResult(
        assignment_id=assignment.id,
        campaign_id=campaign_id,
        channel_id=payload.channel_id,
        assignment_status="created",
        detail="Campaña publicada",
    )

