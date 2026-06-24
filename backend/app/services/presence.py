from __future__ import annotations

from datetime import UTC, datetime, timedelta


ONLINE_HEARTBEAT_WINDOW_SECONDS = 90
ONLINE_HEARTBEAT_WINDOW = timedelta(seconds=ONLINE_HEARTBEAT_WINDOW_SECONDS)


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)


def heartbeat_age_seconds(last_heartbeat_at: datetime | None, *, current_time: datetime | None = None) -> int | None:
    heartbeat_at = _to_utc(last_heartbeat_at)
    if heartbeat_at is None:
        return None

    reference_time = _to_utc(current_time) or now_utc()
    return max(0, int((reference_time - heartbeat_at).total_seconds()))


def is_player_online(last_heartbeat_at: datetime | None, *, current_time: datetime | None = None) -> bool:
    age_seconds = heartbeat_age_seconds(last_heartbeat_at, current_time=current_time)
    return age_seconds is not None and age_seconds <= ONLINE_HEARTBEAT_WINDOW_SECONDS


def resolve_presence_status(last_heartbeat_at: datetime | None, *, current_time: datetime | None = None) -> str:
    return "online" if is_player_online(last_heartbeat_at, current_time=current_time) else "offline"


def build_presence_snapshot(last_heartbeat_at: datetime | None, *, current_time: datetime | None = None) -> dict[str, object]:
    heartbeat_at = _to_utc(last_heartbeat_at)
    age_seconds = heartbeat_age_seconds(heartbeat_at, current_time=current_time)
    online = is_player_online(heartbeat_at, current_time=current_time)
    return {
        "last_heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": age_seconds,
        "is_online": online,
        "status": "online" if online else "offline",
    }
