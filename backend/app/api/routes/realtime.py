from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import decode_access_token
from app.core.database import SessionLocal
from app.models.entities import Channel, User, UserRole
from app.services.presence import is_player_online
from app.services.realtime import manager
from app.services.tenancy import is_branch_scoped, require_branch_assignment, require_client_scope


router = APIRouter()


GLOBAL_SOCKET_ROLES = {UserRole.SUPER_ADMIN.value, UserRole.STAFF_ADMIN.value, UserRole.STAFF_OPERATOR.value}
CLIENT_SOCKET_ROLES = GLOBAL_SOCKET_ROLES | {UserRole.CLIENT.value, UserRole.CLIENT_ADMIN.value, UserRole.CLIENT_OPERATOR.value}
BRANCH_SOCKET_ROLES = {UserRole.BRANCH_MANAGER.value, UserRole.OPERATOR.value}


@router.get("/presence")
def presence(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    scoped_client_id = require_client_scope(current_user, client_id or current_user.client_id)
    query = select(Channel).where(Channel.client_id == scoped_client_id)
    if is_branch_scoped(current_user):
        query = query.where(Channel.branch_id == require_branch_assignment(current_user))
    channels = list(db.scalars(query))
    return {
        "client_id": scoped_client_id,
        "online": [channel.id for channel in channels if is_player_online(channel.last_heartbeat_at)],
        "total": len(channels),
    }


@router.websocket("/ws/clients/{client_id}")
async def client_socket(websocket: WebSocket, client_id: str, token: str = Query(...)) -> None:
    payload = decode_access_token(token)
    payload_client_id = payload.get("client_id")
    payload_role = payload.get("role")
    if payload_role not in CLIENT_SOCKET_ROLES:
        await websocket.close(code=4403)
        return
    if payload_role not in GLOBAL_SOCKET_ROLES and payload_client_id != client_id:
        await websocket.close(code=4403)
        return

    await manager.connect_client(client_id, websocket)
    try:
        while True:
            message = await websocket.receive_json()
            await websocket.send_json({"type": "ack", "message": message})
    except WebSocketDisconnect:
        manager.disconnect(manager.client_rooms, client_id, websocket)


@router.websocket("/ws/channels/{channel_id}")
async def channel_socket(websocket: WebSocket, channel_id: str, token: str = Query(...)) -> None:
    payload = decode_access_token(token)
    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        if not channel:
            await websocket.close(code=4404)
            return
        payload_client_id = payload.get("client_id")
        payload_role = payload.get("role")
        payload_branch_id = payload.get("branch_id")
        if payload_role not in GLOBAL_SOCKET_ROLES and payload_client_id != channel.client_id:
            await websocket.close(code=4403)
            return
        if payload_role in BRANCH_SOCKET_ROLES and payload_branch_id != channel.branch_id:
            await websocket.close(code=4403)
            return
    finally:
        db.close()

    await manager.connect_channel(channel_id, websocket)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "heartbeat":
                await manager.broadcast_to_channel(channel_id, message)
    except WebSocketDisconnect:
        manager.disconnect(manager.channel_rooms, channel_id, websocket)
