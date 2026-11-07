"""Relational physical assignments and playback scopes; Player identity is untouched."""
from uuid import uuid4
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select

from app.models.entities import (
    Branch, Campaign, Channel, ChannelMode, Videowall, VideowallNode,
    VideowallPlaybackGroup, VideowallPlaybackGroupMember, VideowallPublication,
)
from app.services.tenant_ownership import RowRef, TenantGraph, context_for_actor, enum_value
from app.services.tenant_mutations import MutationIntent, write_guard
from app.services.tenancy import can_write_client_scope


def _reject(code="VW_GROUP_INVALID_CONFIGURATION", status=409):
    raise HTTPException(status_code=status, detail=code)


def _members(graph, identity):
    return [graph.snapshot(ref) for ref in graph.collection_children(RowRef("VideowallPlaybackGroup", identity), "WRITE")
            if ref.model == "VideowallPlaybackGroupMember" and graph.snapshot(ref)
            and ref not in getattr(graph, "_removed_playback_members", ())]


def group_members_for_graph(graph, group):
    return _members(graph, group["id"])


def group_geometry_snapshot_for_graph(graph, group):
    wall = graph.snapshot(RowRef("Videowall", group["wall_id"]))
    if (not wall or not wall["playback_groups_enabled"] or wall["render_mode"] != "multi-node"
            or wall["client_id"] != group["client_id"]):
        _reject()
    branch = graph.snapshot(RowRef("Branch", group["branch_id"]))
    campaign = graph.snapshot(RowRef("Campaign", group["campaign_id"]))
    if (not branch or branch["client_id"] != group["client_id"] or not branch["is_active"]
            or not campaign or campaign["client_id"] != group["client_id"] or not campaign["is_active"]):
        _reject()
    panel = wall["output_width"], wall["output_height"]
    if panel not in {(1920, 1080), (3840, 2160)}:
        _reject("VW_GROUP_UNSUPPORTED_PANEL", 422)
    members = _members(graph, group["id"])
    if ((group["mode"] == "INDIVIDUAL" and len(members) != 1)
            or (group["mode"] == "SYNCHRONIZED" and len(members) < 2)
            or group["mode"] not in {"INDIVIDUAL", "SYNCHRONIZED"}):
        _reject("VW_GROUP_INVALID_MEMBER_COUNT", 422)
    physical = []
    for member in members:
        node = graph.snapshot(RowRef("VideowallNode", member["node_id"]))
        channel = graph.snapshot(RowRef("Channel", node["channel_id"])) if node else None
        if (not node or not channel or not node["layout_enabled"] or node["videowall_id"] != wall["id"]
                or not 0 <= node["row_index"] < wall["rows"]
                or not 0 <= node["column_index"] < wall["columns"]
                or channel["client_id"] != group["client_id"] or channel["branch_id"] != group["branch_id"]
                or enum_value(channel["mode"]) != "videowall"
                or enum_value(channel["device_mode"]) != "single"
                or (channel["resolution_width"], channel["resolution_height"]) != panel
                or (channel["canvas_width"], channel["canvas_height"]) != panel):
            _reject("VW_GROUP_INVALID_NODE")
        physical.append((member, node))
    min_row = min(n["row_index"] for _, n in physical)
    min_col = min(n["column_index"] for _, n in physical)
    rows = max(n["row_index"] for _, n in physical) - min_row + 1
    columns = max(n["column_index"] for _, n in physical) - min_col + 1
    from app.services.videowall_integrity import qualified_panel_geometry
    if qualified_panel_geometry(columns * panel[0], rows * panel[1], rows, columns) != panel:
        _reject("VW_GROUP_CANVAS_CAPACITY_EXCEEDED", 422)
    cells = {(n["row_index"] - min_row, n["column_index"] - min_col) for _, n in physical}
    if len(cells) != len(members) or cells != {(r, c) for r in range(rows) for c in range(columns)}:
        _reject("VW_GROUP_MUST_BE_CONTIGUOUS_RECTANGLE", 422)
    nodes = []
    for member, node in physical:
        row, col = node["row_index"] - min_row, node["column_index"] - min_col
        position = row * columns + col + 1
        if (member["position_index"], member["row_index"], member["column_index"]) != (position, row, col):
            _reject("VW_GROUP_LOCAL_POSITION_MISMATCH")
        nodes.append(dict(id=node["id"], channel_id=node["channel_id"], position_index=position,
                          row_index=row, column_index=col, crop_x=col * panel[0], crop_y=row * panel[1],
                          crop_width=panel[0], crop_height=panel[1]))
    return dict(rows=rows, columns=columns, total_width=columns * panel[0], total_height=rows * panel[1],
                panel_width=panel[0], panel_height=panel[1], tolerance_ms=wall["start_tolerance_ms"],
                nodes=sorted(nodes, key=lambda n: n["position_index"]))


