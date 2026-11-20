"""Narrow, transaction-bound authority for durable heartbeat liveness only.

No relationship, token, media pointer or JSON metadata can be changed here.
Canonical history and labels still require the complete Player write guard.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import time
from types import MappingProxyType

from fastapi import HTTPException
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import DBAPIError

from app.models.entities import Branch, Channel, ChannelConnectionStatus, Client, PlayerDevice, PlayerHeartbeatInbox
from app.services.tenant_relations import conflict


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _device_identity(row):
    return tuple(getattr(row, k) for k in ("id", "channel_id", "client_id", "branch_id", "player_token_hash", "is_active"))


def _channel_identity(row):
    return tuple(getattr(row, k) for k in ("id", "client_id", "branch_id", "channel_code"))


class _HeartbeatRootBusy(Exception):
    """An exact pre-write lock stage; never log its historical identifiers."""
    def __init__(self, model, identity, shared):
        self.model, self.identity, self.shared = model, identity, shared


def _locked_roots(db, identity, channel_identity):
    """Fresh root locks; a busy stage is explicit, never parsed from SQL text."""
    if db.get_bind().dialect.name != "postgresql":
        channel = db.get(Channel, channel_identity[0], populate_existing=True)
        device = db.get(PlayerDevice, identity[0], populate_existing=True)
        client = db.get(Client, channel_identity[1], populate_existing=True)
        branch = db.get(Branch, channel_identity[2], populate_existing=True)
        return channel, device, client, branch
    values = {}
    for model, key, shared in ((Channel, channel_identity[0], False),
                               (Client, channel_identity[1], True),
                               (Branch, channel_identity[2], True),
                               (PlayerDevice, identity[0], False)):
        try:
            row = db.scalar(select(model).where(model.id == key)
                            .with_for_update(of=model, read=shared, nowait=True)
                            .execution_options(populate_existing=True))
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            raise _HeartbeatRootBusy(model, key, shared) from error
        if row is None:
            return None, None, None, None
        values[model] = row
    return values[Channel], values[PlayerDevice], values[Client], values[Branch]


def _lock_unavailable():
    error = HTTPException(503, "Player operation temporarily unavailable")
    error._player_503_reason = "TENANT_GUARD_LOCK_UNAVAILABLE"
    return error


def _set_lock_budget(connection, wait_deadline_ns):
    # A late grant may still finish an uncontended NOWAIT fast path. It must
    # never start a new lock wait or inherit the old ten-second DB timeout.
    milliseconds = max(1, (wait_deadline_ns - time.monotonic_ns()) // 1_000_000)
    connection.execute(text("SET LOCAL lock_timeout = '" + str(milliseconds) + "ms'"))


def _postgres_roots(db, identity, channel_identity, deadline_ns):
    """Wait for a known conflicting row only after releasing every held lock.

    BLUE may lock Device->Channel while GREEN locks Channel->Device. Waiting
    for either second row while retaining the first can deadlock. Each busy
    probe therefore rolls back its entire read-only transaction, waits on only
    that exact row, releases it, and obtains all four fresh locks again. There
    is no retry after writes, COMMIT, generic DB error or a failed authority
    check, no sleep/poll loop, and no extension of the original request clock.
    """
    from app.core.player_admission import HEARTBEAT_HANDLER_RESERVE_NS
    wait_deadline_ns = deadline_ns - HEARTBEAT_HANDLER_RESERVE_NS
    key = int.from_bytes(sha256(("tecno.tenant.write.v1:" + channel_identity[1]).encode()).digest()[:8],
                         "big", signed=True)
    while True:
        connection = db.connection()
        if connection.get_isolation_level() != "READ COMMITTED":
            raise conflict()
        _set_lock_budget(connection, wait_deadline_ns)
        # Separate statement: a writer may commit while this barrier waits.
        # The following root queries must use new READ COMMITTED snapshots.
        connection.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key})
        try:
            roots = _locked_roots(db, identity, channel_identity)
        except _HeartbeatRootBusy as busy:
            db.rollback()
            if time.monotonic_ns() >= wait_deadline_ns:
                raise _lock_unavailable() from None
            try:
                connection = db.connection()
                _set_lock_budget(connection, wait_deadline_ns)
                # This transaction owns no tenant, parent or other row lock.
                # A successful wait is only a wakeup, never an authorization.
                db.scalar(select(busy.model.id).where(busy.model.id == busy.identity)
                          .with_for_update(of=busy.model, read=busy.shared))
            finally:
                db.rollback()
            continue
        _set_lock_budget(connection, wait_deadline_ns)
        return roots


@dataclass(frozen=True)
class HeartbeatAcceptance:
    scope: tuple[str, str, str, str]
    channel_values: object
    device_values: object
    transaction: object


@contextmanager
def heartbeat_accept_guard(db, *, device, channel, channel_values, device_values,
                           receipt_id, receipt_values, deadline_ns=None):
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
    from app.core.player_admission import HEARTBEAT_REQUEST_BUDGET_NS
    latest_deadline = time.monotonic_ns() + HEARTBEAT_REQUEST_BUDGET_NS
    if deadline_ns is None:
        deadline_ns = latest_deadline
    elif type(deadline_ns) is not int:
        raise conflict()
    else:
        deadline_ns = min(deadline_ns, latest_deadline)
    try:
        connection = db.connection()
        if connection.dialect.name == "postgresql":
            roots = _postgres_roots(db, original_device, original_channel, deadline_ns)
        elif connection.dialect.name == "sqlite":
            if connection.connection.driver_connection.in_transaction:
                raise conflict()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise conflict()
        if connection.dialect.name != "postgresql":
            roots = _locked_roots(db, original_device, original_channel)
        current_channel, current_device, client, branch = roots
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
            raise _lock_unavailable() from error
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
