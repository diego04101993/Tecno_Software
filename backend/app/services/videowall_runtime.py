"""Opt-in node slices. A missing/unsafe publication never falls back to masters."""
from datetime import datetime
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from app.core.public_urls import build_absolute_public_url
from app.models.entities import (
    Branch, Client, ContentItem, Videowall, VideowallNode,
    VideowallPublication, VideowallPublicationAsset,
)
from app.services.media_retention import incomplete_retention
from app.services.videowall_integrity import publication_read_safe
from app.services.videowall_publications import (
    _current_master, build_geometry_snapshot, geometry_signature,
)


def timeline_position(server_now_ms, epoch_ms, durations_ms):
    """Canonical modulo math only; before epoch the Player must still wait.

    Returning the mathematical position does not authorize playback before the
    persisted start. Android owns that scheduling decision, not this helper.
    """
    if (type(server_now_ms) is not int or type(epoch_ms) is not int
            or not durations_ms or any(type(v) is not int or v <= 0 for v in durations_ms)):
        raise ValueError("Invalid timeline")
    offset = (server_now_ms - epoch_ms) % sum(durations_ms)
    for index, duration in enumerate(durations_ms):
        if offset < duration:
            return {"item_index": index, "item_offset_ms": offset}
        offset -= duration
    raise AssertionError("Positive modulo must select an item")


def _asset_content(db, asset, client_id, base_url):
    row, path = _current_master(db, client_id, asset.derived_content_id)
    if path.stat().st_size != asset.size_bytes:
        raise ValueError("Derived media size changed")
    # Explicit columns only: master IDs, arbitrary metadata and stored probe data
    # are not a public playable-content contract.
    return {
        "id": row["id"], "type": row["type"].value,
        "file_path": row["file_path"],
        "download_url": build_absolute_public_url(base_url, row["file_path"]),
        "mime_type": asset.mime_type, "width": asset.width, "height": asset.height,
        "duration_seconds": asset.duration_ms / 1000,
        "duration_ms": asset.duration_ms, "checksum": asset.checksum, "size": asset.size_bytes,
    }


def _retention(db, channel, wall, node_id, base_url, generated_at, coherent):
    if not coherent:
        return incomplete_retention(channel.id, generated_at, "snapshot_unavailable")
    assets, campaigns, publications = {}, set(), []
    try:
        candidates = db.scalars(select(VideowallPublication).where(
            VideowallPublication.wall_id == wall.id,
            VideowallPublication.status.in_(("PROCESSING", "READY", "ACTIVE"))))
        for publication in candidates:
            if (publication.client_id != channel.client_id or publication.branch_id != channel.branch_id
                    or not publication_read_safe(db, publication.id, profile="RUNTIME")):
                raise ValueError("Unsafe retained publication")
            campaigns.add(publication.campaign_id)
            publications.append(publication.id)
            for asset in db.scalars(select(VideowallPublicationAsset).where(
                    VideowallPublicationAsset.publication_id == publication.id,
                    VideowallPublicationAsset.node_id == node_id,
                    VideowallPublicationAsset.validated.is_(True))):
                content = _asset_content(db, asset, channel.client_id, base_url)
                assets[content["id"]] = {key: content[key] for key in ("file_path", "download_url")}
                assets[content["id"]]["content_id"] = content["id"]
        ordered = [assets[key] for key in sorted(assets)]
        canonical = dict(channel_id=channel.id, publications=sorted(publications),
                         protected_campaign_ids=sorted(campaigns), assets=ordered)
        revision = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return dict(schema_version=1, complete=True, channel_id=channel.id, revision=revision,
                    generated_at=generated_at.isoformat().replace("+00:00", "Z"),
                    protected_campaign_ids=sorted(campaigns), assets=ordered)
    except (HTTPException, ValueError, TypeError, KeyError, OSError):
        return incomplete_retention(channel.id, generated_at, "unsafe_publication")


