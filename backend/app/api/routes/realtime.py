import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.database import SessionLocal
from app.models.entities import Channel, User, UserRole
from app.services.presence import is_player_online
from app.services.realtime import manager
from app.services.tenancy import is_branch_scoped, require_branch_assignment, require_client_scope


router = APIRouter()


GLOBAL_SOCKET_ROLES = {UserRole.SUPER_ADMIN.value, UserRole.STAFF_ADMIN.value, UserRole.STAFF_OPERATOR.value}
CLIENT_SOCKET_ROLES = GLOBAL_SOCKET_ROLES | {UserRole.CLIENT.value, UserRole.CLIENT_ADMIN.value, UserRole.CLIENT_OPERATOR.value}
BRANCH_SOCKET_ROLES = {UserRole.BRANCH_MANAGER.value, UserRole.OPERATOR.value}
WS_REVALIDATION_SECONDS = 30.0


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


def _authorize_socket(token: str, room_id: str, *, channel_room: bool) -> int | None:
    # A fresh session on each authorization prevents an ORM identity map from
    # retaining a user/role that was changed after the handshake.
    with SessionLocal() as db:
        try:
            user = get_current_user(authorization=f"Bearer {token}", db=db)
        except HTTPException as exc:
            if exc.status_code in {401, 403}:
                return 4401 if exc.status_code == 401 else 4403
            raise
        role = user.role.value
        if not channel_room:
            if role not in CLIENT_SOCKET_ROLES:
                return 4403
            if role not in GLOBAL_SOCKET_ROLES and user.client_id != room_id:
                return 4403
        else:
            channel = db.get(Channel, room_id)
            if not channel:
                return 4404
            if role not in CLIENT_SOCKET_ROLES | BRANCH_SOCKET_ROLES:
                return 4403
            if role not in GLOBAL_SOCKET_ROLES and user.client_id != channel.client_id:
                return 4403
            if role in BRANCH_SOCKET_ROLES and user.branch_id != channel.branch_id:
                return 4403
    return None


async def _serve_socket(websocket: WebSocket, token: str, room_id: str, *, channel_room: bool) -> None:
    def authorize() -> int | None:
        return _authorize_socket(token, room_id, channel_room=channel_room)

    code = authorize()
    if code is not None:
        await websocket.close(code=code)
        return
    connect = manager.connect_channel if channel_room else manager.connect_client
    room = manager.channel_rooms if channel_room else manager.client_rooms
    await connect(room_id, websocket, authorize)
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=WS_REVALIDATION_SECONDS)
            except asyncio.TimeoutError:
                if not await manager.authorize(websocket):
                    return
                continue
            except json.JSONDecodeError:
                await websocket.close(code=1007)
                return
            if not await manager.authorize(websocket):
                return
            if channel_room:
                if not isinstance(message, dict):
                    await websocket.close(code=1003)
                    return
                if message.get("type") == "heartbeat":
                    await manager.broadcast_to_channel(room_id, message)
            else:
                await websocket.send_json({"type": "ack", "message": message})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(room, room_id, websocket)


@router.websocket("/ws/clients/{client_id}")
async def client_socket(websocket: WebSocket, client_id: str, token: str = Query(...)) -> None:
    await _serve_socket(websocket, token, client_id, channel_room=False)


@router.websocket("/ws/channels/{channel_id}")
async def channel_socket(websocket: WebSocket, channel_id: str, token: str = Query(...)) -> None:
    await _serve_socket(websocket, token, channel_id, channel_room=True)
