"""Always-on, bounded-metadata request/PostgreSQL correlation; no diagnostic SQL.

Only transaction boundaries, locking SQL and SQL errors are emitted. Successful
non-locking statements are counted, with their safe shape retained only for a
possible error. Worker scopes have independent technical IDs and log lifecycle
and errors, never idle-poll transactions. Logging cannot alter an HTTP result.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
import hashlib
import hmac
import inspect
import logging
import math
import re
import secrets
import threading
import time
import uuid


_current: ContextVar[_Trace | None] = ContextVar("request_pg_correlation", default=None)
_installed = False
_install_lock = threading.Lock()
_target_key = secrets.token_bytes(32)
_SCOPE_ID = "_request_pg_correlation_id"
_PATHS = {"heartbeat": "POST", "runtime-config": "GET", "playback-events": "POST"}
_ENTITIES = frozenset({"Channel", "Client", "Branch", "PlayerDevice", "tenant_barrier",
                       "heartbeat_capacity", "heartbeat_inbox", "playback_inbox"})
_STAGES = frozenset({"DEPENDENCY", "REQUEST", "RUNTIME_ADMISSION", "RUNTIME_DB", "RUNTIME_TENANT_GUARD", "RUNTIME_BUILDER",
                     "RUNTIME_SERIALIZATION", "RESPONSE_READY", "INITIAL_LOCK", "RELEASE_FOR_WAIT",
                     "WAIT_BEGIN", "WAIT_END", "REVALIDATE_BEGIN", "REVALIDATE_END", "COMMIT", "ROLLBACK", "WORKER"})
_TABLES = frozenset({"channels", "clients", "branches", "player_devices", "player_heartbeats",
                     "player_heartbeat_inbox", "player_playback_events", "player_playback_inbox",
                     "campaigns", "contents", "layouts", "device_outputs", "schedules",
                     "campaign_channel_assignments", "campaign_playlist_items", "videowalls", "videowall_nodes",
                     "videowall_publications", "videowall_publication_assets", "videowall_playback_groups",
                     "videowall_playback_group_members"})
_RUNTIME_REASONS = frozenset({"RUNTIME_TENANT_GUARD_LOCK_UNAVAILABLE", "RUNTIME_CHANNEL_LOCK_UNAVAILABLE",
                             "RUNTIME_DB_ERROR", "RUNTIME_ADMISSION_REJECT", "RUNTIME_TIMEOUT_OR_CANCEL",
                             "RUNTIME_DEPENDENCY_UNAVAILABLE", "RUNTIME_OTHER"})


@dataclass
class _Trace:
    endpoint: str
    method: str
    request_id: str | None = None
    worker_id: str | None = None
    scope: dict | None = None
    stage: str = "DEPENDENCY"
    entity: str | None = None
    target_hmac: str | None = None
    active: bool = True
    request_finished: bool = False
    sql_sequence: int = 0
    pids: set = field(default_factory=set)
    lock: object = field(default_factory=threading.RLock)


def _valid_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def request_uuid(scope):
    """One technical UUID across old/new observers, even without a valid header."""
    existing = scope.get(_SCOPE_ID)
    if _valid_uuid(existing):
        return existing.lower()
    trace = _current.get()
    if trace is not None and trace.active and trace.request_id:
        selected = trace.request_id
    else:
        from app.core.player_diagnostics import _current_trace, _request_uuid
        old = _current_trace.get()
        selected = old.request_id if old is not None and _valid_uuid(old.request_id) else _request_uuid(scope.get("headers", []))
    scope[_SCOPE_ID] = selected
    return selected


def clear_request_context():
    _current.set(None)


def _active():
    trace = _current.get()
    return trace if trace is not None and trace.active else None


def _emit(value):
    try:
        logging.getLogger(__name__).warning(json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    except BaseException:
        pass


def _common(trace, *, stage=None):
    return {"request_id": trace.request_id, "worker_id": trace.worker_id,
            "scope": "worker" if trace.worker_id else "request", "endpoint": trace.endpoint,
            "method": trace.method, "stage": stage if stage in _STAGES else trace.stage,
            "request_finished": trace.request_finished,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")}


def _pid(connection):
    """Read the PID already supplied by libpq; never reconnect or run a query."""
    try:
        proxy = getattr(connection, "_dbapi_connection", None)
        driver = getattr(proxy, "driver_connection", None)
        if driver is None:
            return None
        info = getattr(driver, "info", None)
        value = getattr(info, "backend_pid", None)
        if value is None and callable(getattr(driver, "get_backend_pid", None)):
            value = driver.get_backend_pid()
        return value if type(value) is int and value > 0 else None
    except BaseException:
        return None


def _state(connection, *, create=False):
    trace = _active()
    if trace is None or getattr(getattr(connection, "dialect", None), "name", None) != "postgresql":
        return None
    state = getattr(connection, "_request_pg_diagnostic_state", None)
    if state is None and create:
        state = {"id": str(uuid.uuid4()), "pid": _pid(connection), "owner": trace,
                 "count": 0, "last": None, "ending": None}
        setattr(connection, "_request_pg_diagnostic_state", state)
        if state["pid"] is not None:
            with trace.lock:
                trace.pids.add(state["pid"])
        if not trace.worker_id:
            _emit({"event": "request_pg_transaction_start", **_common(trace),
                   "pg_backend_pid": state["pid"], "db_transaction_id": state["id"]})
    elif state is not None and state["owner"] is not trace:
        # A borrowed transaction can be observed by a different request without
        # pretending a new PostgreSQL transaction was begun or taking ownership.
        state["owner"] = trace
        if state["pid"] is not None:
            with trace.lock:
                trace.pids.add(state["pid"])
        if not trace.worker_id:
            _emit({"event": "request_pg_transaction_attached", **_common(trace),
                   "pg_backend_pid": state["pid"], "db_transaction_id": state["id"]})
    return state


def _database_metadata(db=None, connection=None):
    try:
        candidates = [connection] if connection is not None else []
        if db is not None:
            transaction = db.get_transaction()
            if transaction is not None:
                candidates.extend(value[0] for value in getattr(transaction, "_connections", {}).values())
        states = {}
        for candidate in candidates:
            if candidate is not None:
                state = _state(candidate, create=False)
                if state is not None:
                    states[state["id"]] = state
        if len(states) == 1:
            state = next(iter(states.values()))
            return {"pg_backend_pid": state["pid"], "db_transaction_id": state["id"]}
    except BaseException:
        pass
    return {"pg_backend_pid": None, "db_transaction_id": None}


def _sqlstate(error):
    value = getattr(getattr(error, "orig", None), "sqlstate", None)
    if value is None:
        value = getattr(error, "sqlstate", None)
    return value if type(value) is str and re.fullmatch(r"[0-9A-Z]{5}", value) else None


def _exception_class(error):
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError, TimeoutError as PoolTimeout
    if isinstance(error, PoolTimeout):
        return "SQLALCHEMY_POOL_TIMEOUT"
    if isinstance(error, DBAPIError):
        return "DBAPI_ERROR"
    if isinstance(error, SQLAlchemyError):
        return "SQLALCHEMY_ERROR"
    import asyncio
    if isinstance(error, asyncio.CancelledError):
        return "CANCELLED"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    return "OTHER"


def _shape(statement):
    # Shape only: remove literal values before hashing; raw SQL/parameters never
    # enter a log. Technical identifiers are restricted to a fixed table set.
    sql = statement[:65536] if type(statement) is str else ""
    normalized = re.sub(r"'(?:''|[^'])*'", "?", sql)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "?", normalized)
    normalized = " ".join(normalized.split())
    upper = normalized.upper()
    modes = set(re.findall(r"\bFOR\s+(NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\b", upper))
    lock_mode = "NONE" if not modes else "MULTIPLE" if len(modes) > 1 else "FOR_" + next(iter(modes)).replace(" ", "_")
    if "PG_ADVISORY_XACT_LOCK_SHARED" in upper or "PG_TRY_ADVISORY_XACT_LOCK_SHARED" in upper:
        lock_mode = "ADVISORY_SHARED"
    elif "PG_ADVISORY_XACT_LOCK" in upper or "PG_TRY_ADVISORY_XACT_LOCK" in upper:
        lock_mode = "ADVISORY_EXCLUSIVE"
    targets = set(re.findall(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\s+OF\s+([A-Z_][A-Z_0-9]*)", upper))
    tables = sorted(name for name in _TABLES if re.search(r"\b" + re.escape(name) + r"\b", normalized, re.I))
    operation = upper.split(" ", 1)[0] if upper else "OTHER"
    if operation not in {"SELECT", "UPDATE", "INSERT", "DELETE", "WITH", "SET", "SHOW"}:
        operation = "OTHER"
    return {"sql_fingerprint": hmac.new(_target_key, b"sql:" + normalized.encode(), hashlib.sha256).hexdigest(),
            "sql_fingerprint_scope": "process", "sql_operation": operation,
            "sql_tables": tables, "lock_mode": lock_mode, "lock_nowait": "NOWAIT" in upper,
            "lock_targets": sorted(target.lower() for target in targets if target.lower() in _TABLES),
            "fingerprint_truncated": len(statement) > 65536 if type(statement) is str else False}


def _error_metadata(error, connection=None):
    trace = _active()
    state = _state(connection, create=False) if connection is not None else None
    metadata = {"pg_backend_pid": state["pid"] if state else None,
                "db_transaction_id": state["id"] if state else None,
                "stage": state["ending"] if state and state["ending"] else trace.stage if trace else "OTHER",
                "sqlstate": _sqlstate(error), "exception_class": _exception_class(error)}
    if state and state["last"]:
        metadata.update(state["last"])
    if trace is not None and trace.entity in _ENTITIES:
        entity_table = {"Channel": "channels", "Client": "clients", "Branch": "branches", "PlayerDevice": "player_devices"}.get(trace.entity)
        if entity_table is None or metadata.get("sql_tables") == [entity_table]:
            metadata.update(entity=trace.entity, target_hmac=trace.target_hmac, target_hmac_scope="process")
    if state and state["ending"]:
        metadata.update(sql_operation=state["ending"], sql_tables=[], lock_mode="NONE", lock_targets=[], lock_nowait=False,
                        sql_fingerprint=hmac.new(_target_key, b"sql:" + state["ending"].encode(), hashlib.sha256).hexdigest())
        metadata.pop("db_operation_id", None)
        metadata.pop("entity", None)
        metadata.pop("target_hmac", None)
    return metadata


def failure_metadata(error=None):
    """Only actual exception-chain metadata, never an earlier recovered error."""
    try:
        seen = set()
        current = error
        typed_cause = error
        for _ in range(8):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            value = getattr(current, "_request_pg_failure", None)
            if type(value) is dict:
                return dict(value)
            if _exception_class(current) != "OTHER" and _exception_class(typed_cause) == "OTHER":
                typed_cause = current
            current = current.__cause__ or current.__context__
        trace = _active()
        return {"pg_backend_pid": None, "db_transaction_id": None,
                "stage": trace.stage if trace else "OTHER", "sqlstate": _sqlstate(typed_cause),
                "exception_class": _exception_class(typed_cause)}
    except BaseException:
        return {"pg_backend_pid": None, "db_transaction_id": None, "stage": "OTHER", "sqlstate": None, "exception_class": "OTHER"}


def capture_failure(error, *, db=None, connection=None, source_error=None):
    """Attach current transaction metadata before an existing rollback/raise.

    This is for an existing non-SQL rejection such as a full inbox, or for an
    HTTPException which suppresses the actual database exception's cause.
    """
    try:
        if type(getattr(error, "_request_pg_failure", None)) is dict and source_error is None:
            return error
        trace = _active()
        if source_error is not None:
            metadata = failure_metadata(source_error)
        else:
            metadata = {**_database_metadata(db, connection), "stage": trace.stage if trace else "OTHER",
                        "sqlstate": None, "exception_class": "OTHER"}
        error._request_pg_failure = metadata
    except BaseException:
        pass
    return error


def diag_event(name, *, db=None, connection=None, entity=None, target=None,
               remaining_budget_ms=None, error=None, stage=None, component=None):
    try:
        trace = _active()
        if trace is None or type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", name):
            return
        selected = stage if stage in _STAGES else trace.stage
        if stage in _STAGES:
            trace.stage = stage
        metadata = failure_metadata(error) if error is not None else _database_metadata(db, connection)
        error_stage = metadata.pop("stage", None) if error is not None else None
        event = {"event": name, **_common(trace, stage=selected), **metadata, "stage": selected}
        if error_stage in _STAGES:
            event["error_stage"] = error_stage
        if component in {"FASTAPI_SERIALIZER", "JSON_RENDER"}:
            event["component"] = component
        if entity in _ENTITIES:
            event["entity"] = entity
            trace.entity = entity
            trace.target_hmac = None
            if type(target) is str:
                event["target_hmac"] = hmac.new(_target_key, (entity + ":" + target).encode(), hashlib.sha256).hexdigest()
                event["target_hmac_scope"] = "process"
                trace.target_hmac = event["target_hmac"]
        if remaining_budget_ms is None and trace.endpoint == "heartbeat" and trace.scope is not None:
            origin = trace.scope.get("_player_admission_started_ns")
            if type(origin) is int:
                remaining_budget_ms = (origin + 5_000_000_000 - time.monotonic_ns()) / 1_000_000
        if type(remaining_budget_ms) in (int, float) and math.isfinite(remaining_budget_ms):
            event["remaining_budget_ms"] = round(max(0, min(remaining_budget_ms, 2_147_483_647)), 3)
        _emit(event)
        if name == "runtime_admission_end":
            trace.stage = "DEPENDENCY"
    except BaseException:
        pass


@contextmanager
def diagnostic_stage(name, **kwargs):
    trace = _active()
    old = trace.stage if trace else None
    old_entity = (trace.entity, trace.target_hmac) if trace else None
    selected = name.upper() if type(name) is str and name.upper() in _STAGES else "OTHER"
    if trace:
        trace.stage = selected
        trace.entity = trace.target_hmac = None
    diag_event(name + "_start", stage=selected, **kwargs)
    try:
        yield
    except BaseException as error:
        diag_event(name + "_end", stage=selected, error=error, **kwargs)
        raise
    else:
        diag_event(name + "_end", stage=selected, **kwargs)
    finally:
        if trace:
            trace.stage = old
            trace.entity, trace.target_hmac = old_entity


def tag_failure(error, reason, *, stage=None):
    try:
        if reason in _RUNTIME_REASONS:
            error._runtime_503_reason = reason
        metadata = failure_metadata(error)
        if stage is not None:
            selected = stage.upper()
            if selected in _STAGES and metadata["stage"] == "OTHER":
                metadata["stage"] = selected
        error._request_pg_failure = metadata
    except BaseException:
        pass
    return error


def runtime_reason(error):
    try:
        explicit = getattr(error, "_runtime_503_reason", None)
        if explicit in _RUNTIME_REASONS:
            return explicit
        marker = getattr(error, "_player_503_reason", None)
        if type(marker) is str and marker.startswith("ADMISSION_"):
            return "RUNTIME_ADMISSION_REJECT"
        if marker == "TENANT_GUARD_LOCK_UNAVAILABLE":
            return "RUNTIME_TENANT_GUARD_LOCK_UNAVAILABLE"
        metadata = failure_metadata(error)
        if metadata.get("sqlstate") == "55P03":
            if metadata.get("sql_tables") == ["channels"] and metadata.get("lock_mode") not in {None, "NONE", "MULTIPLE"}:
                return "RUNTIME_CHANNEL_LOCK_UNAVAILABLE"
            if metadata.get("stage") == "RUNTIME_TENANT_GUARD":
                return "RUNTIME_TENANT_GUARD_LOCK_UNAVAILABLE"
            return "RUNTIME_DB_ERROR"
        if metadata["exception_class"] in {"DBAPI_ERROR", "SQLALCHEMY_ERROR", "SQLALCHEMY_POOL_TIMEOUT"}:
            return "RUNTIME_DB_ERROR"
        if metadata["exception_class"] in {"TIMEOUT", "CANCELLED"}:
            return "RUNTIME_TIMEOUT_OR_CANCEL"
    except BaseException:
        pass
    return "RUNTIME_OTHER"


def runtime_handler_entered(request):
    try:
        request.scope["_runtime_handler_entered"] = True
    except BaseException:
        pass


def runtime_exception_observed(request, error):
    try:
        if error.status_code != 503:
            return
        reason = runtime_reason(error)
        if reason == "RUNTIME_OTHER" and not request.scope.get("_runtime_handler_entered", False):
            reason = "RUNTIME_DEPENDENCY_UNAVAILABLE"
        request.scope["_runtime_503_metadata"] = {"runtime_503_reason": reason, **failure_metadata(error)}
    except BaseException:
        pass


def worker_scope(name):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            trace = _Trace(endpoint=name, method="WORKER", worker_id=str(uuid.uuid4()), stage="WORKER")
            token = _current.set(trace)
            diag_event("player_worker_start")
            try:
                return await function(*args, **kwargs)
            except BaseException as error:
                diag_event("player_worker_error", error=error)
                raise
            finally:
                diag_event("player_worker_end")
                trace.active = False
                _current.reset(token)
        return wrapped
    return decorate


class RequestPGCorrelationMiddleware:
    def __init__(self, app, *, api_prefix="/api"):
        self.app = app
        self.paths = {api_prefix.rstrip("/") + "/player/" + endpoint: endpoint for endpoint in _PATHS}

    async def __call__(self, scope, receive, send):
        endpoint = self.paths.get(scope.get("path"))
        if scope.get("type") != "http" or endpoint is None or scope.get("method") != _PATHS[endpoint]:
            await self.app(scope, receive, send)
            return
        try:
            trace = _Trace(endpoint=endpoint, method=scope["method"], request_id=request_uuid(scope), scope=scope)
            token = _current.set(trace)
        except BaseException:
            await self.app(scope, receive, send)
            return
        diag_event("runtime_request_start" if endpoint == "runtime-config" else "player_request_start")
        status_code = None

        async def correlated_send(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status")
                if endpoint == "runtime-config":
                    diag_event("runtime_response_ready", stage="RESPONSE_READY")
                    if status_code == 503:
                        metadata = scope.get("_runtime_503_metadata", {"runtime_503_reason": "RUNTIME_OTHER",
                                    "pg_backend_pid": None, "db_transaction_id": None, "sqlstate": None,
                                    "stage": "RESPONSE_READY", "exception_class": "OTHER"})
                        try:
                            _emit({"event": "runtime_http_503", **_common(trace), **metadata, "status_code": 503,
                                   "retry_after_present": any(key.lower() == b"retry-after" for key, _ in message.get("headers", []))})
                        except BaseException:
                            pass
            await send(message)
        try:
            await self.app(scope, receive, correlated_send)
        finally:
            try:
                with trace.lock:
                    pids = sorted(trace.pids)
                _emit({"event": "player_request_result", **_common(trace), "status_code": status_code,
                       "pg_backend_pids_seen": pids})
            except BaseException:
                pass
            finally:
                trace.request_finished = True
                _current.reset(token)


def _install_hooks():
    from sqlalchemy import event
    from sqlalchemy.engine import Connection, Engine
    import fastapi.routing
    from starlette.responses import JSONResponse

    def began(connection):
        try:
            _state(connection, create=True)
        except BaseException:
            pass

    def before_sql(connection, cursor, statement, parameters, context, many):
        try:
            state = _state(connection, create=True)
            if state is None:
                return
            trace = _active()
            with trace.lock:
                trace.sql_sequence += 1
                sequence = trace.sql_sequence
            metadata = {**_shape(statement), "db_operation_id": sequence}
            state["count"] += 1
            state["last"] = metadata
            context._request_pg_operation = metadata
            if metadata["lock_mode"] != "NONE" and not trace.worker_id:
                _emit({"event": "request_pg_sql_start", **_common(trace), "pg_backend_pid": state["pid"],
                       "db_transaction_id": state["id"], **metadata})
        except BaseException:
            pass

    def after_sql(connection, cursor, statement, parameters, context, many):
        try:
            state = _state(connection, create=False)
            metadata = getattr(context, "_request_pg_operation", None)
            trace = _active()
            if state is not None and metadata is not None and metadata["lock_mode"] != "NONE" and not trace.worker_id:
                _emit({"event": "request_pg_sql_end", **_common(trace), "pg_backend_pid": state["pid"],
                       "db_transaction_id": state["id"], **metadata, "outcome": "ok"})
        except BaseException:
            pass

    def failed(error_context):
        try:
            trace = _active()
            if trace is None:
                return
            error = error_context.sqlalchemy_exception or error_context.original_exception
            metadata = _error_metadata(error, error_context.connection)
            if getattr(error, "_request_pg_failure", None) is None:
                error._request_pg_failure = metadata
            _emit({"event": "request_pg_sql_error", **_common(trace), **metadata})
        except BaseException:
            pass

    event.listen(Engine, "begin", began)
    event.listen(Engine, "before_cursor_execute", before_sql)
    event.listen(Engine, "after_cursor_execute", after_sql)
    event.listen(Engine, "handle_error", failed)

    def ending(original, kind):
        @wraps(original)
        def wrapper(connection, *args, **kwargs):
            state, trace = None, None
            try:
                state = _state(connection, create=False)
                trace = _active()
                if state is not None:
                    state["ending"] = kind
            except BaseException:
                state, trace = None, None
            outcome = "ok"
            try:
                return original(connection, *args, **kwargs)
            except BaseException:
                outcome = "error"
                raise
            finally:
                try:
                    if state is not None:
                        if outcome == "ok" and kind == "COMMIT" and trace is not None and trace.endpoint == "heartbeat":
                            diag_event("heartbeat_guard_stage", stage="COMMIT", connection=connection)
                        if trace is not None and not trace.worker_id:
                            _emit({"event": "request_pg_transaction_end", **_common(trace, stage=kind),
                                   "pg_backend_pid": state["pid"], "db_transaction_id": state["id"],
                                   "operation": kind, "outcome": outcome, "sql_count": state["count"],
                                   "boundary": "after_dbapi_call"})
                        setattr(connection, "_request_pg_diagnostic_state", None)
                except BaseException:
                    pass
        return wrapper

    Connection._commit_impl = ending(Connection._commit_impl, "COMMIT")
    Connection._rollback_impl = ending(Connection._rollback_impl, "ROLLBACK")

    original_serialize = fastapi.routing.serialize_response
    @wraps(original_serialize)
    async def serialize(*args, **kwargs):
        trace = _active()
        if trace is not None and trace.endpoint == "runtime-config":
            with diagnostic_stage("runtime_serialization", component="FASTAPI_SERIALIZER"):
                return await original_serialize(*args, **kwargs)
        return await original_serialize(*args, **kwargs)
    fastapi.routing.serialize_response = serialize

    original_render = JSONResponse.render
    @wraps(original_render)
    def render(response, content):
        trace = _active()
        if trace is not None and trace.endpoint == "runtime-config":
            with diagnostic_stage("runtime_serialization", component="JSON_RENDER"):
                return original_render(response, content)
        return original_render(response, content)
    JSONResponse.render = render


def install(app, *, api_prefix="/api"):
    """No new setting: light observation is enabled for the three Player routes."""
    global _installed
    with _install_lock:
        if getattr(app.state, "request_pg_correlation_installed", False):
            return
        if not _installed:
            _install_hooks()
            _installed = True
        app.add_middleware(RequestPGCorrelationMiddleware, api_prefix=api_prefix)
        app.state.request_pg_correlation_installed = True