def build_node_slice_runtime(channel, db, base_url, generated_at: datetime, *, snapshot_coherent=False, playback_context=None):
    """None means legacy. Any raw node_slice membership opts into fail-closed.

    Detect before tenant read filtering: a corrupt foreign node must never turn
    this request into a legacy master playlist through an omitted SQL row.
    """
    from app.services.player_runtime import (
        serialize_channel, serialize_branch, serialize_client, _fallback_payload,
        _resolve_output_mapping, _runtime_output_allowed,
    )
    from app.services.device_outputs import serialize_runtime_output
    memberships = list(db.execute(select(VideowallNode, Videowall).join(
        Videowall, Videowall.id == VideowallNode.videowall_id).where(
        VideowallNode.channel_id == channel.id)).all())
    from app.services.videowall_playback_groups import resolve_channel_playback_group, build_group_geometry_snapshot
    playback_context = playback_context or resolve_channel_playback_group(db, channel)
    group = playback_context[1] if playback_context else None
    if playback_context and (group is None or group.mode == "INDIVIDUAL"):
        return None
    primary_slice_wall = db.scalar(select(Videowall.id).where(
        Videowall.primary_channel_id == channel.id, Videowall.media_delivery == "node_slice"))
    if not group and not primary_slice_wall and not any(wall.media_delivery == "node_slice" for _, wall in memberships):
        return None
    client, branch = db.get(Client, channel.client_id), db.get(Branch, channel.branch_id)
    wall_payload = dict(enabled=True, media_delivery="node_slice")
    payload = dict(
        runtime_version=3, runtime_generated_at=generated_at.isoformat(), server_time=generated_at.isoformat(),
        channel=serialize_channel(channel),
        device=dict(mode="single", canvas_width=channel.resolution_width,
                    canvas_height=channel.resolution_height, expected_outputs=1, root_channel_id=channel.id),
        branch=serialize_branch(branch), client=serialize_client(client),
        mode="videowall", playback_mode="sequential", schedule=None, campaigns=[],
        sequence_items=[], playlist=[], contents=[], layout=None, layout_runtime_snapshot=None,
        dataset_runtime_data=None, videowall=wall_payload, outputs=[],
        output_mapping=_resolve_output_mapping(channel), audio=None, touch=None,
        fallback=_fallback_payload("node_slice_publication_unavailable"),
        media_retention=incomplete_retention(channel.id, generated_at, "unsafe_publication"),
    )
    try:
        if len(memberships) != 1 or primary_slice_wall:
            return payload
        node, wall = memberships[0]
        if wall.client_id != channel.client_id or channel.device_mode.value != "single":
            return payload
        wall_payload.update(wall_id=wall.id, videowall_id=wall.id)
        active_id = group.active_publication_id if group else wall.active_publication_id
        publication = db.get(VideowallPublication, active_id) if active_id else None
        if (publication is None or publication.status != "ACTIVE" or publication.wall_id != wall.id
                or publication.client_id != channel.client_id or publication.branch_id != channel.branch_id
                or not publication_read_safe(db, publication.id, profile="RUNTIME")):
            return payload
        if publication.playback_group_id != (group.id if group else None):
            return payload
        geometry = build_group_geometry_snapshot(db, group) if group else build_geometry_snapshot(db, wall)
        if geometry_signature(geometry) != publication.geometry_signature:
            return payload
        local = next(n for n in geometry["nodes"] if n["id"] == node.id and n["channel_id"] == channel.id)
        all_assets = list(db.scalars(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == publication.id)))
        # Prove the entire effective group, not only this node, before publishing any
        # part of the shared program. No lazy or partial activation is possible.
        descriptors = {asset.id: _asset_content(db, asset, channel.client_id, base_url) for asset in all_assets}
        own = {asset.logical_item_id: asset for asset in all_assets if asset.node_id == node.id}
        playlist, sequence, contents = [], [], []
        for item in publication.program_snapshot_json["items"]:
            asset = own[item["id"]]
            content = descriptors[asset.id]
            entry = dict(id=item["id"], campaign_id=publication.campaign_id,
                         sort_order=item["sort_order"], duration_seconds=item["duration_ms"] / 1000,
                         duration_ms=item["duration_ms"], content_id=asset.derived_content_id, content=content)
            playlist.append(entry)
            sequence.append(dict(entry, item_type="content", layout_id=None, options_json={}, is_enabled=True, layout=None))
            contents.append(content)
        outputs = [output for output in channel.device_outputs if output.enabled
                   and output.root_channel_id == channel.id and output.channel_id == channel.id
                   and _runtime_output_allowed(db, output.channel_id, channel.client_id)]
        if len(outputs) != 1:
            return payload
        output = serialize_runtime_output(outputs[0])
        if (output["geometry"] != dict(x=0, y=0, width=local["crop_width"], height=local["crop_height"])
                or output["source"] != dict(x=0, y=0, width=local["crop_width"], height=local["crop_height"])):
            return payload
        wall_payload.update(
            publication_id=publication.id, rows=geometry["rows"], columns=geometry["columns"],
            total_width=geometry["total_width"], total_height=geometry["total_height"],
            sync_group=group.id if group else wall.id, loop_duration_ms=publication.loop_duration_ms,
            sync=dict(mode="epoch_timeline_v1", loop_start_epoch_ms=publication.loop_start_epoch_ms,
                      tolerance_ms=geometry["tolerance_ms"]),
            node={key: local[key] for key in ("position_index", "row_index", "column_index",
                                            "crop_x", "crop_y", "crop_width", "crop_height")})
        if group:
            wall_payload.update(playback_group_id=group.id, physical_node=dict(position_index=node.position_index,
                row_index=node.row_index, column_index=node.column_index))
        payload.update(
            campaigns=[dict(campaign_id=publication.campaign_id,
                            source="videowall_publication", playlist_items_count=len(playlist))],
            playlist=playlist, sequence_items=sequence, contents=contents, outputs=[output], fallback=None,
            media_retention=_retention(db, channel, wall, node.id, base_url, generated_at, snapshot_coherent))
        return payload
    except (HTTPException, ValueError, KeyError, TypeError, StopIteration, OSError):
        return payload
