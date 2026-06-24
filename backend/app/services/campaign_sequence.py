from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.entities import Campaign, CampaignPlaybackMode, CampaignPlaylistItem, CampaignSequenceItem, CampaignSequenceItemType, ContentItem, Layout


def get_ordered_legacy_playlist_items(campaign_id: str, db: Session) -> list[CampaignPlaylistItem]:
    return list(
        db.scalars(
            select(CampaignPlaylistItem)
            .where(CampaignPlaylistItem.campaign_id == campaign_id)
            .order_by(CampaignPlaylistItem.sort_order.asc(), CampaignPlaylistItem.created_at.asc())
        )
    )


def get_ordered_sequence_items(campaign_id: str, db: Session, *, enabled_only: bool = False) -> list[CampaignSequenceItem]:
    query = (
        select(CampaignSequenceItem)
        .where(CampaignSequenceItem.campaign_id == campaign_id)
        .order_by(CampaignSequenceItem.sort_order.asc(), CampaignSequenceItem.created_at.asc())
    )
    if enabled_only:
        query = query.where(CampaignSequenceItem.is_enabled.is_(True))
    return list(db.scalars(query))


def get_sequence_item_or_404(campaign_id: str, sequence_item_id: str, db: Session) -> CampaignSequenceItem:
    item = db.scalar(
        select(CampaignSequenceItem).where(
            CampaignSequenceItem.id == sequence_item_id,
            CampaignSequenceItem.campaign_id == campaign_id,
        )
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sequence item not found")
    return item


def get_legacy_playlist_item_or_404(campaign_id: str, playlist_item_id: str, db: Session) -> CampaignPlaylistItem:
    item = db.scalar(
        select(CampaignPlaylistItem).where(
            CampaignPlaylistItem.id == playlist_item_id,
            CampaignPlaylistItem.campaign_id == campaign_id,
        )
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist item not found")
    return item


def resequence_legacy_playlist_items(items: list[CampaignPlaylistItem], db: Session) -> None:
    existing_items = [item for item in items if item.id]
    for index, item in enumerate(existing_items, start=1):
        item.sort_order = -index
        db.add(item)
    db.flush()

    for index, item in enumerate(items, start=1):
        item.sort_order = index
        db.add(item)
    db.flush()


def resequence_sequence_items(items: list[CampaignSequenceItem], db: Session) -> None:
    existing_items = [item for item in items if item.id]
    for index, item in enumerate(existing_items, start=1):
        item.sort_order = -index
        db.add(item)
    db.flush()

    for index, item in enumerate(items, start=1):
        item.sort_order = index
        db.add(item)
    db.flush()


def sequence_item_zone_key(item: CampaignSequenceItem) -> str:
    options = item.options_json or {}
    zone_key = options.get("zone_key")
    return zone_key.strip() if isinstance(zone_key, str) and zone_key.strip() else "main"


def normalize_playback_mode(raw_mode: str | None) -> str:
    if raw_mode == CampaignPlaybackMode.RANDOM.value:
        return CampaignPlaybackMode.RANDOM.value
    return CampaignPlaybackMode.SEQUENTIAL.value


def validate_sequence_item_target(
    campaign: Campaign,
    *,
    item_type: CampaignSequenceItemType,
    content_id: str | None,
    layout_id: str | None,
    db: Session,
) -> tuple[str | None, str | None]:
    if item_type == CampaignSequenceItemType.CONTENT:
        if not content_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="content_id is required for content items")
        content = db.get(ContentItem, content_id)
        if not content or content.client_id != campaign.client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
        return content.id, None

    if not layout_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="layout_id is required for layout items")
    layout = db.get(Layout, layout_id)
    if not layout or layout.client_id != campaign.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout not found")
    return None, layout.id


def sync_sequence_from_legacy(campaign_id: str, db: Session) -> list[CampaignSequenceItem]:
    legacy_items = get_ordered_legacy_playlist_items(campaign_id, db)
    db.execute(delete(CampaignSequenceItem).where(CampaignSequenceItem.campaign_id == campaign_id))
    db.flush()

    created: list[CampaignSequenceItem] = []
    for item in legacy_items:
        sequence_item = CampaignSequenceItem(
            campaign_id=campaign_id,
            item_type=CampaignSequenceItemType.CONTENT,
            content_id=item.content_id,
            layout_id=None,
            sort_order=item.sort_order,
            duration_seconds=item.duration_seconds,
            options_json={"zone_key": item.zone_key},
            is_enabled=True,
        )
        db.add(sequence_item)
        created.append(sequence_item)

    db.flush()
    return get_ordered_sequence_items(campaign_id, db)


def sync_legacy_playlist_from_sequence(campaign_id: str, db: Session) -> list[CampaignPlaylistItem]:
    sequence_items = get_ordered_sequence_items(campaign_id, db, enabled_only=False)
    compatible_items = [
        item
        for item in sequence_items
        if item.is_enabled and item.item_type == CampaignSequenceItemType.CONTENT and item.content_id
    ]

    db.execute(delete(CampaignPlaylistItem).where(CampaignPlaylistItem.campaign_id == campaign_id))
    db.flush()

    created: list[CampaignPlaylistItem] = []
    for index, item in enumerate(compatible_items, start=1):
        playlist_item = CampaignPlaylistItem(
            campaign_id=campaign_id,
            content_id=item.content_id,
            sort_order=index,
            duration_seconds=item.duration_seconds,
            zone_key=sequence_item_zone_key(item),
        )
        db.add(playlist_item)
        created.append(playlist_item)

    db.flush()
    return get_ordered_legacy_playlist_items(campaign_id, db)


def resolve_effective_sequence_items(campaign_id: str, db: Session) -> list[CampaignSequenceItem]:
    sequence_items = get_ordered_sequence_items(campaign_id, db)
    if sequence_items:
        return sequence_items

    legacy_items = get_ordered_legacy_playlist_items(campaign_id, db)
    if not legacy_items:
        return []

    return sync_sequence_from_legacy(campaign_id, db)
