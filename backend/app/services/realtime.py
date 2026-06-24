from collections import defaultdict
from datetime import UTC, datetime

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self.client_rooms: dict[str, list[WebSocket]] = defaultdict(list)
        self.channel_rooms: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect_client(self, client_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.client_rooms[client_id].append(websocket)

    async def connect_channel(self, channel_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.channel_rooms[channel_id].append(websocket)

    def disconnect(self, room: dict[str, list[WebSocket]], key: str, websocket: WebSocket) -> None:
        if key in room and websocket in room[key]:
            room[key].remove(websocket)
            if not room[key]:
                del room[key]

    async def broadcast_to_client(self, client_id: str, payload: dict) -> None:
        for websocket in list(self.client_rooms.get(client_id, [])):
            await websocket.send_json(payload)

    async def broadcast_to_channel(self, channel_id: str, payload: dict) -> None:
        for websocket in list(self.channel_rooms.get(channel_id, [])):
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

