"""Typed publication snapshots; JSON never establishes tenant or media ownership."""
from collections.abc import Mapping
import re


MAX_GROUP_CANVAS_PIXELS = 11520 * 2160


def qualified_panel_geometry(canvas_width, canvas_height, rows, columns):
    """Qualified V1 processing geometry; no coercion, remainder or resizing."""
    if any(type(value) is not int or value <= 0
           for value in (canvas_width, canvas_height, rows, columns)):
        return None
    if (canvas_width * canvas_height > MAX_GROUP_CANVAS_PIXELS
            or canvas_width % columns or canvas_height % rows):
        return None
    panel = canvas_width // columns, canvas_height // rows
    return panel if panel in {(1920, 1080), (3840, 2160)} else None


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def valid_geometry(value):
    if not isinstance(value, Mapping) or set(value) != {
        "rows", "columns", "total_width", "total_height", "panel_width",
        "panel_height", "tolerance_ms", "nodes"
    }:
        return False
    panel = qualified_panel_geometry(value["total_width"], value["total_height"],
                                     value["rows"], value["columns"])
    if (panel is None or any(type(value[key]) is not int for key in ("panel_width", "panel_height"))
            or panel != (value["panel_width"], value["panel_height"])):
        return False
    if not _integer(value["tolerance_ms"], 1) or value["tolerance_ms"] > 60000:
        return False
    nodes = value["nodes"]
    count = value["rows"] * value["columns"]
    if not isinstance(nodes, (list, tuple)) or len(nodes) != count:
        return False
    ids, channels, positions = set(), set(), set()
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) != {
            "id", "channel_id", "position_index", "row_index", "column_index",
            "crop_x", "crop_y", "crop_width", "crop_height"
        }:
            return False
        pos = node["position_index"]
        if not _integer(pos, 1) or pos > count:
            return False
        if (not isinstance(node["id"], str) or not node["id"]
                or not isinstance(node["channel_id"], str) or not node["channel_id"]):
            return False
        row, column = divmod(pos - 1, value["columns"])
        if (any(type(node[key]) is not int for key in ("row_index", "column_index", "crop_x", "crop_y", "crop_width", "crop_height"))
                or (node["row_index"], node["column_index"], node["crop_x"], node["crop_y"],
                    node["crop_width"], node["crop_height"])
                   != (row, column, column * panel[0], row * panel[1], *panel)):
            return False
        ids.add(node["id"]); channels.add(node["channel_id"]); positions.add(pos)
    return len(ids) == len(channels) == count and positions == set(range(1, count + 1))


def _live_geometry_matches(graph, wall, geometry, playback_group=None):
    if playback_group is not None:
        from app.services.videowall_playback_groups import group_geometry_snapshot_for_graph
        from app.services.videowall_publications import geometry_signature
        return geometry_signature(group_geometry_snapshot_for_graph(graph, playback_group)) == geometry_signature(geometry)
    from sqlalchemy import select
    from app.models.entities import VideowallNode
    from app.services.tenant_ownership import RowRef, enum_value
    if any(wall.get(k) != geometry[k] for k in ("rows", "columns", "total_width", "total_height")):
        return False
    if wall.get("start_tolerance_ms") != geometry["tolerance_ms"]:
        return False
    ids = set(graph.session.scalars(select(VideowallNode.id).where(VideowallNode.videowall_id == wall["id"])))
    for ref, values in getattr(graph, "_proposals", {}).items():
        if ref.model == "VideowallNode":
            ids.discard(ref.identity)
            if values.get("videowall_id") == wall["id"]:
                ids.add(ref.identity)
    if ids != {n["id"] for n in geometry["nodes"]}:
        return False
    for node in geometry["nodes"]:
        live = graph.snapshot(RowRef("VideowallNode", node["id"]))
        channel = graph.snapshot(RowRef("Channel", node["channel_id"]))
        if (live is None or channel is None or live["channel_id"] != node["channel_id"]
                or any(live.get(k) != node[k] for k in ("position_index", "row_index", "column_index"))
                or enum_value(channel["mode"]) != "videowall"
                or (channel["resolution_width"], channel["resolution_height"])
                   != (geometry["panel_width"], geometry["panel_height"])
                or (channel["canvas_width"], channel["canvas_height"])
                   != (geometry["panel_width"], geometry["panel_height"])):
            return False
    return True


