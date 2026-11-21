"""Heartbeat-only durable receipt acceptance and one owned consumer.

No schema creation, startup side effects, network access, raw payload logging or
client-submission deduplication. A successful ACK follows the inbox COMMIT.
The processor supplies the existing authoritative sanitization/write guard.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager
import contextvars
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import threading
import time
from typing import Any
from uuid import uuid4
from weakref import WeakKeyDictionary

import anyio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, exists, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.models.entities import Branch, Channel, Client, ChannelConnectionStatus, PlayerDevice, PlayerHeartbeatInbox
from app.schemas.domain import PlayerHeartbeatAck, PlayerHeartbeatCreate
from app.services.tenancy import require_active_channel_tenant
from app.services.tenant_relations import conflict
from app.services.heartbeat_acceptance import heartbeat_accept_guard, locked_player_heartbeat_receipt
from app.core.request_pg_diagnostics import worker_scope


MAX_PENDING_COUNT = 128
MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_ATTEMPTS = 5
_RETRY_SECONDS = (1, 2, 4, 8)
_CAPACITY_KEY = int.from_bytes(hashlib.sha256(b"tecno.heartbeat.inbox.capacity.v1").digest()[:8], "big", signed=True)
_WORKER_KEY = int.from_bytes(hashlib.sha256(b"tecno.heartbeat.inbox.worker.v1").digest()[:8], "big", signed=True)
_SQLITE_TEST_LOCK = threading.RLock()
_SQLITE_WORKER_LOCK = threading.Lock()
_METRICS_LOCK = threading.Lock()
_METRICS: WeakKeyDictionary[Any, Counter] = WeakKeyDictionary()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _engine(bind):
    return getattr(bind, "engine", bind)


def _increment(bind, name: str, amount: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS.setdefault(_engine(bind), Counter())[name] += amount


def counters(bind) -> dict[str, int]:
    """Process counters contain no request, player, tenant, token or body values."""
    with _METRICS_LOCK:
        return dict(_METRICS.get(_engine(bind), Counter()))


def _payload_json(payload: PlayerHeartbeatCreate) -> tuple[dict, str, int]:
    # Canonicalization is only an immutable receipt comparison/byte weight.
    # It is NEVER an idempotency key between separate HTTP submissions.
    value = payload.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return json.loads(encoded), encoded, len(encoded.encode("utf-8"))


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@contextmanager
def _capacity_serialization(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # Transaction-scoped and released by the caller's COMMIT/ROLLBACK.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CAPACITY_KEY})
        yield
    elif dialect == "sqlite":
        # Test-only, one process. Not advertised as a distributed SQL lock.
        with _SQLITE_TEST_LOCK:
            yield
    else:
        raise RuntimeError("Heartbeat inbox requires PostgreSQL or the isolated SQLite test substitute")


def _storage_unavailable():
    return HTTPException(status_code=503, detail="Heartbeat receipt storage is temporarily unavailable")


def enqueue(db: Session, device: PlayerDevice, channel: Channel,
            payload: PlayerHeartbeatCreate, *, deadline_ns: int | None = None) -> PlayerHeartbeatAck:
    """One complete private report and required liveness state commit before200."""
    bind = db.get_bind()
    # A Session bound to an externally owned Connection may leave its outer
    # transaction uncommitted after Session.commit(). Such a200 is not durable.
    if not isinstance(bind, Engine):
        raise conflict()
    _increment(bind, "enqueue_attempts")
    if (type(device) is not PlayerDevice or type(channel) is not Channel
            or inspect(device).session is not db or inspect(channel).session is not db
            or not inspect(device).persistent or not inspect(channel).persistent
            or db.new or db.dirty or db.deleted or db.in_nested_transaction()):
        raise conflict()
    expected = (device.id, channel.id, channel.client_id, channel.branch_id)
    if (not device.is_active or (device.channel_id, device.client_id, device.branch_id) != expected[1:]):
        raise conflict()
    try:
        value, _, payload_bytes = _payload_json(payload)
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError("Heartbeat report exceeds the finite per-report budget")
    except (ValueError, TypeError, UnicodeError):
        _increment(bind, "invalid_payload")
        raise HTTPException(status_code=422, detail="Invalid heartbeat report payload") from None
    try:
        accepted_at = datetime.now(UTC)
        identity = str(uuid4())
        try:
            channel_status = ChannelConnectionStatus(payload.status.lower())
        except ValueError:
            channel_status = ChannelConnectionStatus.UNKNOWN
        channel_values = {"status": channel_status, "last_ping_at": accepted_at}
        device_values = {"last_seen_at": accepted_at}
        if payload.app_version:
            device_values["app_version"] = payload.app_version
        values = dict(player_device_id=expected[0], channel_id=expected[1], client_id=expected[2],
                      branch_id=expected[3], payload_json=value, payload_bytes=payload_bytes,
                      accepted_at=accepted_at, status="PENDING", attempts=0,
                      available_at=accepted_at, last_error_code=None)
        # The typed guard owns the same tenant barrier and fresh four-root row
        # locks. Its certificate permits only these liveness scalars and this
        # immutable private receipt, never optional references or label writes.
        with heartbeat_accept_guard(db, device=device, channel=channel,
                                    receipt_id=identity, receipt_values=values,
                                    channel_values=channel_values, device_values=device_values,
                                    deadline_ns=deadline_ns) as plan:
            if plan.scope != expected or plan.transaction is not db.get_transaction():
                raise conflict()
            with _capacity_serialization(db):
                count, total_bytes = db.execute(select(func.count(), func.coalesce(func.sum(
                    PlayerHeartbeatInbox.payload_bytes), 0)).select_from(PlayerHeartbeatInbox)).one()
                if int(count) >= MAX_PENDING_COUNT or int(total_bytes) + payload_bytes > MAX_PENDING_BYTES:
                    _increment(bind, "capacity_rejections")
                    raise _storage_unavailable()
                for key, item in plan.channel_values.items():
                    setattr(channel, key, item)
                for key, item in plan.device_values.items():
                    setattr(device, key, item)
                db.add(PlayerHeartbeatInbox(id=identity, **values))
                db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        _increment(bind, "enqueue_storage_errors")
        raise _storage_unavailable() from None
    _increment(bind, "accepted_commits")
    return PlayerHeartbeatAck(ok=True, received_at=accepted_at, channel_id=expected[1])


@dataclass(frozen=True)
class _Receipt:
    identity: str
    scope: tuple[str, str, str, str]
    canonical_payload: str
    payload_bytes: int
    accepted_at: datetime

    @classmethod
    def capture(cls, row):
        return cls(row.id, (row.player_device_id, row.channel_id, row.client_id, row.branch_id),
                   _canonical(row.payload_json), row.payload_bytes, _utc(row.accepted_at))

    def matches(self, row) -> bool:
        return (self.identity == row.id
                and self.scope == (row.player_device_id, row.channel_id, row.client_id, row.branch_id)
                and self.canonical_payload == _canonical(row.payload_json)
                and self.payload_bytes == row.payload_bytes
                and self.accepted_at == _utc(row.accepted_at))


class _NoLongerAvailable(Exception):
    pass


class _AuthorityChanged(Exception):
    pass


class _TargetDeleted(Exception):
    pass


class _ImmutableReceiptChanged(Exception):
    pass


def _failure_code(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, _TargetDeleted):
        return "TARGET_DELETED", True
    if isinstance(exc, _ImmutableReceiptChanged):
        return "IMMUTABLE_RECEIPT_CHANGED", True
    if isinstance(exc, (_AuthorityChanged, ValidationError, ValueError, TypeError, UnicodeError)):
        return "AUTHORITY_CHANGED", True
    if isinstance(exc, HTTPException):
        return ("AUTHORITY_CHANGED", True) if exc.status_code in {401, 403, 404, 409, 422} else (
            "PROCESSOR_TEMPORARILY_UNAVAILABLE", False)
    if isinstance(exc, SQLAlchemyError):
        return "DATABASE_TEMPORARILY_UNAVAILABLE", False
    return "PROCESSOR_ERROR", False


def _retain_failure(engine, receipt: _Receipt, code: str, terminal: bool) -> bool:
    # The failed processing Session has already closed/rolled back. Only
    # private retry scalars change; historical scope and payload are untouched.
    try:
        with Session(engine, autoflush=False) as db:
            row = db.scalar(select(PlayerHeartbeatInbox).where(PlayerHeartbeatInbox.id == receipt.identity)
                            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if row is None or row.status != "PENDING":
                return False
            if not receipt.matches(row):
                terminal, code = True, "IMMUTABLE_RECEIPT_CHANGED"
            elif terminal and code.startswith("AUTHORITY"):
                if any(db.get(model, identity) is None for model, identity in zip(
                        (PlayerDevice, Channel, Client, Branch), receipt.scope)):
                    code = "TARGET_DELETED"
            row.attempts += 1
            row.last_error_code = code
            if terminal or row.attempts >= MAX_ATTEMPTS:
                row.status = "FAILED"
                metric = "retained_failed"
            else:
                row.available_at = datetime.now(UTC) + timedelta(seconds=_RETRY_SECONDS[row.attempts - 1])
                metric = "scheduled_retries"
            db.commit()
            _increment(engine, metric)
            return True
    except SQLAlchemyError:
        _increment(engine, "failure_record_storage_errors")
        return False


def materialize_one(engine, processor: Callable) -> bool:
    """Own one short Session; never borrow a request's connection or transaction."""
    engine = _engine(engine)
    if engine.dialect.name == "sqlite":
        if not _SQLITE_WORKER_LOCK.acquire(blocking=False):
            _increment(engine, "materializer_busy")
            return False
        try:
            return _materialize_one_locked(engine, processor)
        finally:
            _SQLITE_WORKER_LOCK.release()
    return _materialize_one_locked(engine, processor)