def build_group_geometry_snapshot(db, group):
    graph = TenantGraph(db)
    row = graph.snapshot(RowRef("VideowallPlaybackGroup", group.id))
    if row is None:
        _reject()
    return group_geometry_snapshot_for_graph(graph, row)


def playback_group_semantics(graph, ref, context, row):
    """Apply to persisted and proposed state; no request metadata can prove ownership."""
    try:
        if ref.model == "VideowallPlaybackGroupMember":
            group = graph.snapshot(RowRef("VideowallPlaybackGroup", row["group_id"]))
            if group is None:
                return False
            group_geometry_snapshot_for_graph(graph, group)
            return True
        if row["mode"] not in {"INDIVIDUAL", "SYNCHRONIZED"} or not row["name"].strip():
            return False
        members = _members(graph, ref.identity)
        # Empty groups are retained history. Their publication/epoch is not reset.
        if members:
            group_geometry_snapshot_for_graph(graph, row)
        return True
    except (HTTPException, KeyError, TypeError, ValueError):
        return False


def read_group(db, group):
    graph = TenantGraph(db)
    invalid = graph.invalid_relations(RowRef("VideowallPlaybackGroup", group.id),
                                      profile="READ", context=SimpleNamespace(branch_id=None))
    if any(edge.read_policy != "FOREIGN_FIELD_NEUTRALIZED" for edge in invalid):
        _reject("Playback group not found", 404)
    members = list(db.scalars(select(VideowallPlaybackGroupMember).where(
        VideowallPlaybackGroupMember.group_id == group.id).order_by(VideowallPlaybackGroupMember.position_index)))
    wall = db.get(Videowall, group.wall_id)
    geometry = build_group_geometry_snapshot(db, group) if members else None
    values = {key: getattr(group, key) for key in (
        "id", "client_id", "branch_id", "wall_id", "name", "mode", "campaign_id", "active_publication_id")}
    for edge in invalid:
        values[edge.field] = None
    values.update(rows=geometry["rows"] if geometry else 0, columns=geometry["columns"] if geometry else 0,
                  total_width=geometry["total_width"] if geometry else 0,
                  total_height=geometry["total_height"] if geometry else 0,
                  output_width=wall.output_width, output_height=wall.output_height, members=[])
    for member in members:
        node = db.get(VideowallNode, member.node_id)
        values["members"].append(dict(id=member.id, group_id=group.id, node_id=node.id, channel_id=node.channel_id,
            position_index=member.position_index, row_index=member.row_index, column_index=member.column_index,
            physical_position_index=node.position_index, physical_row_index=node.row_index,
            physical_column_index=node.column_index))
    return values


def _authorize(db, actor, wall):
    context_for_actor(db, actor, client_id=wall.client_id)
    if not can_write_client_scope(actor):
        _reject("Playback configuration unavailable", 403)