def publication_semantics(graph, ref, context, row):
    from app.services.tenant_ownership import RowRef, enum_value
    from app.services.videowall_publications import geometry_signature, validate_program_snapshot
    from app.services.media_references import media_owned_by
    from app.services.media_storage import resolve_stored_media_path, MediaPathError
    from app.core.config import get_settings
    from fastapi import HTTPException
    try:
        if row["status"] not in {"PENDING", "PROCESSING", "READY", "ACTIVE", "FAILED"}:
            return False
        program, geometry = row["program_snapshot_json"], row["geometry_snapshot_json"]
        group_id = row.get("playback_group_id")
        ready = row["status"] in {"READY", "ACTIVE"}
        validate_program_snapshot(program, ready=ready)
        if (not valid_geometry(geometry) or geometry_signature(geometry) != row["geometry_signature"]
                or program["campaign_id"] != row["campaign_id"]
                or row["encoding_profile"] != "code302_h264_cfr_v1"
                or (group_id is None and (geometry["rows"], geometry["columns"]) != (1, 3))
                or row["expected_asset_count"] != len(program["items"]) * len(geometry["nodes"])):
            return False
        if ready and row["loop_duration_ms"] != sum(i["duration_ms"] for i in program["items"]):
            return False
        if row["status"] == "ACTIVE" and (not _integer(row["loop_start_epoch_ms"])
                                           or row["activated_at"] is None):
            return False
        assets = [graph.snapshot(child) for child in graph.collection_children(ref, "WRITE")
                  if child.model == "VideowallPublicationAsset"]
        expected = {(item["id"], node["position_index"]) for item in program["items"] for node in geometry["nodes"]}
        if (len(assets) != row["expected_asset_count"] or any(asset is None for asset in assets)
                or {(asset["logical_item_id"], asset["position_index"]) for asset in assets} != expected
                or (ready and any(asset["validated"] is not True for asset in assets))):
            return False
        owner = graph.resolve_owner(ref)
        wall = graph.snapshot(RowRef("Videowall", row["wall_id"]))
        if wall is None or wall["client_id"] != owner.client_id:
            return False
        if group_id is not None:
            from app.services.videowall_playback_groups import group_members_for_graph
            if not isinstance(group_id, str) or not group_id:
                return False
            group = graph.snapshot(RowRef("VideowallPlaybackGroup", group_id))
            if (group is None or group["client_id"] != owner.client_id
                    or group["wall_id"] != row["wall_id"] or group["branch_id"] != row["branch_id"]):
                return False
            # An empty/reconfigured group is retained history. Only its current
            # nonempty playback scope must match the frozen publication geometry.
            if (wall.get("playback_groups_enabled") is True
                    and group["active_publication_id"] == ref.identity
                    and group_members_for_graph(graph, group)):
                if (group["mode"] != "SYNCHRONIZED" or group["campaign_id"] != row["campaign_id"]
                        or not _live_geometry_matches(graph, wall, geometry, group)):
                    return False
        elif (not wall.get("playback_groups_enabled") and wall["media_delivery"] == "node_slice"
              and wall["active_publication_id"] == ref.identity):
            if not _live_geometry_matches(graph, wall, geometry):
                return False
        for node in geometry["nodes"]:
            live = graph.snapshot(RowRef("VideowallNode", node["id"]))
            channel = graph.snapshot(RowRef("Channel", node["channel_id"]))
            if (live is None or channel is None or live["videowall_id"] != row["wall_id"]
                    or live["channel_id"] != node["channel_id"]
                    or channel["client_id"] != owner.client_id or channel["branch_id"] != row["branch_id"]):
                return False
        root = get_settings().MEDIA_ROOT
        base = getattr(graph, "_persisted", graph)
        for item in program["items"]:
            master = RowRef("ContentItem", item["master_content_id"])
            data = graph.snapshot(master)
            if (data is None or graph.resolve_owner(master).client_id != owner.client_id
                    or enum_value(data["type"]) != item["type"] or not data["file_path"]):
                return False
            path = resolve_stored_media_path(root, data["file_path"], must_exist=False)
            if path is None:
                return False
            base.media_paths.add(path)
            if base.media_validation_locked and not media_owned_by(graph.session, root, path, owner.client_id):
                return False
        return True
    except (KeyError, TypeError, ValueError, HTTPException, MediaPathError):
        return False