def _materialize_one_locked(engine, processor) -> bool:
    receipt = None
    try:
        with Session(engine, autoflush=False) as db:
            if engine.dialect.name == "postgresql" and not db.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(:key)"), {"key": _WORKER_KEY}):
                _increment(engine, "materializer_busy")
                return False
            # A later report for a device must not overtake its earlier pending
            # report, including one currently delayed by transient backoff.
            older = aliased(PlayerHeartbeatInbox)
            row = db.scalar(select(PlayerHeartbeatInbox).where(
                PlayerHeartbeatInbox.status == "PENDING", PlayerHeartbeatInbox.available_at <= datetime.now(UTC),
                ~exists(select(older.id).where(older.player_device_id == PlayerHeartbeatInbox.player_device_id,
                    older.status == "PENDING", or_(older.accepted_at < PlayerHeartbeatInbox.accepted_at,
                        and_(older.accepted_at == PlayerHeartbeatInbox.accepted_at, older.id < PlayerHeartbeatInbox.id)))))
                .order_by(PlayerHeartbeatInbox.accepted_at, PlayerHeartbeatInbox.id).limit(1))
            if row is None:
                return False
            receipt = _Receipt.capture(row)
            payload = PlayerHeartbeatCreate.model_validate(json.loads(receipt.canonical_payload))
            device = db.get(PlayerDevice, receipt.scope[0])
            channel = db.get(Channel, receipt.scope[1])
            if device is None or channel is None:
                raise _TargetDeleted()
            if (not device.is_active
                    or (device.channel_id, device.client_id, device.branch_id) != receipt.scope[1:]
                    or (channel.id, channel.client_id, channel.branch_id) != receipt.scope[1:]):
                raise _AuthorityChanged()
            # Token rotation after durable acceptance does not revoke an already
            # authenticated report. Current active scope still governs effects.
            require_active_channel_tenant(channel, db)
            claimed = False

            def before_commit(plan):
                nonlocal claimed
                with locked_player_heartbeat_receipt(db, plan=plan, receipt_id=receipt.identity,
                                                    channel=channel, player_device=device) as current:
                    if current is None or current.status != "PENDING":
                        raise _NoLongerAvailable()
                    if not receipt.matches(current):
                        raise _ImmutableReceiptChanged()
                    db.delete(current)
                    claimed = True

            # The callback keeps this same transaction (and worker advisory lock)
            # through its complete canonical proof, history insert and COMMIT.
            processor(payload, device, db, event_id=receipt.identity,
                      received_at=receipt.accepted_at, raw_report=json.loads(receipt.canonical_payload),
                      before_commit=before_commit)
            if not claimed:
                raise RuntimeError("Heartbeat processor did not claim its durable receipt")
            _increment(engine, "materialized_commits")
            return True
    except _NoLongerAvailable:
        _increment(engine, "receipt_claim_misses")
        return False
    except Exception as exc:
        _increment(engine, "processing_failures")
        if receipt is None:
            _increment(engine, "candidate_read_errors")
            return False
        code, terminal = _failure_code(exc)
        return _retain_failure(engine, receipt, code, terminal)


