"""Durable, tenant-scoped publication authority for opt-in Android node slices.

Processing runs outside transactions. This service alone fences worker results,
registers generated ContentItems and activates an immutable shared timeline.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
from types import MappingProxyType
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from app.core.config import get_settings
from app.models.entities import (
    Branch, Campaign, CampaignPlaybackMode, CampaignPlaylistItem,
    CampaignSequenceItem, Channel, ChannelMode, Client, ContentItem, ContentType,
    User, Videowall, VideowallNode, VideowallPublication, VideowallPublicationAsset,
)
from app.schemas.domain import VideowallPublicationRead
from app.services.media_references import canonical_media_reference, media_owned_by
from app.services.media_storage import MediaPathError, resolve_stored_media_path
from app.services.tenancy import can_write_client_scope, require_client_scope
from app.services.tenant_mutations import MutationIntent, write_guard
from app.services.tenant_ownership import RowRef, context_for_actor
from app.services.videowall_geometry import compute_videowall_cell_by_row_col


ENCODING_PROFILE = "code302_h264_cfr_v1"
ACTIVATION_LEAD_MS = 10_000
PUBLICATION_OPERATION = "prepare_videowall_publication"
MAX_PROGRAM_ITEMS = 1024
MAX_TIMELINE_MS = (1 << 63) - 1


def now_utc():
    return datetime.now(UTC)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _reject(code, status=409):
    raise HTTPException(status_code=status, detail=code)


def _value(row, key, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def require_node_slice_geometry(wall):
    """Pure V1 preset validation; never reinterpret a legacy wall."""
    if (_value(wall, "render_mode") != "multi-node"
            or _value(wall, "rows") != 1 or _value(wall, "columns") != 3):
        _reject("VW1_REQUIRES_1X3_MULTI_NODE", 422)
    dimensions = (_value(wall, "total_width"), _value(wall, "total_height"))
    panels = {(5760, 1080): (1920, 1080), (11520, 2160): (3840, 2160)}
    panel = panels.get(dimensions)
    if panel is None:
        _reject("VW1_UNSUPPORTED_CANVAS", 422)
    if (_value(wall, "output_width"), _value(wall, "output_height")) != panel:
        _reject("VW1_PANEL_DIMENSIONS_MISMATCH", 422)
    tolerance = _value(wall, "start_tolerance_ms", 250)
    if type(tolerance) is not int or not 1 <= tolerance <= 60_000:
        _reject("VW1_INVALID_TOLERANCE", 422)
    return panel


def _current_group(db, wall, identity, *, campaign_id=None, branch_id=None):
    from app.models.entities import VideowallPlaybackGroup
    group = db.scalar(select(VideowallPlaybackGroup).where(VideowallPlaybackGroup.id == identity)
                      .execution_options(populate_existing=True))
    if (group is None or group.wall_id != wall.id or group.client_id != wall.client_id
            or not wall.playback_groups_enabled or group.mode != "SYNCHRONIZED"
            or (campaign_id is not None and group.campaign_id != campaign_id)
            or (branch_id is not None and group.branch_id != branch_id)):
        _reject("VW1_PLAYBACK_GROUP_UNAVAILABLE")
    return group


def build_geometry_snapshot(db, wall, playback_group=None):
    if playback_group is not None:
        from app.services.videowall_playback_groups import build_group_geometry_snapshot
        group = _current_group(db, wall, _value(playback_group, "id"))
        return build_group_geometry_snapshot(db, group)
    panel = require_node_slice_geometry(wall)
    nodes = list(db.execute(select(
        VideowallNode.id, VideowallNode.channel_id, VideowallNode.position_index,
        VideowallNode.row_index, VideowallNode.column_index,
        Channel.client_id, Channel.branch_id, Channel.mode,
        Channel.resolution_width, Channel.resolution_height,
        Channel.canvas_width, Channel.canvas_height,
    ).join(Channel, Channel.id == VideowallNode.channel_id)
      .where(VideowallNode.videowall_id == wall.id)
      .order_by(VideowallNode.position_index)).mappings())
    if len(nodes) != 3 or {n["position_index"] for n in nodes} != {1, 2, 3}:
        _reject("VW1_REQUIRES_THREE_DISTINCT_NODES", 422)
    if len({n["channel_id"] for n in nodes}) != 3 or len({n["branch_id"] for n in nodes}) != 1:
        _reject("VW1_INVALID_NODE_BINDINGS")
    branch_id = nodes[0]["branch_id"]
    branch = db.execute(select(Branch.client_id, Branch.is_active).where(Branch.id == branch_id)).mappings().first()
    if not branch or branch["client_id"] != wall.client_id or not branch["is_active"]:
        _reject("VW1_INVALID_NODE_BINDINGS")
    result = dict(rows=1, columns=3, total_width=wall.total_width,
                  total_height=wall.total_height, panel_width=panel[0], panel_height=panel[1],
                  tolerance_ms=wall.start_tolerance_ms, nodes=[])
    for n in nodes:
        if (n["client_id"] != wall.client_id or n["mode"] != ChannelMode.VIDEOWALL
                or n["row_index"] != 0 or n["column_index"] != n["position_index"] - 1
                or (n["resolution_width"], n["resolution_height"]) != panel
                or (n["canvas_width"], n["canvas_height"]) != panel):
            _reject("VW1_INVALID_NODE_BINDINGS")
        cell = compute_videowall_cell_by_row_col(
            columns=3, rows=1, total_width=wall.total_width, total_height=wall.total_height,
            row_index=n["row_index"], column_index=n["column_index"])
        result["nodes"].append(dict(id=n["id"], channel_id=n["channel_id"],
            position_index=cell.position_index, row_index=0, column_index=cell.column_index,
            crop_x=cell.x, crop_y=cell.y, crop_width=cell.width, crop_height=cell.height))
    return result


def _publication_context(db, publication):
    wall = db.scalar(select(Videowall).where(Videowall.id == publication.wall_id)
                     .execution_options(populate_existing=True))
    if wall is None or wall.client_id != publication.client_id:
        _reject("VW1_GEOMETRY_CHANGED")
    group_id = getattr(publication, "playback_group_id", None)
    group = (_current_group(db, wall, group_id, campaign_id=publication.campaign_id,
                            branch_id=publication.branch_id) if group_id is not None else None)
    return wall, group


def _current_publication_geometry(db, publication):
    wall, group = _publication_context(db, publication)
    geometry = build_geometry_snapshot(db, wall, group)
    if geometry_signature(geometry) != publication.geometry_signature:
        _reject("VW1_GEOMETRY_CHANGED")
    return geometry


def geometry_signature(snapshot):
    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(child) for key, child in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(child) for child in value]
        return value
    return hashlib.sha256(json.dumps(plain(snapshot), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_program_snapshot(snapshot, *, ready=False):
    """Typed backend snapshot, not free-form request metadata or ownership proof."""
    if not isinstance(snapshot, Mapping) or set(snapshot) != {"campaign_id", "items"}:
        _reject("VW1_INVALID_PROGRAM_SNAPSHOT")
    items = snapshot.get("items")
    if (not isinstance(snapshot["campaign_id"], str)
            or not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_PROGRAM_ITEMS):
        _reject("VW1_INVALID_PROGRAM_SNAPSHOT")
    seen, total = set(), 0
    for index, item in enumerate(items, 1):
        if (not isinstance(item, Mapping)
                or set(item) != {"id", "master_content_id", "type", "sort_order", "duration_ms"}
                or not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 36
                or not isinstance(item["master_content_id"], str) or not 1 <= len(item["master_content_id"]) <= 36
                or item["id"] in seen or item["type"] not in {"image", "video"}
                or type(item["sort_order"]) is not int or item["sort_order"] != index):
            _reject("VW1_INVALID_PROGRAM_SNAPSHOT")
        seen.add(item["id"])
        duration = item["duration_ms"]
        if duration is None and item["type"] == "video" and not ready:
            continue
        if type(duration) is not int or not 0 < duration <= MAX_TIMELINE_MS:
            _reject("VW1_INVALID_PROGRAM_SNAPSHOT")
        total += duration
        if total > MAX_TIMELINE_MS:
            _reject("VW1_INVALID_PROGRAM_SNAPSHOT")
    return snapshot


def _current_master(db, client_id, identity, *, expected_type=None):
    """Fresh authoritative columns, canonical path and owner before file access."""
    client = db.execute(select(Client.id, Client.status).where(Client.id == client_id)).mappings().first()
    row = db.execute(select(ContentItem.id, ContentItem.client_id, ContentItem.type,
                            ContentItem.file_path).where(ContentItem.id == identity)).mappings().first()
    if (not client or client["status"] != "active" or not row or row["client_id"] != client_id
            or row["type"] not in {ContentType.IMAGE, ContentType.VIDEO} or not row["file_path"]
            or (expected_type and row["type"].value != expected_type)):
        _reject("VW1_MEDIA_UNAVAILABLE")
    root = get_settings().MEDIA_ROOT
    try:
        classification, key = canonical_media_reference(row["file_path"])
        path = resolve_stored_media_path(root, row["file_path"])
        if (classification != "LOCAL" or path is None
                or resolve_stored_media_path(root, key) != path
                or not media_owned_by(db, root, path, client_id)):
            _reject("VW1_MEDIA_UNAVAILABLE")
    except (MediaPathError, OSError, ValueError):
        _reject("VW1_MEDIA_UNAVAILABLE")
    return row, path


def _program_snapshot(db, wall, campaign_id):
    campaign = db.execute(select(Campaign.id, Campaign.client_id, Campaign.is_active,
                                 Campaign.playback_mode, Campaign.layout_id).where(Campaign.id == campaign_id)).mappings().first()
    if not campaign or campaign["client_id"] != wall.client_id or not campaign["is_active"]:
        _reject("Campaign not found", 404)
    if campaign["playback_mode"] != CampaignPlaybackMode.SEQUENTIAL.value:
        _reject("VW1_REQUIRES_SEQUENTIAL_PLAYBACK", 422)
    if campaign["layout_id"] is not None:
        _reject("VW1_IMAGE_VIDEO_ONLY", 422)
    rows = list(db.scalars(select(CampaignSequenceItem).where(
        CampaignSequenceItem.campaign_id == campaign_id).order_by(
        CampaignSequenceItem.sort_order, CampaignSequenceItem.created_at)))
    if rows:
        rows = [r for r in rows if r.is_enabled]
    else:
        rows = list(db.scalars(select(CampaignPlaylistItem).where(
            CampaignPlaylistItem.campaign_id == campaign_id)
            .order_by(CampaignPlaylistItem.sort_order, CampaignPlaylistItem.created_at)))
    if not 1 <= len(rows) <= MAX_PROGRAM_ITEMS:
        _reject("VW1_INVALID_PROGRAM_LENGTH", 422)
    items = []
    for index, row in enumerate(rows, 1):
        if not row.content_id or getattr(row, "layout_id", None):
            _reject("VW1_IMAGE_VIDEO_ONLY", 422)
        master, _ = _current_master(db, wall.client_id, row.content_id)
        if master["type"] == ContentType.IMAGE:
            if type(row.duration_seconds) is not int or row.duration_seconds <= 0:
                _reject("VW1_INVALID_IMAGE_DURATION", 422)
            duration_ms = row.duration_seconds * 1000
        else:
            duration_ms = None  # FFprobe, never the historical default15, is authoritative.
        items.append(dict(id=row.id, master_content_id=row.content_id,
                          type=master["type"].value, sort_order=index, duration_ms=duration_ms))
    return validate_program_snapshot(dict(campaign_id=campaign_id, items=items))


def publication_read(db, publication, wall=None):
    wall = wall or db.get(Videowall, publication.wall_id)
    selected = wall
    group_id = getattr(publication, "playback_group_id", None)
    if group_id is not None:
        from app.models.entities import VideowallPlaybackGroup
        selected = db.get(VideowallPlaybackGroup, group_id)
    count = db.scalar(select(func.count()).select_from(VideowallPublicationAsset).where(
        VideowallPublicationAsset.publication_id == publication.id,
        VideowallPublicationAsset.validated.is_(True)))
    return VideowallPublicationRead(
        id=publication.id, wall_id=publication.wall_id, campaign_id=publication.campaign_id,
        playback_group_id=group_id,
        status=publication.status, expected_asset_count=publication.expected_asset_count,
        validated_asset_count=count, loop_duration_ms=publication.loop_duration_ms,
        loop_start_epoch_ms=publication.loop_start_epoch_ms, error_code=publication.error_code,
        error_message=publication.error_message, activated_at=publication.activated_at,
        created_at=publication.created_at, updated_at=publication.updated_at,
        is_current_active=bool(selected and selected.active_publication_id == publication.id))


def _authorize(db, actor, client_id):
    context_for_actor(db, actor, client_id=client_id)
    if not can_write_client_scope(actor) or require_client_scope(actor, client_id) != client_id:
        _reject("Publication operation unavailable", 403)
    return actor


def create_publication(db, wall, campaign_id, actor, playback_group=None):
    _authorize(db, actor, wall.client_id)
    if playback_group is None and wall.media_delivery != "node_slice":
        _reject("VW1_NODE_SLICE_NOT_ENABLED", 422)
    group = (_current_group(db, wall, _value(playback_group, "id"), campaign_id=campaign_id)
             if playback_group is not None else None)
    geometry = build_geometry_snapshot(db, wall, group)
    program = _program_snapshot(db, wall, campaign_id)
    branch_id = db.scalar(select(Channel.branch_id).where(Channel.id == geometry["nodes"][0]["channel_id"]))
    identity = str(uuid4())
    values = dict(client_id=wall.client_id, branch_id=branch_id, wall_id=wall.id,
                  playback_group_id=group.id if group is not None else None,
                  campaign_id=campaign_id, requested_by_user_id=actor.id, status="PENDING",
                  program_snapshot_json=program, geometry_snapshot_json=geometry,
                  geometry_signature=geometry_signature(geometry), encoding_profile=ENCODING_PROFILE,
                  expected_asset_count=len(program["items"]) * len(geometry["nodes"]), attempt_count=0)
    assets = [VideowallPublicationAsset(id=str(uuid4()), publication_id=identity,
        node_id=node["id"], master_content_id=item["master_content_id"],
        logical_item_id=item["id"], position_index=node["position_index"], sort_order=item["sort_order"],
        width=geometry["panel_width"], height=geometry["panel_height"],
        duration_ms=None, validated=False)
        for item in program["items"] for node in geometry["nodes"]]
    asset_fields = ("publication_id", "node_id", "master_content_id", "logical_item_id",
                    "position_index", "sort_order", "width", "height", "duration_ms", "validated")
    intents = (MutationIntent("CREATE", RowRef("VideowallPublication", identity), tuple(values.items())),)
    intents += tuple(MutationIntent("CREATE", RowRef("VideowallPublicationAsset", a.id),
                                   tuple((k, getattr(a, k)) for k in asset_fields)) for a in assets)
    with write_guard(db, actor=actor, client_id=wall.client_id, operation=PUBLICATION_OPERATION, intents=intents):
        if group is not None:
            group = _current_group(db, wall, group.id, campaign_id=campaign_id, branch_id=branch_id)
        if geometry_signature(build_geometry_snapshot(db, wall, group)) != values["geometry_signature"]:
            _reject("VW1_GEOMETRY_CHANGED")
        publication = VideowallPublication(id=identity, **values)
        db.add(publication)
        db.add_all(assets)
        db.commit()
        db.refresh(publication)
    return publication


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class PublicationWork:
    publication_id: str
    lease_token: str
    client_id: str
    branch_id: str
    wall_id: str
    campaign_id: str
    requested_by_user_id: str
    geometry_snapshot_json: Mapping
    program_snapshot_json: Mapping
    encoding_profile: str


def _worker_actor(db, publication):
    actor = db.scalar(select(User).where(User.id == publication.requested_by_user_id)
                      .execution_options(populate_existing=True))
    if actor is None:
        _reject("Publication operation unavailable", 403)
    return _authorize(db, actor, publication.client_id)


def _lease_available(publication, now):
    return publication.status == "PENDING" or (publication.status == "PROCESSING"
        and publication.lease_expires_at is not None and _utc(publication.lease_expires_at) <= now)


def _require_lease(db, identity, token):
    publication = db.scalar(select(VideowallPublication).where(VideowallPublication.id == identity)
                            .execution_options(populate_existing=True))
    if (not publication or publication.status != "PROCESSING" or not token
            or publication.lease_token != token or publication.lease_expires_at is None
            or _utc(publication.lease_expires_at) <= now_utc()):
        _reject("VW1_LEASE_LOST")
    actor = _worker_actor(db, publication)
    _current_publication_geometry(db, publication)
    return publication, actor


def _lease_seconds(value):
    if type(value) is not int or not 1 <= value <= 3600:
        raise ValueError("Lease duration must be1..3600seconds")
    return value


def claim_publication(db, worker_id, lease_seconds=120):
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 120:
        raise ValueError("Invalid worker identifier")
    seconds = _lease_seconds(lease_seconds)
    now = now_utc()
    cursor = None
    while True:
        query = select(VideowallPublication.id, VideowallPublication.created_at).where(or_(
            VideowallPublication.status == "PENDING", and_(VideowallPublication.status == "PROCESSING",
                VideowallPublication.lease_expires_at <= now)))
        if cursor is not None:
            query = query.where(or_(VideowallPublication.created_at > cursor[0], and_(
                VideowallPublication.created_at == cursor[0], VideowallPublication.id > cursor[1])))
        candidates = list(db.execute(query.order_by(
            VideowallPublication.created_at, VideowallPublication.id).limit(50)))
        if not candidates:
            return None
        cursor = (candidates[-1].created_at, candidates[-1].id)
        for identity, _ in candidates:
            try:
                publication = db.scalar(select(VideowallPublication).where(VideowallPublication.id == identity)
                                        .execution_options(populate_existing=True))
                if publication is None or not _lease_available(publication, now):
                    continue
                actor = _worker_actor(db, publication)
                _current_publication_geometry(db, publication)
                values = dict(status="PROCESSING", lease_token=secrets.token_hex(32), lease_owner=worker_id,
                              lease_expires_at=now_utc() + timedelta(seconds=seconds),
                              attempt_count=publication.attempt_count + 1, error_code=None, error_message=None)
                intent = MutationIntent("UPDATE", RowRef("VideowallPublication", identity), tuple(values.items()))
                with write_guard(db, actor=actor, client_id=publication.client_id, operation=PUBLICATION_OPERATION,
                                 intents=(intent,)):
                    if not _lease_available(publication, now_utc()):
                        _reject("VW1_LEASE_LOST")
                    _current_publication_geometry(db, publication)
                    for key, value in values.items():
                        setattr(publication, key, value)
                    db.commit()
                    db.refresh(publication)
                program = deepcopy(publication.program_snapshot_json)
                for item in program["items"]:
                    row, _ = _current_master(db, publication.client_id, item["master_content_id"], expected_type=item["type"])
                    item["master_file_path"] = row["file_path"]  # Ephemeral, authoritative columns only.
                return PublicationWork(publication.id, publication.lease_token, publication.client_id,
                    publication.branch_id, publication.wall_id, publication.campaign_id,
                    publication.requested_by_user_id, _freeze(deepcopy(publication.geometry_snapshot_json)),
                    _freeze(program), publication.encoding_profile)
            except HTTPException:
                db.rollback()
                continue


def renew_publication_lease(db, identity, token, lease_seconds=120):
    seconds = _lease_seconds(lease_seconds)
    try:
        publication, actor = _require_lease(db, identity, token)
        deadline = now_utc() + timedelta(seconds=seconds)
        intent = MutationIntent("UPDATE", RowRef("VideowallPublication", identity), (("lease_expires_at", deadline),))
        with write_guard(db, actor=actor, client_id=publication.client_id, operation=PUBLICATION_OPERATION, intents=(intent,)):
            _require_lease(db, identity, token)
            publication.lease_expires_at = deadline
            db.commit()
        return True
    except HTTPException:
        db.rollback()
        return False


def resolve_publication_master(db, work, item):
    publication, _ = _require_lease(db, work.publication_id, work.lease_token)
    if geometry_signature(work.geometry_snapshot_json) != publication.geometry_signature:
        _reject("VW1_GEOMETRY_CHANGED")
    stored = next((value for value in publication.program_snapshot_json["items"]
                   if value["id"] == item["id"]), None)
    if (stored is None or stored["master_content_id"] != item["master_content_id"]
            or stored["type"] != item["type"]):
        _reject("VW1_MEDIA_UNAVAILABLE")
    row, path = _current_master(db, publication.client_id, stored["master_content_id"], expected_type=stored["type"])
    if row["file_path"] != item.get("master_file_path"):
        _reject("VW1_MASTER_CHANGED")
    return path


def _file_sha256(path):
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _reject("VW1_MEDIA_CHANGED")
    return digest


def _validated_prepared(publication, prepared_items):
    items = publication.program_snapshot_json["items"]
    if not isinstance(prepared_items, Mapping) or set(prepared_items) != {i["id"] for i in items}:
        _reject("VW1_INCOMPLETE_SLICES")
    geometry = publication.geometry_snapshot_json
    positions = {node["position_index"] for node in geometry["nodes"]}
    panel = (geometry["panel_width"], geometry["panel_height"])
    normalized = []
    for item in items:
        result = prepared_items[item["id"]]
        if (result.kind != item["type"] or type(result.duration_ms) is not int or result.duration_ms <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", result.source_sha256)
                or (item["type"] == "image" and result.duration_ms != item["duration_ms"])
                or len(result.slices) != len(positions)
                or any(type(s.position_index) is not int for s in result.slices)
                or {s.position_index for s in result.slices} != positions):
            _reject("VW1_INVALID_SLICE_RESULT")
        for sliced in result.slices:
            path = Path(sliced.path)
            if (path.is_symlink() or not path.is_file() or (sliced.width, sliced.height) != panel
                    or sliced.mime_type != ("image/png" if item["type"] == "image" else "video/mp4")
                    or type(sliced.duration_ms) is not int or abs(sliced.duration_ms - result.duration_ms) > geometry["tolerance_ms"]
                    or (sliced.position_index != 1 and sliced.has_audio)
                    or sliced.size_bytes != path.stat().st_size or sliced.size_bytes <= 0
                    or _file_sha256(path) != sliced.sha256):
                _reject("VW1_INVALID_SLICE_RESULT")
            if item["type"] == "video" and (
                    type(sliced.fps_num) is not int or sliced.fps_num <= 0
                    or type(sliced.fps_den) is not int or sliced.fps_den <= 0
                    or type(sliced.frame_count) is not int or sliced.frame_count <= 0
                    or (sliced.fps_num, sliced.fps_den, sliced.frame_count)
                       != (result.fps_num, result.fps_den, result.frame_count)):
                _reject("VW1_INVALID_SLICE_RESULT")
            normalized.append((item, result, sliced, path))
    return normalized


def complete_publication(db, identity, token, prepared_items):
    from app.services.media_references import generated_content_references
    publication, actor = _require_lease(db, identity, token)
    _current_publication_geometry(db, publication)
    prepared = _validated_prepared(publication, prepared_items)
    assets = list(db.scalars(select(VideowallPublicationAsset).where(
        VideowallPublicationAsset.publication_id == identity)))
    expected = {(item["id"], sliced.position_index) for item, _, sliced, _ in prepared}
    if len(assets) != publication.expected_asset_count or {(a.logical_item_id, a.position_index) for a in assets} != expected:
        _reject("VW1_INCOMPLETE_SLICES")
    by_key = {(a.logical_item_id, a.position_index): a for a in assets}
    program = deepcopy(publication.program_snapshot_json)
    for item in program["items"]:
        result = prepared_items[item["id"]]
        item["duration_ms"] = result.duration_ms
    validate_program_snapshot(program, ready=True)
    loop = sum(item["duration_ms"] for item in program["items"])
    contents = []
    asset_changes = []
    for item, result, sliced, path in prepared:
        content = ContentItem(id=str(uuid4()), client_id=publication.client_id,
            name=f"VW1 {identity[:8]} node{sliced.position_index}",
            type=ContentType.IMAGE if item["type"] == "image" else ContentType.VIDEO,
            duration_seconds=max(1, (result.duration_ms + 999) // 1000),
            metadata_json=dict(content_type=sliced.mime_type, width=sliced.width, height=sliced.height,
                               duration_ms=result.duration_ms, checksum=sliced.sha256, size_bytes=sliced.size_bytes))
        contents.append((content, path))
        asset = by_key[(item["id"], sliced.position_index)]
        probe = dict(fps_num=sliced.fps_num, fps_den=sliced.fps_den, frame_count=sliced.frame_count,
                     has_audio=sliced.has_audio, keyframe_pts_ms=list(sliced.keyframe_pts_ms),
                     source_sha256=result.source_sha256)
        changes = dict(derived_content_id=content.id, duration_ms=result.duration_ms,
                       mime_type=sliced.mime_type, checksum=sliced.sha256,
                       size_bytes=sliced.size_bytes, probe_json=probe, validated=True)
        asset_changes.append((asset, changes))
    values = dict(status="READY", program_snapshot_json=program, loop_duration_ms=loop,
                  lease_token=None, lease_owner=None, lease_expires_at=None, error_code=None, error_message=None)
    with generated_content_references(db, contents) as batch:
        intents = (MutationIntent("UPDATE", RowRef("VideowallPublication", identity), tuple(values.items())),)
        intents += tuple(MutationIntent("UPDATE", RowRef("VideowallPublicationAsset", a.id), tuple(v.items())) for a, v in asset_changes)
        content_fields = ("client_id", "name", "type", "file_path", "duration_seconds", "metadata_json")
        intents += tuple(MutationIntent("CREATE", RowRef("ContentItem", c.id),
            tuple((field, getattr(c, field)) for field in content_fields)) for c in batch.contents)
        with write_guard(db, actor=actor, client_id=publication.client_id, operation=PUBLICATION_OPERATION, intents=intents):
            _require_lease(db, identity, token)
            _current_publication_geometry(db, publication)
            for item in publication.program_snapshot_json["items"]:
                _, master_path = _current_master(db, publication.client_id, item["master_content_id"], expected_type=item["type"])
                if _file_sha256(master_path) != prepared_items[item["id"]].source_sha256:
                    _reject("VW1_MASTER_CHANGED")
            batch.publish()
            for asset, changes in asset_changes:
                for field, value in changes.items():
                    setattr(asset, field, value)
            for field, value in values.items():
                setattr(publication, field, value)
            db.commit()


def fail_publication(db, identity, token, error_code, error_message=None):
    try:
        publication, actor = _require_lease(db, identity, token)
        code = error_code if isinstance(error_code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", error_code) else "VW1_PREPARATION_FAILED"
        # Public status never reflects paths, FFmpeg output or input values.
        values = dict(status="FAILED", lease_token=None, lease_owner=None, lease_expires_at=None,
                      error_code=code, error_message="No se pudo preparar la publicación. Revisa el archivo maestro y la configuración.")
        intent = MutationIntent("UPDATE", RowRef("VideowallPublication", identity), tuple(values.items()))
        with write_guard(db, actor=actor, client_id=publication.client_id, operation=PUBLICATION_OPERATION, intents=(intent,)):
            _require_lease(db, identity, token)
            for field, value in values.items():
                setattr(publication, field, value)
            db.commit()
        return True
    except HTTPException:
        db.rollback()
        return False


def activate_publication(db, wall, identity, actor):
    _authorize(db, actor, wall.client_id)
    publication = db.scalar(select(VideowallPublication).where(
        VideowallPublication.id == identity, VideowallPublication.wall_id == wall.id,
        VideowallPublication.client_id == wall.client_id).execution_options(populate_existing=True))
    if publication is None:
        _reject("Publication not found", 404)
    values = {}
    pointer = {}

    def activation_intents():
        # Called only after the central tenant gate and fresh actor lock. No ORM
        # setters here: the exact timestamp/epoch enter the checked mutation plan.
        db.refresh(wall, with_for_update=True)
        db.refresh(publication, with_for_update=True)
        current_wall, group = _publication_context(db, publication)
        if group is not None:
            db.refresh(group, with_for_update=True)
        selected = group if group is not None else current_wall
        pointer["row"] = selected
        pointer["ref"] = RowRef("VideowallPlaybackGroup" if group is not None else "Videowall", selected.id)
        if group is None and wall.media_delivery != "node_slice":
            _reject("VW1_NODE_SLICE_NOT_ENABLED")
        if publication.status == "ACTIVE" and selected.active_publication_id == identity:
            _current_publication_geometry(db, publication)
            return (MutationIntent("UPDATE", RowRef("VideowallPublication", identity)),
                    MutationIntent("UPDATE", pointer["ref"]))
        if publication.status != "READY" or publication.loop_start_epoch_ms is not None:
            _reject("VW1_PUBLICATION_NOT_READY")
        geometry = _current_publication_geometry(db, publication)
        assets = list(db.scalars(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == identity)))
        items = validate_program_snapshot(publication.program_snapshot_json, ready=True)["items"]
        expected = {(item["id"], node["position_index"]) for item in items for node in geometry["nodes"]}
        if (len(assets) != publication.expected_asset_count
                or {(a.logical_item_id, a.position_index) for a in assets} != expected
                or any(not a.validated or not a.derived_content_id for a in assets)
                or publication.loop_duration_ms != sum(i["duration_ms"] for i in items)):
            _reject("VW1_INCOMPLETE_SLICES")
        for asset in assets:
            _, path = _current_master(db, publication.client_id, asset.derived_content_id)
            if _file_sha256(path) != asset.checksum:
                _reject("VW1_MEDIA_CHANGED")
        activated = now_utc()
        values.update(status="ACTIVE", loop_start_epoch_ms=int(activated.timestamp() * 1000) + ACTIVATION_LEAD_MS,
                      activated_at=activated)
        return (MutationIntent("UPDATE", RowRef("VideowallPublication", identity), tuple(values.items())),
                MutationIntent("UPDATE", pointer["ref"], (("active_publication_id", identity),)))

    with write_guard(db, actor=actor, client_id=wall.client_id, operation="activate_videowall_publication",
                     intent_factory=activation_intents):
        if values:
            for field, value in values.items():
                setattr(publication, field, value)
            pointer["row"].active_publication_id = identity
        db.commit()
        db.refresh(publication)
    return publication
