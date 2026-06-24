from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.routes.channels import cleanup_channel_delete_dependencies, get_channel_delete_block_message, join_human_list
from app.models.entities import Channel, ChannelMode, User, Videowall, VideowallNode
from app.schemas.domain import (
    VideowallCreate,
    VideowallNodeCreate,
    VideowallNodeRead,
    VideowallNodeUpdate,
    VideowallRead,
    VideowallUpdate,
)
from app.services.presence import is_player_online
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    branch_videowall_ids_query,
    can_write_client_scope,
    get_branch_in_scope,
    get_channel_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)
from app.services.videowall_geometry import (
    VideowallCellGeometry,
    compute_videowall_cell_by_row_col,
    position_from_row_col,
    row_col_from_position,
)


router = APIRouter()


def build_videowall_delete_block_message(videowall: Videowall, blocked_nodes: list[str]) -> str:
    return f"No se puede eliminar el videowall {videowall.name} porque {join_human_list(blocked_nodes)}."


def get_videowall_in_scope(videowall_id: str, db: Session, current_user: User) -> Videowall:
    videowall = db.get(Videowall, videowall_id)
    if not videowall:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Videowall not found")

    require_client_scope(current_user, videowall.client_id)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        visible_videowall = db.scalar(
            select(Videowall.id).where(
                Videowall.id == videowall_id,
                Videowall.id.in_(branch_videowall_ids_query(branch_id)),
            )
        )
        if not visible_videowall:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Videowall not found")

    return videowall


def _require_positive_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} debe ser mayor que cero")
    return value


def _validate_videowall_geometry(columns: int, rows: int, total_width: int, total_height: int) -> tuple[int, int, int, int]:
    safe_columns = _require_positive_int(columns, "columns")
    safe_rows = _require_positive_int(rows, "rows")
    safe_total_width = _require_positive_int(total_width, "total_width")
    safe_total_height = _require_positive_int(total_height, "total_height")
    return safe_columns, safe_rows, safe_total_width, safe_total_height


def _resolve_videowall_branch_id(videowall_id: str, db: Session) -> str | None:
    return db.scalar(
        select(Channel.branch_id)
        .join(VideowallNode, VideowallNode.channel_id == Channel.id)
        .where(VideowallNode.videowall_id == videowall_id)
        .order_by(VideowallNode.created_at.asc())
        .limit(1)
    )


def _resolve_node_geometry(
    videowall: Videowall,
    *,
    position_index: int | None,
    row_index: int | None,
    column_index: int | None,
    fallback_position_index: int | None = None,
    fallback_row_index: int | None = None,
    fallback_column_index: int | None = None,
) -> VideowallCellGeometry:
    columns, rows, total_width, total_height = _validate_videowall_geometry(
        videowall.columns,
        videowall.rows,
        videowall.total_width,
        videowall.total_height,
    )
    max_position = columns * rows

    resolved_position = position_index if position_index is not None else fallback_position_index
    resolved_row = row_index if row_index is not None else fallback_row_index
    resolved_column = column_index if column_index is not None else fallback_column_index

    if resolved_row is not None and resolved_column is not None:
        if resolved_row < 0 or resolved_row >= rows:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"node_row fuera de rango para esta matriz ({rows} fila(s))")
        if resolved_column < 0 or resolved_column >= columns:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"node_col fuera de rango para esta matriz ({columns} columna(s))")

        computed_position = position_from_row_col(columns, resolved_row, resolved_column)
        if resolved_position is not None and resolved_position != computed_position:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La posición solicitada no coincide con node_row/node_col",
            )
        return compute_videowall_cell_by_row_col(
            columns=columns,
            rows=rows,
            total_width=total_width,
            total_height=total_height,
            row_index=resolved_row,
            column_index=resolved_column,
        )

    if resolved_position is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes enviar position_index o el par node_row/node_col para ubicar el nodo",
        )

    if resolved_position < 1 or resolved_position > max_position:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"position_index fuera de rango para esta matriz ({max_position} posición(es))",
        )

    derived_row, derived_column = row_col_from_position(columns, resolved_position)
    return compute_videowall_cell_by_row_col(
        columns=columns,
        rows=rows,
        total_width=total_width,
        total_height=total_height,
        row_index=derived_row,
        column_index=derived_column,
    )


def _ensure_node_slot_available(
    videowall_id: str,
    geometry: VideowallCellGeometry,
    db: Session,
    *,
    exclude_node_id: str | None = None,
) -> None:
    query = select(VideowallNode.id).where(
        VideowallNode.videowall_id == videowall_id,
        VideowallNode.row_index == geometry.row_index,
        VideowallNode.column_index == geometry.column_index,
    )
    if exclude_node_id:
        query = query.where(VideowallNode.id != exclude_node_id)

    if db.scalar(query.limit(1)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un nodo en fila {geometry.row_index + 1}, columna {geometry.column_index + 1}",
        )


