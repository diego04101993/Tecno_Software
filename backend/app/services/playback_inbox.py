"""Playback-specific durable receipt ingress and one owned consumer.

No schema creation, startup side effects, network access, raw payload logging or
client-submission deduplication. A successful ACK follows the inbox COMMIT.
The processor supplies the existing authoritative sanitization/write guard.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager, contextmanager
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
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.entities import Channel, PlayerDevice, PlayerPlaybackInbox
from app.core.playback_failure_diagnostics import storage_503, tagged_503
from app.core.request_pg_diagnostics import playback_receipt_scope, playback_worker_event, receipt_media_deferred, worker_scope
from app.schemas.domain import PlayerPlaybackEventAck, PlayerPlaybackEventCreate
from app.services.tenancy import require_active_channel_tenant
from app.services.tenant_mutations import (
    MutationIntent, PlayerPlaybackReceiptBusy, PlayerPlaybackReceiptUnavailable, PlayerReceiptMediaDeferred,
    locked_player_playback_receipt, player_write_guard,
)
from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import conflict


MAX_PENDING_COUNT = 64
MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 5
_RETRY_SECONDS = (1, 2, 4, 8)
# Only retry a NOWAIT scope-lock conflict before COMMIT, releasing every DB lock
# first. Nine attempts / at most 2.55s backoff; no extra admission queue or thread.
_PARENT_LOCK_BACKOFF = (0, .01, .02, .04, .08, .16, .32, .64, 1.28)
_CAPACITY_KEY = int.from_bytes(hashlib.sha256(b"tecno.playback.inbox.capacity.v1").digest()[:8], "big", signed=True)
_WORKER_KEY = int.from_bytes(hashlib.sha256(b"tecno.playback.inbox.worker.v1").digest()[:8], "big", signed=True)
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


def _payload_json(payload: PlayerPlaybackEventCreate) -> tuple[dict, str, int]:
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
        raise RuntimeError("Playback inbox requires PostgreSQL or the isolated SQLite test substitute")


def enqueue(db: Session, device: PlayerDevice, channel: Channel,
            payload: PlayerPlaybackEventCreate) -> PlayerPlaybackEventAck:
    """Commit one new receipt, then ACK. No optional raw ID is authoritative.

    Authentication belongs to the existing route dependency. player_write_guard
    rechecks current persistent scope and incoming JSON never supplies ownership.
    """
    bind = db.get_bind()
    _increment(bind, "enqueue_attempts")
    try:
        value, _, payload_bytes = _payload_json(payload)
    except (ValueError, TypeError, UnicodeError):
        _increment(bind, "invalid_payload")
        raise HTTPException(status_code=422, detail="Invalid playback event payload") from None

    identity = str(uuid4())
    accepted_at = datetime.now(UTC)
    scope = (device.id, channel.id, channel.client_id, channel.branch_id)
    authenticated_hash, channel_code = device.player_token_hash, channel.channel_code
    values = dict(player_device_id=scope[0], channel_id=scope[1], client_id=scope[2], branch_id=scope[3],
                  payload_json=value, payload_bytes=payload_bytes, accepted_at=accepted_at,
                  status="PENDING", attempts=0, available_at=accepted_at, last_error_code=None)
    intents = (
        MutationIntent("UPDATE", RowRef("PlayerDevice", device.id), ()),
        MutationIntent("CREATE", RowRef("PlayerPlaybackInbox", identity), tuple(values.items())),
    )
    for attempt, delay in enumerate(_PARENT_LOCK_BACKOFF):
        if attempt:
            time.sleep(delay)
        try:
            if attempt:
                # Rollback expired ORM rows. Reload, but never silently authenticate
                # against a new token, moved scope or changed channel identity.
                device = db.get(PlayerDevice, scope[0], populate_existing=True)
                channel = db.get(Channel, scope[1], populate_existing=True)
                if (device is None or channel is None or not device.is_active
                        or device.player_token_hash != authenticated_hash
                        or channel.channel_code != channel_code
                        or (device.id, device.channel_id, device.client_id, device.branch_id) != scope
                        or (channel.id, channel.client_id, channel.branch_id) != scope[1:]):
                    raise conflict()
            with _capacity_serialization(db):
                count, total_bytes = db.execute(select(func.count(), func.coalesce(func.sum(
                    PlayerPlaybackInbox.payload_bytes), 0)).select_from(PlayerPlaybackInbox)).one()
                # FAILED receipts still consume both budgets; no TTL or silent eviction.
                if int(count) >= MAX_PENDING_COUNT or int(total_bytes) + payload_bytes > MAX_PENDING_BYTES:
                    _increment(bind, "capacity_rejections")
                    raise tagged_503(HTTPException(status_code=503, detail="Playback receipt storage is temporarily unavailable"),
                                     "PLAYBACK_INBOX_FULL", "COUNT_OR_BYTE_CAPACITY", db=db)
                with player_write_guard(db, channel=channel, player_device=device,
                                        operation="create_player_playback_event", intents=intents,
                                        first_playback_acceptance=attempt == 0):
                    db.add(PlayerPlaybackInbox(id=identity, **values))
                    db.commit()
            break
        except PlayerPlaybackReceiptBusy as error:
            db.rollback()
            if attempt == len(_PARENT_LOCK_BACKOFF) - 1:
                _increment(bind, "parent_lock_rejections")
                raise tagged_503(HTTPException(status_code=503, detail="Player operation temporarily unavailable"),
                                 "PLAYBACK_DB_LOCK_UNAVAILABLE", "PARENT_SCOPE_LOCK_RETRY_EXHAUSTED",
                                 db=db, source_error=error) from None
            _increment(bind, "parent_lock_retries")
        except HTTPException:
            db.rollback()
            raise
        except SQLAlchemyError as error:
            db.rollback()
            _increment(bind, "enqueue_storage_errors")
            raise storage_503(HTTPException(status_code=503, detail="Playback receipt storage is temporarily unavailable"),
                              error, db=db) from None

    # Build from captured scalar values: do not reopen an expired ORM row after
    # COMMIT or acquire a second connection only to construct this receipt ACK.
    _increment(bind, "accepted_commits")
    return PlayerPlaybackEventAck(event_id=identity, recorded_at=accepted_at, channel_id=scope[1])


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


def _failure_code(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, _TargetDeleted):
        return "TARGET_DELETED", True
    if isinstance(exc, PlayerPlaybackReceiptBusy):
        return "TARGET_TEMPORARILY_LOCKED", False
    if isinstance(exc, (_AuthorityChanged, ValidationError, ValueError, TypeError, UnicodeError)):
        return "AUTHORITY_OR_RECEIPT_INVALID", True
    if isinstance(exc, HTTPException):
        return ("AUTHORITY_OR_VALIDATION_REJECTED", True) if exc.status_code in {401, 403, 404, 409, 422} else (
            "PROCESSOR_TEMPORARILY_UNAVAILABLE", False)
    if isinstance(exc, SQLAlchemyError):
        return "DATABASE_TEMPORARILY_UNAVAILABLE", False
    return "PROCESSOR_ERROR", False


def _retain_failure(engine, receipt: _Receipt, code: str, terminal: bool) -> bool:
    # The failed processing transaction was rolled back first. This transaction
    # changes only private operational retry fields, never historical IDs/payload.
    try:
        playback_worker_event("PLAYBACK_RETAIN_FAILURE", reason=code)
        with Session(engine, autoflush=False) as db:
            row = db.scalar(select(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id == receipt.identity)
                            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if row is None or row.status != "PENDING":
                return False
            if not receipt.matches(row):
                terminal, code = True, "IMMUTABLE_RECEIPT_CHANGED"
            elif terminal and code.startswith("AUTHORITY_"):
                from app.models.entities import Branch, Client
                # A DELETE may win after the unlocked peek but before the live
                # event guard. Classify from a new transaction, not stale ORM.
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
            playback_worker_event("PLAYBACK_RETAIN_FAILURE", db=db, reason=code, receipt_status=row.status)
            db.commit()
            _increment(engine, metric)
            return True
    except SQLAlchemyError:
        # The original durable receipt is retained by transaction rollback.
        _increment(engine, "failure_record_storage_errors")
        return False


def _defer_media(engine, receipt: _Receipt, error: PlayerReceiptMediaDeferred) -> bool:
    # Parent/media locks were released before opening this receipt-only Session.
    # Preserve the original acceptance, scope, payload and real failure budget.
    try:
        with Session(engine, autoflush=False) as db:
            row = db.scalar(select(PlayerPlaybackInbox).where(PlayerPlaybackInbox.id == receipt.identity)
                            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if row is None or row.status != "PENDING":
                return False
            changed = not receipt.matches(row)
            if not changed:
                row.available_at = datetime.now(UTC) + timedelta(seconds=_RETRY_SECONDS[0])
                db.commit()
                _increment(engine, "media_deferrals")
                receipt_media_deferred("playback", receipt.identity, error=error,
                    pending_age_seconds=max(0.0, (datetime.now(UTC) - receipt.accepted_at).total_seconds()),
                    defer_seconds=_RETRY_SECONDS[0])
                return True
        _increment(engine, "processing_failures")
        return _retain_failure(engine, receipt, "IMMUTABLE_RECEIPT_CHANGED", True)
    except SQLAlchemyError as exc:
        _increment(engine, "deferral_storage_errors")
        playback_worker_event("PLAYBACK_RETAIN_FAILURE", error=exc, reason="DATABASE_TEMPORARILY_UNAVAILABLE")
        return False


def materialize_one(engine, processor: Callable) -> bool:
    """Process at most one receipt; each call owns and closes its own Session.

    Peek without locking. The processor obtains existing parent/tenant locks and
    complete proof FIRST. Its before_commit callback then claims/deletes the
    receipt in the SAME transaction as the final event INSERT/COMMIT. Never lock
    an inbox row before parent locks: enqueue uses parent->incoming-inbox order.
    """
    # A request Session may be bound to a Connection in an isolated fixture.
    # The consumer always owns a fresh engine-backed Session, never that socket.
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
    observation = ExitStack()
    try:
        with Session(engine, autoflush=False) as db:
            if engine.dialect.name == "postgresql" and not db.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(:key)"), {"key": _WORKER_KEY}):
                _increment(engine, "materializer_busy")
                return False
            row = db.scalar(select(PlayerPlaybackInbox).where(
                PlayerPlaybackInbox.status == "PENDING", PlayerPlaybackInbox.available_at <= datetime.now(UTC))
                .order_by(PlayerPlaybackInbox.accepted_at, PlayerPlaybackInbox.id).limit(1))
            if row is None:
                return False
            receipt = _Receipt.capture(row)
            observation.enter_context(playback_receipt_scope(receipt.identity, row.attempts + 1, db=db))
            playback_worker_event("PLAYBACK_RECEIPT_SELECTED", db=db, receipt_status=row.status)
            payload = PlayerPlaybackEventCreate.model_validate(json.loads(receipt.canonical_payload))
            playback_worker_event("PLAYBACK_VALIDATE_SCOPE", db=db)
            device = db.get(PlayerDevice, receipt.scope[0])
            channel = db.get(Channel, receipt.scope[1])
            if device is None or channel is None:
                raise _TargetDeleted()
            if (not device.is_active
                    or (device.channel_id, device.client_id, device.branch_id) != receipt.scope[1:]
                    or (channel.id, channel.client_id, channel.branch_id) != receipt.scope[1:]):
                raise _AuthorityChanged()
            require_active_channel_tenant(channel, db)
            claimed = False

            def before_commit(plan):
                nonlocal claimed
                playback_worker_event("PLAYBACK_CLAIM_RECEIPT", db=db)
                with locked_player_playback_receipt(db, plan=plan, receipt_id=receipt.identity,
                                                    channel=channel, player_device=device) as current:
                    if current is None or current.status != "PENDING":
                        raise _NoLongerAvailable()
                    if not receipt.matches(current):
                        raise _AuthorityChanged()
                    # A second consumer may have consumed the unlocked peek.
                    # The typed helper requires the live parent-first guard.
                    db.delete(current)
                    claimed = True

            playback_worker_event("PLAYBACK_MATERIALIZE", db=db)
            processor(payload, device, db, event_id=receipt.identity,
                      recorded_at=receipt.accepted_at, before_commit=before_commit)
            if not claimed:
                raise RuntimeError("Playback processor did not claim its durable receipt")
            playback_worker_event("PLAYBACK_COMMITTED", receipt_status="MATERIALIZED")
            _increment(engine, "materialized_commits")
            return True
    except (_NoLongerAvailable, PlayerPlaybackReceiptUnavailable):
        _increment(engine, "receipt_claim_misses")
        return False
    except PlayerReceiptMediaDeferred as exc:
        if receipt is None:
            raise
        return _defer_media(engine, receipt, exc)
    except Exception as exc:
        # The Session context closed/rolled back before private retry marking.
        _increment(engine, "processing_failures")
        if receipt is None:
            _increment(engine, "candidate_read_errors")
            return False
        code, terminal = _failure_code(exc)
        playback_worker_event("PLAYBACK_MATERIALIZE", error=exc, reason=code)
        return _retain_failure(engine, receipt, code, terminal)
    finally:
        observation.close()


def snapshot(db: Session) -> dict[str, int | float | None]:
    """Small private table aggregate; no raw IDs/payloads or historical scan."""
    count, payload_bytes, oldest, oldest_pending = db.execute(select(func.count(), func.coalesce(func.sum(
        PlayerPlaybackInbox.payload_bytes), 0), func.min(PlayerPlaybackInbox.accepted_at),
        func.min(case((PlayerPlaybackInbox.status == "PENDING", PlayerPlaybackInbox.accepted_at))))
        .select_from(PlayerPlaybackInbox)).one()
    failed = db.scalar(select(func.count()).select_from(PlayerPlaybackInbox).where(
        PlayerPlaybackInbox.status == "FAILED"))
    return {"pending_count": int(count), "pending_payload_bytes": int(payload_bytes),
            "failed_count": int(failed),
            "oldest_age_seconds": max(0.0, (datetime.now(UTC) - _utc(oldest)).total_seconds()) if oldest else None,
             "oldest_pending_age_seconds": max(0.0, (datetime.now(UTC) - _utc(oldest_pending)).total_seconds()) if oldest_pending else None,
             "media_deferrals": 0, "deferral_storage_errors": 0,
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


@worker_scope("playback-worker")
async def _consume(control: _Worker, engine, processor, activity) -> None:
    try:
        while not control.stop.is_set():
            # The caller supplies the same fair GLOBALHEAVY4 activity gate used
            # by HB/runtime. Acquire BEFORE opening any consumer DB Session.
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
    workers = getattr(app.state, "playback_inbox_workers", None)
    if workers is None:
        workers = app.state.playback_inbox_workers = {}
    control = workers.get(engine)
    if control is not None and control.task is not None and not control.task.done():
        if control.loop is not loop:
            raise RuntimeError("Stop the existing playback consumer before replacing its event loop")
        control.wake.set()
        return control.task
    control = _Worker(loop=loop, stop=asyncio.Event(), wake=asyncio.Event())
    workers[engine] = control
    control.task = loop.create_task(_consume(control, engine, processor, activity or _no_activity),
                                    name="playback-inbox-consumer", context=contextvars.Context())
    _increment(engine, "worker_starts")
    return control.task


async def stop_worker(app, engine) -> None:
    """Stop before another job; await any in-progress atomic transaction safely."""
    engine = _engine(engine)
    workers = getattr(app.state, "playback_inbox_workers", {})
    control = workers.get(engine)
    if control is None or control.task is None:
        return
    if control.loop is not asyncio.get_running_loop():
        if not control.task.done():
            raise RuntimeError("Playback consumer belongs to another running event loop")
    else:
        control.stop.set()
        control.wake.set()
        try:
            await control.task
        except asyncio.CancelledError:
            if not control.task.done() or not control.task.cancelled():
                raise
    workers.pop(engine, None)