def asset_semantics(graph, ref, context, row):
    from app.services.tenant_ownership import RowRef, enum_value
    pub = graph.snapshot(RowRef("VideowallPublication", row["publication_id"]))
    if pub is None:
        return False
    try:
        geometry, program = pub["geometry_snapshot_json"], pub["program_snapshot_json"]
        node = next(n for n in geometry["nodes"] if n["id"] == row["node_id"])
        item = next(i for i in program["items"] if i["id"] == row["logical_item_id"])
        if (row["master_content_id"] != item["master_content_id"]
                or row["position_index"] != node["position_index"]
                or row["sort_order"] != item["sort_order"]
                or (row["width"], row["height"]) != (node["crop_width"], node["crop_height"])
                or type(row["validated"]) is not bool):
            return False
        if not row["validated"]:
            return pub["status"] not in {"READY", "ACTIVE"} and all(row.get(k) is None for k in (
                "derived_content_id", "duration_ms", "mime_type", "checksum", "size_bytes", "probe_json"))
        if (pub["status"] not in {"READY", "ACTIVE"} or row["duration_ms"] != item["duration_ms"]
                or not _integer(row["duration_ms"], 1) or not _integer(row["size_bytes"], 1)
                or not isinstance(row["checksum"], str) or not re.fullmatch("[0-9a-f]{64}", row["checksum"])
                or row["derived_content_id"] == row["master_content_id"]):
            return False
        derived = graph.snapshot(RowRef("ContentItem", row["derived_content_id"]))
        if derived is None or enum_value(derived["type"]) != item["type"]:
            return False
        if row["mime_type"] != {"image": "image/png", "video": "video/mp4"}[item["type"]]:
            return False
        probe = row["probe_json"]
        if not isinstance(probe, Mapping) or set(probe) != {
            "fps_num", "fps_den", "frame_count", "has_audio", "keyframe_pts_ms", "source_sha256"
        }:
            return False
        if (type(probe["has_audio"]) is not bool or (row["position_index"] != 1 and probe["has_audio"])
                or not isinstance(probe["source_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", probe["source_sha256"])
                or not isinstance(probe["keyframe_pts_ms"], (list, tuple))
                or any(not _integer(p) for p in probe["keyframe_pts_ms"])):
            return False
        if item["type"] == "video" and not all(_integer(probe[k], 1) for k in ("fps_num", "fps_den", "frame_count")):
            return False
        return True
    except (KeyError, StopIteration, TypeError, ValueError):
        return False


def publication_read_safe(db, identity, *, profile="READ"):
    """Prove all typed snapshots and every asset, without granting authorization.

    Used in the eligible-ID set before pagination and by the MVCC runtime. No
    writes, repairs or request metadata can establish media ownership here.
    """
    from types import SimpleNamespace
    from app.services.tenant_ownership import TenantGraph, RowRef, ContextIntegrityState
    from fastapi import HTTPException
    graph = TenantGraph(db)
    graph.media_paths = set()
    graph.media_validation_locked = True
    ref = RowRef("VideowallPublication", identity)
    context = SimpleNamespace(branch_id=None)
    try:
        proof = graph.context_integrity(ref, context, profile=profile)
        if proof.state != ContextIntegrityState.CONSISTENT:
            return False
        from app.services.media_references import readable_media_ids
        from app.services.runtime_read import runtime_reads
        content_keys = None
        if runtime_reads(db) is not None:
            content_keys = {(graph.snapshot(checked)["client_id"], checked.identity)
                            for checked in proof.checked if checked.model == "ContentItem"}
        allowed_media = set(readable_media_ids(db, "ContentItem", content_keys=content_keys))
        for checked in proof.checked:
            if checked.model == "ContentItem" and checked.identity not in allowed_media:
                return False
            if checked.model == "VideowallPublication":
                if not publication_semantics(graph, checked, context, graph.snapshot(checked)):
                    return False
            elif checked.model == "VideowallPublicationAsset":
                if not asset_semantics(graph, checked, context, graph.snapshot(checked)):
                    return False
        return True
    except (HTTPException, KeyError, TypeError, ValueError):
        return False


def readable_publication_ids(db, name, *, profile="READ"):
    from sqlalchemy import select
    from app.models.entities import VideowallPublication, VideowallPublicationAsset
    publications = [identity for identity in db.scalars(select(VideowallPublication.id))
                    if publication_read_safe(db, identity, profile=profile)]
    if name == "VideowallPublication":
        return publications
    return list(db.scalars(select(VideowallPublicationAsset.id).where(
        VideowallPublicationAsset.publication_id.in_(publications))))
