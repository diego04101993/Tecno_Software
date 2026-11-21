"""Fresh runtime activity proof with bounded, lock-free waits between attempts."""
from datetime import UTC, datetime
import time

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.entities import Branch, Channel, Client, PlayerDevice
from app.services.tenant_mutations import MutationIntent, PlayerRuntimeRootBusy, player_write_guard
from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import conflict, model_class


# The existing client target is eight seconds, measured from admission entry.
# Only half may be spent waiting for ownership roots; the builder/serialization
# retain the other half. This does not alter admission, pool or HTTP timeouts.
RUNTIME_REQUEST_BUDGET_NS = 8_000_000_000
RUNTIME_ROOT_WAIT_BUDGET_NS = 4_000_000_000
MAX_ROOT_WAITS = 4


def player_identity(device):
    return tuple(getattr(device, field) for field in
                 ("id", "channel_id", "client_id", "branch_id", "player_token_hash", "is_active"))


def _channel_identity(channel):
    return tuple(getattr(channel, field) for field in ("id", "client_id", "branch_id", "channel_code"))


def validate_runtime_player(db, expected):
    """Fresh authentication/scope within this transaction, never cached authority."""
    device = db.get(PlayerDevice, expected[0], populate_existing=True)
    if device is None or not device.is_active or device.player_token_hash != expected[4]:
        raise HTTPException(401, "Invalid player token")
    if player_identity(device) != expected:
        raise conflict()
    channel = db.get(Channel, expected[1], populate_existing=True)
    if channel is None or (channel.client_id, channel.branch_id) != expected[2:4]:
        raise HTTPException(404, "Channel not found")
    client = db.get(Client, channel.client_id, populate_existing=True)
    branch = db.get(Branch, channel.branch_id, populate_existing=True)
    if (client is None or client.status != "active" or branch is None or not branch.is_active
            or branch.client_id != client.id):
        raise HTTPException(403, "Player operation unavailable")
    return device, channel


def _unavailable(source=None):
    from app.core.request_pg_diagnostics import capture_failure
    error = HTTPException(503, "Player operation temporarily unavailable")
    return capture_failure(error, source_error=source)


def _set_wait_budget(db, deadline_ns):
    milliseconds = max(1, (deadline_ns - time.monotonic_ns()) // 1_000_000)
    db.execute(text("SET LOCAL lock_timeout = '" + str(milliseconds) + "ms'"))


def record_runtime_activity(db, device, channel, *, request_started_ns=None):
    """Run the unchanged complete guard, never retry writes or uncertain commits.

    A NOWAIT conflict on one of the four exact authenticated roots rolls back
    the entire guard, including its advisory/row/media locks. A separate
    transaction waits for only that root, releases it, then authenticates and
    runs both ownership phases again. Waiting is a wakeup, not authorization.
    Four waits and one fixed monotonic deadline bound repeated interference.
    """
    expected = player_identity(device)
    channel_expected = _channel_identity(channel)
    postgres = db.get_bind().dialect.name == "postgresql"
    now = time.monotonic_ns()
    started = now if request_started_ns is None else min(now, request_started_ns)
    wait_deadline = min(started + RUNTIME_REQUEST_BUDGET_NS,
                        now + RUNTIME_ROOT_WAIT_BUDGET_NS)
    # Preserve the same timestamp through every pre-write attempt, normalized
    # monotonically by the original guard against freshly locked state.
    observed_at = datetime.now(UTC)
    for attempt in range(MAX_ROOT_WAITS + 1):
        entered = False
        try:
            if postgres and time.monotonic_ns() >= wait_deadline:
                raise _unavailable()
            if attempt:
                device, channel = validate_runtime_player(db, expected)
                if _channel_identity(channel) != channel_expected:
                    raise conflict()
            intent = MutationIntent("UPDATE", RowRef("PlayerDevice", expected[0]),
                                    (("last_seen_at", observed_at),))
            kwargs = {"runtime_wait_deadline_ns": wait_deadline} if postgres else {}
            with player_write_guard(db, channel=channel, player_device=device,
                                    operation="get_runtime_config", intents=(intent,), **kwargs) as plan:
                entered = True
                device.last_seen_at = next(p.values["last_seen_at"] for p in plan.proposed
                                           if p.reference == intent.reference)
                db.add(device)
                db.commit()
            return channel, expected
        except PlayerRuntimeRootBusy as busy:
            if entered:
                raise
            source = busy.__cause__
            db.rollback()
            if attempt == MAX_ROOT_WAITS or time.monotonic_ns() >= wait_deadline:
                raise _unavailable(source) from source
            try:
                _set_wait_budget(db, wait_deadline)
                model = model_class(busy.reference.model)
                # No tenant barrier or other row/media lock is held here.
                db.scalar(select(model.id).where(model.id == busy.reference.identity)
                          .with_for_update(of=model, read=busy.lock_mode == "share"))
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    raise _unavailable(error) from error
                raise
            finally:
                db.rollback()
    raise AssertionError("Bounded runtime root attempts exhausted")