def snapshot(db: Session) -> dict[str, int | float | None]:
    """Small private table aggregate; no raw IDs/payloads or historical scan."""
    count, payload_bytes, oldest = db.execute(select(func.count(), func.coalesce(func.sum(
        PlayerHeartbeatInbox.payload_bytes), 0), func.min(PlayerHeartbeatInbox.accepted_at))
        .select_from(PlayerHeartbeatInbox)).one()
    failed = db.scalar(select(func.count()).select_from(PlayerHeartbeatInbox).where(
        PlayerHeartbeatInbox.status == "FAILED"))
    return {"pending_count": int(count), "backlog_count": int(count),
            "pending_only_count": int(count) - int(failed), "pending_payload_bytes": int(payload_bytes),
            "failed_count": int(failed),
            "oldest_age_seconds": max(0.0, (datetime.now(UTC) - _utc(oldest)).total_seconds()) if oldest else None,
            **counters(db.get_bind())}


@dataclass
class _Worker:
    loop: Any
    stop: asyncio.Event
    wake: asyncio.Event
    task: asyncio.Task | None = None


@asynccontextmanager
async def _no_activity() -> AsyncIterator[None]:
    yield


async def _finish_atomic_thread(engine, processor) -> bool:
    # Cancellation of the AnyIO child Task can finish its Future while its
    # synchronous callable still owns SQL. Track that callable independently.
    state_lock = threading.Lock()
    finished = threading.Event()
    state = {"started": False, "cancel_before_start": False}

    def owned_call():
        with state_lock:
            if state["cancel_before_start"]:
                finished.set()
                return False
            state["started"] = True
        try:
            return materialize_one(engine, processor)
        finally:
            # materialize_one has exited its owned Session before this signal.
            finished.set()

    job = asyncio.create_task(anyio.to_thread.run_sync(owned_call, abandon_on_cancel=False),
                              context=contextvars.Context())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(job)
            break
        except asyncio.CancelledError:
            cancelled = True
            with state_lock:
                if not state["started"]:
                    # Fence a callable that may already be queued but has not
                    # entered SQL. Even a late scheduler cannot start it now.
                    state["cancel_before_start"] = True
                    finished.set()
                cancel_before_start = state["cancel_before_start"]
            if cancel_before_start and not job.done():
                job.cancel()
            if job.cancelled():
                break
    while not finished.is_set():
        try:
            await asyncio.sleep(.01)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return result


