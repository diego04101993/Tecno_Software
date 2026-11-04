from __future__ import annotations

from uuid import uuid4
from copy import deepcopy

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.entities import Campaign, CampaignPlaybackMode, CampaignPlaylistItem, CampaignSequenceItem, CampaignSequenceItemType, ContentItem, Layout
from app.services.tenant_mutations import require_delete_coverage


_LEGACY_COLUMNS = ("campaign_id", "content_id", "sort_order", "duration_seconds", "zone_key")
_SEQUENCE_COLUMNS = ("campaign_id", "item_type", "content_id", "layout_id", "sort_order",
                     "duration_seconds", "options_json", "is_enabled")


def _item_copies(rows, fields):
    return [dict(id=row.id, **{field: deepcopy(getattr(row, field)) for field in fields}) for row in rows]


def _sequence_copy_from_legacy(items, *, preserve_ids=False):
    return [dict(id=item["id"] if preserve_ids else str(uuid4()), campaign_id=item["campaign_id"],
                 item_type=CampaignSequenceItemType.CONTENT, content_id=item["content_id"], layout_id=None,
                 sort_order=item["sort_order"], duration_seconds=item["duration_seconds"],
                 options_json={"zone_key": item["zone_key"]}, is_enabled=True) for item in items]


def _legacy_copy_from_sequence(items):
    compatible = [item for item in items if item["is_enabled"] and
                  item["item_type"] == CampaignSequenceItemType.CONTENT and item["content_id"]]
    result = []
    for index, item in enumerate(compatible, 1):
        zone = (item["options_json"] or {}).get("zone_key")
        result.append(dict(id=str(uuid4()), campaign_id=item["campaign_id"], content_id=item["content_id"],
                           sort_order=index, duration_seconds=item["duration_seconds"],
                           zone_key=zone.strip() if isinstance(zone, str) and zone.strip() else "main"))
    return result


def _execute_item_batch(db, campaign, actor, operation, legacy, sequence, expected_ids):
    """Two explicit collections; prove old and new rows before DELETE or setters."""
    from app.services.tenant_mutations import MutationIntent, write_guard
    from app.services.tenant_ownership import RowRef
    from app.services.tenant_relations import conflict

    old_legacy = get_ordered_legacy_playlist_items(campaign.id, db)
    old_sequence = get_ordered_sequence_items(campaign.id, db)
    if ({item.id for item in old_legacy}, {item.id for item in old_sequence}) != expected_ids:
        raise conflict()
    batches = ((CampaignPlaylistItem, old_legacy, legacy, _LEGACY_COLUMNS),
               (CampaignSequenceItem, old_sequence, sequence, _SEQUENCE_COLUMNS))
    intents = [MutationIntent("UPDATE", RowRef("Campaign", campaign.id))]
    for model, old, proposed, fields in batches:
        old_ids = {item.id for item in old}
        new_ids = {item["id"] for item in proposed}
        if len(new_ids) != len(proposed):
            raise conflict()
        intents.extend(MutationIntent("DELETE", RowRef(model.__name__, identity)) for identity in sorted(old_ids - new_ids))
        for item in proposed:
            values = {field: item[field] for field in fields}
            if item["id"] not in old_ids:
                values.update({field: item[field] for field in ("created_at", "updated_at") if field in item})
            intents.append(MutationIntent("UPDATE" if item["id"] in old_ids else "CREATE",
                RowRef(model.__name__, item["id"]), tuple(values.items())))
    with write_guard(db, actor=actor, client_id=campaign.client_id, operation=operation, intents=tuple(intents)):
        for model, old, proposed, fields in batches:
            if set(db.scalars(select(model.id).where(model.campaign_id == campaign.id))) != {item.id for item in old}:
                raise conflict()
        for model, old, proposed, fields in batches:
            existing = {item.id: item for item in old}
            retained = {item["id"] for item in proposed}
            for item in old:
                if item.id not in retained:
                    db.delete(item)
            # Preserve the existing collision-free resequence protocol.
            for index, item in enumerate(old, 1):
                if item.id in retained:
                    item.sort_order = -index
            db.flush()
            for values in proposed:
                item = existing.get(values["id"])
                if item is None:
                    new_values = {field: values[field] for field in fields}
                    new_values.update({field: values[field] for field in ("created_at", "updated_at") if field in values})
                    item = model(id=values["id"], **new_values)
                else:
                    for field in fields:
                        setattr(item, field, values[field])
                db.add(item)
            db.flush()
        db.commit()
        return get_ordered_legacy_playlist_items(campaign.id, db), get_ordered_sequence_items(campaign.id, db)