def replace_playback_groups(db, wall, payload, actor):
    _authorize(db, actor, wall)
    groups = list(db.scalars(select(VideowallPlaybackGroup).where(VideowallPlaybackGroup.wall_id == wall.id)))
    by_id = {g.id: g for g in groups}
    old_members = list(db.scalars(select(VideowallPlaybackGroupMember).where(
        VideowallPlaybackGroupMember.group_id.in_(by_id))))
    nodes = {n.id: n for n in db.scalars(select(VideowallNode).where(VideowallNode.videowall_id == wall.id))}
    used, used_groups, changes, new_members = set(), set(), [], []
    for item in payload.groups:
        identity = item.id or str(uuid4())
        if identity in used_groups or (item.id and item.id not in by_id):
            _reject("VW_GROUP_UNKNOWN_OR_DUPLICATE")
        used_groups.add(identity)
        if len(set(item.node_ids)) != len(item.node_ids) or used.intersection(item.node_ids):
            _reject("VW_GROUP_OVERLAPPING_MEMBERS", 422)
        used.update(item.node_ids)
        selected = [nodes.get(identity) for identity in item.node_ids]
        if any(n is None or not n.layout_enabled for n in selected):
            _reject("VW_GROUP_NODE_NOT_ASSIGNED", 422)
        branches = {db.get(Channel, n.channel_id).branch_id for n in selected}
        if len(branches) != 1:
            _reject("VW_GROUP_BRANCH_MISMATCH")
        values = dict(client_id=wall.client_id, branch_id=next(iter(branches)), wall_id=wall.id,
                      name=item.name.strip(), mode=item.mode, campaign_id=item.campaign_id)
        existing = by_id.get(identity)
        if existing and (existing.mode != item.mode or existing.campaign_id != item.campaign_id):
            values["active_publication_id"] = None
        changes.append((identity, existing, values))
        min_row, min_col = min(n.row_index for n in selected), min(n.column_index for n in selected)
        cols = max(n.column_index for n in selected) - min_col + 1
        for node in selected:
            row, col = node.row_index - min_row, node.column_index - min_col
            new_members.append(VideowallPlaybackGroupMember(id=str(uuid4()), group_id=identity, node_id=node.id,
                                position_index=row * cols + col + 1, row_index=row, column_index=col))
    intents = [MutationIntent("UPDATE", RowRef("Videowall", wall.id), (("playback_groups_enabled", True),
                                                                                 ("active_publication_id", None)))]
    intents += [MutationIntent("DELETE", RowRef("VideowallPlaybackGroupMember", m.id)) for m in old_members]
    intents += [MutationIntent("UPDATE" if existing else "CREATE", RowRef("VideowallPlaybackGroup", identity), tuple(v.items()))
                for identity, existing, v in changes]
    fields = ("group_id", "node_id", "position_index", "row_index", "column_index")
    intents += [MutationIntent("CREATE", RowRef("VideowallPlaybackGroupMember", m.id),
                              tuple((key, getattr(m, key)) for key in fields)) for m in new_members]
    with write_guard(db, actor=actor, client_id=wall.client_id, operation="update_playback_groups", intents=tuple(intents)):
        current_group_ids = set(db.scalars(select(VideowallPlaybackGroup.id).where(VideowallPlaybackGroup.wall_id == wall.id)))
        current_member_ids = set(db.scalars(select(VideowallPlaybackGroupMember.id).where(
            VideowallPlaybackGroupMember.group_id.in_(current_group_ids))))
        if current_group_ids != set(by_id) or current_member_ids != {m.id for m in old_members}:
            _reject("VW_GROUP_CONFIGURATION_CHANGED")
        for member in old_members:
            db.delete(member)
        db.flush()  # free unique current membership before inserting its replacement
        for identity, existing, values in changes:
            if existing is None:
                db.add(VideowallPlaybackGroup(id=identity, **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
        wall.playback_groups_enabled = True
        wall.active_publication_id = None
        db.add_all(new_members)
        db.commit()
    return [read_group(db, group) for group in db.scalars(select(VideowallPlaybackGroup).where(
        VideowallPlaybackGroup.wall_id == wall.id).order_by(VideowallPlaybackGroup.created_at, VideowallPlaybackGroup.id))]


def update_physical_layout(db, wall, payload, actor):
    _authorize(db, actor, wall)
    if wall.render_mode != "multi-node" or payload.total_width != payload.columns * payload.output_width or payload.total_height != payload.rows * payload.output_height:
        _reject("VW_PHYSICAL_GEOMETRY_MISMATCH", 422)
    if (payload.output_width, payload.output_height) != (wall.output_width, wall.output_height):
        _reject("VW_PHYSICAL_PANEL_CHANGE_REQUIRES_EXPLICIT_SCREEN_CONFIGURATION", 422)
    nodes = list(db.scalars(select(VideowallNode).where(VideowallNode.videowall_id == wall.id)))
    selected, cells = {}, set()
    for item in payload.nodes:
        cell = item.row_index, item.column_index
        if (item.node_id in selected or cell in cells or item.row_index >= payload.rows or item.column_index >= payload.columns
                or item.node_id not in {n.id for n in nodes}):
            _reject("VW_PHYSICAL_INVALID_ASSIGNMENT", 422)
        selected[item.node_id] = item
        cells.add(cell)
    values = {key: getattr(payload, key) for key in ("rows", "columns", "total_width", "total_height", "output_width", "output_height")}
    values.update(playback_groups_enabled=True, active_publication_id=None)
    updates = []
    for node in nodes:
        item = selected.get(node.id)
        patch = dict(layout_enabled=item is not None)
        if item:
            patch.update(row_index=item.row_index, column_index=item.column_index,
                         position_index=item.row_index * payload.columns + item.column_index + 1,
                         x=item.column_index * payload.output_width, y=item.row_index * payload.output_height,
                         width=payload.output_width, height=payload.output_height)
        updates.append((node, patch))
    intents = (MutationIntent("UPDATE", RowRef("Videowall", wall.id), tuple(values.items())),)
    intents += tuple(MutationIntent("UPDATE", RowRef("VideowallNode", n.id), tuple(v.items())) for n, v in updates)
    with write_guard(db, actor=actor, client_id=wall.client_id, operation="update_physical_videowall_layout", intents=intents):
        if set(db.scalars(select(VideowallNode.id).where(VideowallNode.videowall_id == wall.id))) != {n.id for n in nodes}:
            _reject("VW_PHYSICAL_CONFIGURATION_CHANGED")
        for key, value in values.items():
            setattr(wall, key, value)
        for node, patch in updates:
            for key, value in patch.items():
                setattr(node, key, value)
        db.commit()
        db.refresh(wall)
    return wall


def resolve_channel_playback_group(db, channel):
    """None=legacy; (wall,None)=explicit standalone unassigned; invalid=>409."""
    memberships = list(db.execute(select(VideowallNode, Videowall).join(
        Videowall, Videowall.id == VideowallNode.videowall_id).where(VideowallNode.channel_id == channel.id)))
    opted = [(node, wall) for node, wall in memberships if wall.playback_groups_enabled]
    if not opted:
        return None
    if len(memberships) != 1 or len(opted) != 1:
        _reject()
    node, wall = opted[0]
    if wall.client_id != channel.client_id:
        _reject()
    member = db.scalar(select(VideowallPlaybackGroupMember).where(VideowallPlaybackGroupMember.node_id == node.id))
    if not node.layout_enabled:
        if member:
            _reject()
        return wall, None
    if member is None:
        return wall, None
    group = db.get(VideowallPlaybackGroup, member.group_id)
    if not group or group.wall_id != wall.id or group.client_id != channel.client_id or group.branch_id != channel.branch_id:
        _reject()
    build_group_geometry_snapshot(db, group)
    return wall, group