def _apply_geometry_to_node(node: VideowallNode, geometry: VideowallCellGeometry) -> None:
    node.position_index = geometry.position_index
    node.row_index = geometry.row_index
    node.column_index = geometry.column_index
    node.x = geometry.x
    node.y = geometry.y
    node.width = geometry.width
    node.height = geometry.height


@router.get("/", response_model=list[VideowallRead])
def list_videowalls(
    client_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Videowall]:
    query = apply_client_filter(select(Videowall).order_by(Videowall.created_at.desc()), Videowall, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Videowall.client_id == target_client_id)

    if branch_id:
        branch = get_branch_in_scope(db, branch_id, current_user)
        if target_client_id and branch.client_id != target_client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")
        query = query.where(Videowall.id.in_(branch_videowall_ids_query(branch.id)))

    if is_branch_scoped(current_user):
        query = query.where(Videowall.id.in_(branch_videowall_ids_query(require_branch_assignment(current_user))))

    return list(db.scalars(query))


@router.post("/", response_model=VideowallRead, status_code=status.HTTP_201_CREATED)
def create_videowall(
    payload: VideowallCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Videowall:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create videowalls")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del videowall es obligatorio")
    columns, rows, total_width, total_height = _validate_videowall_geometry(
        payload.columns,
        payload.rows,
        payload.total_width,
        payload.total_height,
    )

    videowall = Videowall(
        client_id=client_id,
        name=name,
        columns=columns,
        rows=rows,
        total_width=total_width,
        total_height=total_height,
        start_tolerance_ms=_require_positive_int(payload.start_tolerance_ms, "start_tolerance_ms"),
        sync_mode=(payload.sync_mode or "play_at_timestamp").strip() or "play_at_timestamp",
    )
    db.add(videowall)
    db.commit()
    db.refresh(videowall)
    return videowall


@router.patch("/{videowall_id}", response_model=VideowallRead)
def update_videowall(
    videowall_id: str,
    payload: VideowallUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Videowall:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify videowalls")

    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    next_columns = payload.columns if payload.columns is not None else videowall.columns
    next_rows = payload.rows if payload.rows is not None else videowall.rows
    next_total_width = payload.total_width if payload.total_width is not None else videowall.total_width
    next_total_height = payload.total_height if payload.total_height is not None else videowall.total_height
    columns, rows, total_width, total_height = _validate_videowall_geometry(
        next_columns,
        next_rows,
        next_total_width,
        next_total_height,
    )

    nodes = list(
        db.scalars(
            select(VideowallNode)
            .where(VideowallNode.videowall_id == videowall.id)
            .order_by(VideowallNode.position_index.asc(), VideowallNode.created_at.asc())
        )
    )

    for node in nodes:
        if node.row_index < 0 or node.row_index >= rows or node.column_index < 0 or node.column_index >= columns:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No se puede redimensionar la matriz porque uno o más nodos quedarían fuera de rango",
            )

    if payload.name is not None:
        next_name = payload.name.strip()
        if not next_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del videowall es obligatorio")
        videowall.name = next_name
    videowall.columns = columns
    videowall.rows = rows
    videowall.total_width = total_width
    videowall.total_height = total_height
    if payload.start_tolerance_ms is not None:
        videowall.start_tolerance_ms = _require_positive_int(payload.start_tolerance_ms, "start_tolerance_ms")
    if payload.sync_mode is not None:
        videowall.sync_mode = payload.sync_mode.strip() or videowall.sync_mode

    for node in nodes:
        geometry = compute_videowall_cell_by_row_col(
            columns=columns,
            rows=rows,
            total_width=total_width,
            total_height=total_height,
            row_index=node.row_index,
            column_index=node.column_index,
        )
        _apply_geometry_to_node(node, geometry)

    db.add(videowall)
    db.commit()
    db.refresh(videowall)
    return videowall


@router.delete("/{videowall_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_videowall(
    videowall_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete videowalls")

    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    nodes = list(
        db.scalars(
            select(VideowallNode)
            .where(VideowallNode.videowall_id == videowall.id)
            .order_by(VideowallNode.position_index.asc(), VideowallNode.created_at.asc())
        )
    )

    blocked_nodes: list[str] = []
    channels_to_delete: list[Channel] = []
    for node in nodes:
        channel = get_channel_in_scope(db, node.channel_id, current_user)
        if is_player_online(channel.last_heartbeat_at):
            blocked_nodes.append(f"el monitor {node.position_index} ({channel.name}) est\u00e1 en l\u00ednea")
            continue
        channels_to_delete.append(channel)

    if blocked_nodes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=build_videowall_delete_block_message(videowall, blocked_nodes),
        )

    for node in nodes:
        db.delete(node)
    for channel in channels_to_delete:
        cleanup_channel_delete_dependencies(db, channel)
        db.delete(channel)
    db.delete(videowall)
    db.commit()