@worker_scope("heartbeat-worker")
async def _consume(control: _Worker, engine, processor, activity) -> None:
    try:
        while not control.stop.is_set():
            # A caller-owned bounded background permit precedes this Session.
            # No request socket or Player admission permit is inherited.
            async with activity():
                if control.stop.is_set():
                    break
                progressed = await _finish_atomic_thread(engine, processor)
            if progressed:
                continue
            control.wake.clear()
            try:
                await asyncio.wait_for(control.wake.wait(), timeout=0.25)
            except TimeoutError:
                pass
    finally:
        _increment(engine, "worker_stops")


def ensure_worker(app, engine, processor: Callable, *, activity=None) -> asyncio.Task:
    """Lazy one-consumer lifecycle. Caller may also invoke at owned startup.

    No DDL and no worker starts merely by importing this module. stop_worker is
    required on lifespan shutdown and when tests replace an application's loop.
    """
    engine = _engine(engine)
    loop = asyncio.get_running_loop()
    workers = getattr(app.state, "heartbeat_inbox_workers", None)
    if workers is None:
        workers = app.state.heartbeat_inbox_workers = {}
    control = workers.get(engine)
    if control is not None and control.task is not None and not control.task.done():
        if control.loop is not loop:
            raise RuntimeError("Stop the existing heartbeat consumer before replacing its event loop")
        control.wake.set()
        return control.task
    control = _Worker(loop=loop, stop=asyncio.Event(), wake=asyncio.Event())
    workers[engine] = control
    control.task = loop.create_task(_consume(control, engine, processor, activity or _no_activity),
                                    name="heartbeat-inbox-consumer", context=contextvars.Context())
    _increment(engine, "worker_starts")
    return control.task


async def stop_worker(app, engine) -> None:
    """Stop before another job; await any in-progress atomic transaction safely."""
    engine = _engine(engine)
    workers = getattr(app.state, "heartbeat_inbox_workers", {})
    control = workers.get(engine)
    if control is None or control.task is None:
        return
    if control.loop is not asyncio.get_running_loop():
        if not control.task.done():
            raise RuntimeError("Heartbeat consumer belongs to another running event loop")
    else:
        control.stop.set()
        control.wake.set()
        try:
            await control.task
        except asyncio.CancelledError:
            if not control.task.done() or not control.task.cancelled():
                raise
    workers.pop(engine, None)
