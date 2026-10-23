from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import WebSocket
from starlette.websockets import WebSocketState


class ConnectionManager:
    def __init__(self) -> None:
        self.client_rooms: dict[str, list[WebSocket]] = defaultdict(list)
        self.channel_rooms: dict[str, list[WebSocket]] = defaultdict(list)
        self.authorizers: dict[WebSocket, Callable[[], int | None]] = {}

    async def connect_client(self, client_id: str, websocket: WebSocket, authorize: Callable[[], int | None]) -> None:
        await websocket.accept()
        self.authorizers[websocket] = authorize
        self.client_rooms[client_id].append(websocket)

    async def connect_channel(self, channel_id: str, websocket: WebSocket, authorize: Callable[[], int | None]) -> None:
        await websocket.accept()
        self.authorizers[websocket] = authorize
        self.channel_rooms[channel_id].append(websocket)

    def disconnect(self, room: dict[str, list[WebSocket]], key: str, websocket: WebSocket) -> None:
        self.authorizers.pop(websocket, None)
        if key in room and websocket in room[key]:
            room[key].remove(websocket)
            if not room[key]:
                del room[key]

    async def authorize(self, websocket: WebSocket) -> bool:
        if websocket.application_state != WebSocketState.CONNECTED:
            return False
        authorize = self.authorizers.get(websocket)
        code = authorize() if authorize is not None else 4401
        if code is None:
            return True
        await websocket.close(code=code)
        for room in (self.client_rooms, self.channel_rooms):
            for key in list(room):
                self.disconnect(room, key, websocket)
        return False

    async def broadcast_to_client(self, client_id: str, payload: dict) -> None:
        for websocket in list(self.client_rooms.get(client_id, [])):
            if await self.authorize(websocket):
                await websocket.send_json(payload)

    async def broadcast_to_channel(self, channel_id: str, payload: dict) -> None:
        for websocket in list(self.channel_rooms.get(channel_id, [])):
            if await self.authorize(websocket):
                await websocket.send_json(payload)

    @staticmethod
    def heartbeat_payload(channel_id: str, playback: str | None = None) -> dict:
        return {
            "type": "heartbeat",
            "channel_id": channel_id,
            "current_playback": playback,
            "timestamp": datetime.now(UTC).isoformat(),
        }


manager = ConnectionManager()