@router.get("/{videowall_id}/nodes", response_model=list[VideowallNodeRead])
def list_nodes(
    videowall_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[VideowallNode]:
    get_videowall_in_scope(videowall_id, db, current_user)
    return list(
        db.scalars(
            select(VideowallNode)
            .where(VideowallNode.videowall_id == videowall_id)
            .order_by(VideowallNode.row_index.asc(), VideowallNode.column_index.asc(), VideowallNode.position_index.asc())
        )
    )


@router.post("/{videowall_id}/nodes", response_model=VideowallNodeRead, status_code=status.HTTP_201_CREATED)
def upsert_node(
    videowall_id: str,
    payload: VideowallNodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VideowallNode:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify videowalls")

    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    channel = get_channel_in_scope(db, payload.channel_id, current_user)
    if channel.client_id != videowall.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Videowall or channel not found")
    if channel.mode != ChannelMode.VIDEOWALL:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo canales en modo videowall pueden unirse a una matriz")

    videowall_branch_id = _resolve_videowall_branch_id(videowall.id, db)
    if videowall_branch_id and channel.branch_id != videowall_branch_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Todos los nodos del videowall deben pertenecer a la misma sucursal",
        )

    other_videowall = db.scalar(
        select(VideowallNode)
        .where(
            VideowallNode.channel_id == channel.id,
            VideowallNode.videowall_id != videowall.id,
        )
        .limit(1)
    )
    if other_videowall:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este canal ya pertenece a otro videowall",
        )

    existing_node = db.scalar(
        select(VideowallNode)
        .where(
            VideowallNode.videowall_id == videowall.id,
            VideowallNode.channel_id == channel.id,
        )
        .limit(1)
    )
    geometry = _resolve_node_geometry(
        videowall,
        position_index=payload.position_index,
        row_index=payload.row_index,
        column_index=payload.column_index,
        fallback_position_index=existing_node.position_index if existing_node else None,
        fallback_row_index=existing_node.row_index if existing_node else None,
        fallback_column_index=existing_node.column_index if existing_node else None,
    )
    _ensure_node_slot_available(videowall.id, geometry, db, exclude_node_id=existing_node.id if existing_node else None)

    node = existing_node or VideowallNode(videowall_id=videowall.id, channel_id=channel.id)
    _apply_geometry_to_node(node, geometry)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.patch("/{videowall_id}/nodes/{node_id}", response_model=VideowallNodeRead)
def update_node_position(
    videowall_id: str,
    node_id: str,
    payload: VideowallNodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VideowallNode:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify videowalls")

    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    node = db.scalar(
        select(VideowallNode)
        .where(
            VideowallNode.id == node_id,
            VideowallNode.videowall_id == videowall.id,
        )
        .limit(1)
    )
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Videowall node not found")

    geometry = _resolve_node_geometry(
        videowall,
        position_index=payload.position_index,
        row_index=payload.row_index,
        column_index=payload.column_index,
        fallback_position_index=node.position_index,
        fallback_row_index=node.row_index,
        fallback_column_index=node.column_index,
    )
    _ensure_node_slot_available(videowall.id, geometry, db, exclude_node_id=node.id)
    _apply_geometry_to_node(node, geometry)

    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.delete("/{videowall_id}/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node_screen(
    videowall_id: str,
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to modify videowalls")

    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    node = db.scalar(
        select(VideowallNode).where(
            VideowallNode.id == node_id,
            VideowallNode.videowall_id == videowall.id,
        )
    )
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Videowall node not found")

    channel = get_channel_in_scope(db, node.channel_id, current_user)
    delete_block_message = get_channel_delete_block_message(db, channel, include_videowall_membership=False)
    if delete_block_message:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=delete_block_message,
        )

    cleanup_channel_delete_dependencies(db, channel)
    db.delete(node)
    db.delete(channel)
    db.commit()


@router.get("/{videowall_id}/preview")
def get_preview(
    videowall_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    videowall = get_videowall_in_scope(videowall_id, db, current_user)
    nodes = list(
        db.scalars(
            select(VideowallNode)
            .where(VideowallNode.videowall_id == videowall_id)
            .order_by(VideowallNode.row_index.asc(), VideowallNode.column_index.asc(), VideowallNode.position_index.asc())
        )
    )
    return {
        "videowall": VideowallRead.model_validate(videowall).model_dump(),
        "nodes": [VideowallNodeRead.model_validate(node).model_dump() for node in nodes],
    }