def edit_campaign_items(campaign, db, actor, *, operation, side, action, payload=None, item_id=None):
    """Pure preparation followed by one guarded batch; operation is server-owned."""
    from app.services.content_cleanup import content_supports_manual_duration, resolve_sequence_item_duration
    from app.services.tenant_relations import conflict
    expected_operation = f"{action}_{side}" if action == "reorder" else f"{action}_{side}_item"
    if side not in {"playlist", "sequence"} or action not in {"add", "update", "reorder", "duplicate"} or operation != expected_operation:
        raise conflict()
    legacy_rows = get_ordered_legacy_playlist_items(campaign.id, db)
    sequence_rows = get_ordered_sequence_items(campaign.id, db)
    expected_ids = ({item.id for item in legacy_rows}, {item.id for item in sequence_rows})
    legacy = _item_copies(legacy_rows, _LEGACY_COLUMNS)
    sequence = _item_copies(sequence_rows, _SEQUENCE_COLUMNS)
    if side == "sequence" and not sequence:
        sequence = _sequence_copy_from_legacy(legacy, preserve_ids=True)
        for item, source in zip(sequence, legacy_rows):
            item.update(created_at=source.created_at, updated_at=source.updated_at)
    items = legacy if side == "playlist" else sequence
    target = next((item for item in items if item["id"] == item_id), None) if item_id else None
    if item_id and target is None:
        raise HTTPException(status_code=404, detail="Playlist item not found" if side == "playlist" else "Sequence item not found")
    result_id, result_index = item_id, None
    if action == "reorder":
        if sorted(payload.ordered_item_ids) != sorted(item["id"] for item in items):
            raise HTTPException(status_code=400, detail=f"ordered_item_ids must contain every {side} item exactly once")
        by_id = {item["id"]: item for item in items}
        items[:] = [by_id[identity] for identity in payload.ordered_item_ids]
        for index, item in enumerate(items, 1):
            item["sort_order"] = index
    elif action == "update":
        kind = CampaignSequenceItemType.CONTENT if side == "playlist" else target["item_type"]
        content = db.get(ContentItem, target["content_id"]) if target.get("content_id") else None
        if payload.duration_seconds is not None:
            requested = (payload.duration_seconds if kind == CampaignSequenceItemType.LAYOUT or content_supports_manual_duration(content)
                         else content.duration_seconds if content else payload.duration_seconds)
            target["duration_seconds"] = resolve_sequence_item_duration(item_type=kind, content=content,
                campaign_default_duration_seconds=campaign.default_duration_seconds, requested_duration_seconds=requested)
        if side == "playlist":
            if payload.zone_key is not None:
                target["zone_key"] = payload.zone_key.strip() or "main"
        else:
            if payload.options_json is not None:
                target["options_json"] = deepcopy(payload.options_json)
            if payload.is_enabled is not None:
                target["is_enabled"] = payload.is_enabled
        if payload.sort_order is not None:
            items.remove(target)
            items.insert(max(0, min(len(items), payload.sort_order - 1)), target)
            for index, item in enumerate(items, 1):
                item["sort_order"] = index
    elif action in {"add", "duplicate"}:
        identity = str(uuid4())
        if action == "add":
            kind = CampaignSequenceItemType.CONTENT if side == "playlist" else CampaignSequenceItemType(payload.item_type)
            content_id, layout_id = validate_sequence_item_target(campaign, item_type=kind, content_id=payload.content_id,
                layout_id=None if side == "playlist" else payload.layout_id, db=db)
            content = db.get(ContentItem, content_id) if content_id else None
            new = dict(id=identity, campaign_id=campaign.id, item_type=kind, content_id=content_id, layout_id=layout_id,
                sort_order=max(1, payload.sort_order), duration_seconds=resolve_sequence_item_duration(item_type=kind,
                content=content, campaign_default_duration_seconds=campaign.default_duration_seconds,
                requested_duration_seconds=payload.duration_seconds),
                options_json={"zone_key": payload.zone_key} if side == "playlist" else deepcopy(payload.options_json),
                is_enabled=True if side == "playlist" else payload.is_enabled)
            index = max(0, min(len(sequence), payload.sort_order - 1))
        else:
            source_index = items.index(target)
            new = (_sequence_copy_from_legacy([target])[0] if side == "playlist" else deepcopy(target))
            new["id"] = identity
            new.pop("created_at", None)
            new.pop("updated_at", None)
            source_index = next((i for i, item in enumerate(sequence)
                if item["content_id"] == target["content_id"] and item["duration_seconds"] == target["duration_seconds"]),
                source_index) if side == "playlist" else source_index
            index = source_index + 1
        if side == "playlist" and action == "add":
            sequence.append(new)
            sequence.sort(key=lambda item: item["sort_order"])
            result_index = max(1, payload.sort_order) - 1
        else:
            sequence.insert(index, new)
            for order, item in enumerate(sequence, 1):
                item["sort_order"] = order
            result_index = index
        result_id = identity
    else:
        from app.services.tenant_relations import conflict
        raise conflict()
    if side == "playlist" and action in {"update", "reorder"}:
        sequence = _sequence_copy_from_legacy(legacy)
    else:
        legacy = _legacy_copy_from_sequence(sequence)
    saved_legacy, saved_sequence = _execute_item_batch(db, campaign, actor, operation, legacy, sequence, expected_ids)
    saved = saved_legacy if side == "playlist" else saved_sequence
    if action == "reorder":
        return saved
    if side == "playlist" and action in {"add", "duplicate"}:
        return saved[min(result_index, len(saved) - 1)]
    return next(item for item in saved if item.id == result_id)


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
    require_delete_coverage(db, items)
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
    require_delete_coverage(db, items)
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
    require_delete_coverage(db, (db.get(Campaign, campaign_id),))
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
    require_delete_coverage(db, (db.get(Campaign, campaign_id),))
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
