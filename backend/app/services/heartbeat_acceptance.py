"""Narrow, transaction-bound authority for durable heartbeat liveness only.

No relationship, token, media pointer or JSON metadata can be changed here.
Canonical history and labels still require the complete Player write guard.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType

from fastapi import HTTPException
from sqlalchemy import event, inspect, select, text, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from app.models.entities import Branch, Channel, ChannelConnectionStatus, Client, PlayerDevice, PlayerHeartbeatInbox
from app.services.tenant_relations import conflict


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _device_identity(row):
    return tuple(getattr(row, k) for k in ("id", "channel_id", "client_id", "branch_id", "player_token_hash", "is_active"))


def _channel_identity(row):
    return tuple(getattr(row, k) for k in ("id", "client_id", "branch_id", "channel_code"))


def _locked_roots(db, identity, channel_identity):
    """One PostgreSQL statement, ordered materialized root locks, no media graph."""
    if db.get_bind().dialect.name != "postgresql":
        channel = db.get(Channel, channel_identity[0], populate_existing=True)
        device = db.get(PlayerDevice, identity[0], populate_existing=True)
        client = db.get(Client, channel_identity[1], populate_existing=True)
        branch = db.get(Branch, channel_identity[2], populate_existing=True)
        return channel, device, client, branch
    # First concrete anchor may wait for an ordinary administrative reader;
    # we hold no other row lock yet. Later NOWAIT locks avoid BLUE's reverse
    # Device->Channel / parent-delete cycles without changing the Player clock.
    c = select(Channel).where(Channel.id == channel_identity[0]).with_for_update(of=Channel)\
        .cte("hb_accept_channel").prefix_with("MATERIALIZED")
    t = select(Client).join(c, true()).where(Client.id == channel_identity[1])\
        .with_for_update(of=Client, read=True, nowait=True).cte("hb_accept_client").prefix_with("MATERIALIZED")
    b = select(Branch).join(t, true()).where(Branch.id == channel_identity[2])\
        .with_for_update(of=Branch, read=True, nowait=True).cte("hb_accept_branch").prefix_with("MATERIALIZED")
    d = select(PlayerDevice).join(b, true()).where(PlayerDevice.id == identity[0])\
        .with_for_update(of=PlayerDevice, nowait=True).cte("hb_accept_device").prefix_with("MATERIALIZED")
    ca, da, ta, ba = (aliased(model, cte) for model, cte in
                       ((Channel, c), (PlayerDevice, d), (Client, t), (Branch, b)))
    row = db.execute(select(ca, da, ta, ba).select_from(d).join(c, true()).join(t, true()).join(b, true())
                     .execution_options(populate_existing=True)).one_or_none()
    return row if row is not None else (None, None, None, None)


@dataclass(frozen=True)
class HeartbeatAcceptance:
    scope: tuple[str, str, str, str]
    channel_values: object
    device_values: object
    transaction: object


@contextmanager
def heartbeat_accept_guard(db, *, device, channel, channel_values, device_values,
                           receipt_id, receipt_values):
    """Validate fresh scope and bind precisely three allowlisted ORM writes.

    A shared structural barrier precedes fresh READ COMMITTED snapshots. It is
    deliberately a separate statement: a snapshot taken before waiting for a
    structural writer must not authorize after that writer commits.
    """
    if db.new or db.dirty or db.deleted:
        raise conflict()
    if type(device) is not PlayerDevice or type(channel) is not Channel:
        raise conflict()
    for row in (device, channel):
        state = inspect(row)
        if not state.persistent or state.session is not db:
            raise conflict()
    original_device, original_channel = _device_identity(device), _channel_identity(channel)
    if (original_device[1:4] != original_channel[:3] or not original_device[-1]
            or not original_device[4] or not original_channel[1] or not original_channel[2]):
        raise conflict()
    cv, dv = dict(channel_values), dict(device_values)
    if (set(cv) != {"status", "last_ping_at"} or not isinstance(cv["status"], ChannelConnectionStatus)
            or set(dv) - {"last_seen_at", "app_version"} or "last_seen_at" not in dv
            or not isinstance(cv["last_ping_at"], datetime) or not isinstance(dv["last_seen_at"], datetime)
            or ("app_version" in dv and (type(dv["app_version"]) is not str or not dv["app_version"]))):
        raise conflict()
    scope = (original_device[0], original_channel[0], original_channel[1], original_channel[2])
    if tuple(receipt_values.get(k) for k in ("player_device_id", "channel_id", "client_id", "branch_id")) != scope:
        raise conflict()
    if (type(receipt_id) is not str or not receipt_id or receipt_values.get("status") != "PENDING"
            or type(receipt_values.get("attempts")) is not int or receipt_values["attempts"] != 0
            or receipt_values.get("last_error_code") is not None
            or type(receipt_values.get("payload_json")) is not dict
            or type(receipt_values.get("payload_bytes")) is not int or receipt_values["payload_bytes"] <= 0
            or not isinstance(receipt_values.get("accepted_at"), datetime)
            or not isinstance(receipt_values.get("available_at"), datetime)):
        raise conflict()
    if not (_utc(cv["last_ping_at"]) == _utc(dv["last_seen_at"])
            == _utc(receipt_values["accepted_at"]) == _utc(receipt_values["available_at"])):
        raise conflict()
    from app.services.tenant_ownership import freeze
    receipt_expected = freeze(dict(receipt_values, id=receipt_id))
    try:
        connection = db.connection()
        if connection.dialect.name == "postgresql":
            if connection.get_isolation_level() != "READ COMMITTED":
                raise conflict()
            # The existing Player guard uses this same DB lock timeout.
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            key = int.from_bytes(sha256(("tecno.tenant.write.v1:" + scope[2]).encode()).digest()[:8], "big", signed=True)
            connection.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key})
        elif connection.dialect.name == "sqlite":
            if connection.connection.driver_connection.in_transaction:
                raise conflict()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise conflict()
        current_channel, current_device, client, branch = _locked_roots(db, original_device, original_channel)
        if (current_device is None or current_channel is None
                or _device_identity(current_device) != original_device
                or _channel_identity(current_channel) != original_channel):
            raise conflict()
        if (client is None or branch is None or client.status != "active"
                or not branch.is_active or branch.client_id != client.id):
            raise HTTPException(403, "Player operation unavailable")
        for values, field, prior in ((cv, "last_ping_at", current_channel.last_ping_at),
                                      (dv, "last_seen_at", current_device.last_seen_at)):
            values[field] = max(_utc(values[field]), _utc(prior)) if prior else _utc(values[field])
        transaction = db.get_transaction()
        plan = HeartbeatAcceptance(scope, MappingProxyType(cv), MappingProxyType(dv), transaction)

        def validate_flush(session, _context, _instances):
            if session is not db or db.get_transaction() is not transaction or not transaction.is_active or db.deleted:
                raise conflict()
            if (_device_identity(current_device) != original_device
                    or _channel_identity(current_channel) != original_channel):
                raise conflict()
            new = tuple(db.new)
            if (len(new) != 1 or type(new[0]) is not PlayerHeartbeatInbox
                    or freeze({k: getattr(new[0], k) for k in receipt_expected}) != receipt_expected):
                raise conflict()
            # A receipt alone is insufficient. Require the approved liveness
            # values even when a caller forgot to mark either root dirty.
            for root, expected in ((current_channel, cv), (current_device, dv)):
                if any(getattr(root, k) != value for k, value in expected.items()):
                    raise conflict()
            for changed in db.dirty:
                expected = cv if changed is current_channel else dv if changed is current_device else None
                if expected is None:
                    raise conflict()
                altered = {a.key for a in inspect(changed).attrs if a.history.has_changes()}
                if altered - set(expected) or any(getattr(changed, k) != value for k, value in expected.items()):
                    raise conflict()
        def reject_manual_dml(execution):
            # The reviewed executor uses ORM flush for exactly three rows;
            # no bulk Session write belongs to this narrow certificate.
            if execution.is_insert or execution.is_update or execution.is_delete:
                raise conflict()

        event.listen(db, "before_flush", validate_flush)
        event.listen(db, "do_orm_execute", reject_manual_dml)
        try:
            yield plan
        finally:
            event.remove(db, "do_orm_execute", reject_manual_dml)
            event.remove(db, "before_flush", validate_flush)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) not in {"40001", "40P01", "55P03"}:
            raise
        db.rollback()
        if getattr(error.orig, "sqlstate", None) == "55P03":
            exc = HTTPException(503, "Player operation temporarily unavailable")
            exc._player_503_reason = "TENANT_GUARD_LOCK_UNAVAILABLE"
            raise exc from error
        raise conflict() from error


@contextmanager
def locked_player_heartbeat_receipt(db, *, plan, receipt_id, channel, player_device):
    """Consume only under the live, complete canonical heartbeat write plan."""
    from app.services.tenant_mutations import _PlayerTelemetryGraph
    gate = plan._gate
    if (type(gate.graph) is not _PlayerTelemetryGraph or gate.graph.session is not db
            or not gate.active or gate.transaction is not db.get_transaction()
            or plan.operation.function != "create_player_heartbeat"):
        raise conflict()
    plan.assert_usable(gate, plan.context)
    for row in (channel, player_device):
        state = inspect(row)
        if not state.persistent or state.session is not db:
            raise conflict()
    row = db.scalar(select(PlayerHeartbeatInbox).where(PlayerHeartbeatInbox.id == receipt_id)
                    .with_for_update(nowait=True).execution_options(populate_existing=True))
    if row is not None and (row.player_device_id, row.channel_id, row.client_id, row.branch_id) != (
            inspect(player_device).identity[0], inspect(channel).identity[0], plan.context.client_id, plan.context.branch_id):
        raise conflict()
    yield row
