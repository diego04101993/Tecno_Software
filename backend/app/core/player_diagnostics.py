"""Opt-in, process-local Player timing diagnostics, without business data.

Enable before importing the application with ``PLAYER_DIAGNOSTICS=1``. Disabled
decorators return the original callable and ``install`` installs no hooks. The
module never logs arguments, URLs, SQL, parameters, response bodies, entity IDs,
or exception messages. Only the explicitly allowed endpoint names and a request
UUID are included. Timings use the same process monotonic clock.

SQL and pool hooks are process-wide, but record only inside the three diagnostic
Player requests. This includes their read-snapshot engines and media NullPools.
POOL_ACQUIRE measures Pool.connect(), including connection/recycle/validation,
rather than pretending that a checkout event measures acquisition wait alone.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Iterator
import uuid
import weakref


ENABLED = os.environ.get("PLAYER_DIAGNOSTICS") == "1"
PATHS = frozenset({
    "/api/player/runtime-config",
    "/api/player/heartbeat",
    "/api/player/playback-events",
})
_METHODS_BY_PATH = {
    "/api/player/runtime-config": "GET",
    "/api/player/heartbeat": "POST",
    "/api/player/playback-events": "POST",
}
STAGES = (
    "AUTH", "HANDLER", "OWNERSHIP", "MEDIA_VALIDATION", "ADMISSION",
    "POOL_ACQUIRE", "SQL", "TRANSACTION", "COMMIT", "SERIALIZATION",
    "JSON_RENDER", "OTHER",
)
_MAX_SPANS = 512
_MAX_ACTIVE_SPANS = 2048
_ADMISSION_CAPACITY = 4
_EXCLUSIVE_PRIORITY = (
    "ADMISSION", "AUTH", "COMMIT", "POOL_ACQUIRE", "SQL",
    "MEDIA_VALIDATION", "OWNERSHIP", "SERIALIZATION", "JSON_RENDER",
)
_current_trace: ContextVar[_Trace | None] = ContextVar("player_diagnostic_trace", default=None)
_emit_lock = threading.Lock()
_install_lock = threading.Lock()
_hooks_installed = False


def _emit(event: dict[str, Any]) -> None:
    """A failed diagnostic sink must never change an application response."""
    try:
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
        with _emit_lock:
            sys.stdout.write(line)
            sys.stdout.flush()
    except BaseException:
        pass


class _Span:
    __slots__ = ("trace", "sequence", "stage", "start_ns", "closed")

    def __init__(self, trace: _Trace, sequence: int, stage: str, start_ns: int) -> None:
        self.trace = trace
        self.sequence = sequence
        self.stage = stage
        self.start_ns = start_ns
        self.closed = False


class _Trace:
    """One shared object survives AnyIO's copied thread ContextVars safely."""

    def __init__(self, request_id: str, path: str, method: str, accepted_ns: int | None = None) -> None:
        self.request_id = request_id
        self.path = path
        self.method = method if method in {"GET", "POST"} else "OTHER"
        self.start_ns = accepted_ns if accepted_ns is not None else time.monotonic_ns()
        self.lock = threading.RLock()
        self.sequence = 0
        self.active: dict[int, _Span] = {}
        self.records: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.totals_ns: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()
        self.first_start_ns: dict[str, int] = {}
        self.last_end_ns: dict[str, int] = {}
        self.exclusive_ns: Counter[str] = Counter()
        self.exclusive_cursor_ns = self.start_ns
        self.milestones: dict[str, int] = {"REQUEST_ACCEPTED": self.start_ns}
        self.snapshots: list[dict[str, Any]] = []
        self.finished = False

    def _account_exclusive(self, now_ns: int) -> None:
        active_stages = {span.stage for span in self.active.values()}
        selected = next((stage for stage in _EXCLUSIVE_PRIORITY if stage in active_stages), "OTHER")
        self.exclusive_ns[selected] += max(0, now_ns - self.exclusive_cursor_ns)
        self.exclusive_cursor_ns = now_ns

    def checkpoint(self, name: str) -> None:
        with self.lock:
            if self.finished:
                return
            now_ns = time.monotonic_ns()
            self.milestones.setdefault(name, now_ns)
            if len(self.snapshots) < 8:
                self.snapshots.append({
                    "name": name,
                    "monotonic_ns": now_ns,
                    "counters": _counter_snapshot(),
                    "anyio_limiter": _limiter_snapshot(),
                })

    def begin(self, stage: str) -> _Span | None:
        if stage not in STAGES or stage == "OTHER":
            return None
        with self.lock:
            if self.finished or len(self.active) >= _MAX_ACTIVE_SPANS:
                return None
            now_ns = time.monotonic_ns()
            self._account_exclusive(now_ns)
            self.sequence += 1
            span = _Span(self, self.sequence, stage, now_ns)
            self.active[span.sequence] = span
            self.first_start_ns.setdefault(stage, now_ns)
            self.milestones.setdefault("SQL_TOTAL_START" if stage == "SQL" else stage + "_START", now_ns)
            if stage == "POOL_ACQUIRE":
                self.milestones.setdefault("DB_CHECKOUT_START", now_ns)
            if stage == "HANDLER":
                self.milestones.setdefault("HANDLER_ENTER", now_ns)
            return span

    def end(self, span: _Span | None, outcome: str = "ok", end_ns: int | None = None) -> None:
        if span is None:
            return
        with self.lock:
            if span.closed or self.finished:
                return
            now_ns = end_ns if end_ns is not None else time.monotonic_ns()
            self._account_exclusive(now_ns)
            span.closed = True
            self.active.pop(span.sequence, None)
            duration_ns = max(0, now_ns - span.start_ns)
            self.counts[span.stage] += 1
            self.totals_ns[span.stage] += duration_ns
            self.last_end_ns[span.stage] = now_ns
            self.milestones["SQL_TOTAL_END" if span.stage == "SQL" else span.stage + "_END"] = now_ns
            if span.stage == "POOL_ACQUIRE":
                self.milestones["DB_CHECKOUT_END"] = now_ns
            if outcome != "ok":
                self.errors[span.stage] += 1
            if len(self.records) < _MAX_SPANS:
                self.records.append({
                    "stage": span.stage,
                    "start_ns": span.start_ns,
                    "end_ns": now_ns,
                    "outcome": outcome if outcome in {"ok", "error", "cancelled", "unfinished"} else "error",
                })
            else:
                self.dropped[span.stage] += 1

    def finish(self, status_code: int, response_started: bool, response_completed: bool, failed: bool) -> dict[str, Any]:
        with self.lock:
            end_ns = time.monotonic_ns()
            for span in list(self.active.values()):
                self.end(span, "unfinished", end_ns)
            self._account_exclusive(end_ns)
            self.milestones["REQUEST_END"] = end_ns
            self.finished = True
            records = sorted(self.records, key=lambda record: (record["start_ns"], record["end_ns"]))
            stages: dict[str, Any] = {}
            for stage in STAGES:
                if stage == "OTHER":
                    continue
                count = self.counts[stage]
                stages[stage] = {
                    "status": "observed" if count else "stage_not_applicable",
                    "count": count,
                    "errors": self.errors[stage],
                    "total_ms": round(self.totals_ns[stage] / 1_000_000, 6) if count else None,
                    "start_ns": self.first_start_ns.get(stage),
                    "end_ns": self.last_end_ns.get(stage),
                    "spans": [record for record in records if record["stage"] == stage],
                    "spans_dropped": self.dropped[stage],
                }
            # HANDLER and TRANSACTION are envelopes, not attribution categories.
            # Account intervals online at every transition, with explicit
            # priority, so nested spans are never subtracted twice and bounded
            # detail truncation does not affect exact exclusive totals.
            other_ns = self.exclusive_ns["OTHER"]
            stages["OTHER"] = {
                "status": "observed",
                "count": 1,
                "errors": 0,
                "total_ms": round(other_ns / 1_000_000, 6),
                "spans": self._other_intervals(records, end_ns) if not self.dropped else [],
                "spans_dropped": 0,
                "timeline_complete": not bool(self.dropped),
            }
            return {
                "event": "player_request_diagnostics",
                "version": 1,
                "pid": os.getpid(),
                "request_id": self.request_id,
                "endpoint": self.path,
                "method": self.method,
                "start_ns": self.start_ns,
                "end_ns": end_ns,
                "duration_ms": round((end_ns - self.start_ns) / 1_000_000, 6),
                "status_code": status_code,
                "response_started": response_started,
                "response_completed": response_completed,
                "failed": failed,
                "milestones": self.milestones,
                "sql_total": {
                    "start_ns": self.first_start_ns.get("SQL"),
                    "end_ns": self.last_end_ns.get("SQL"),
                    "count": self.counts["SQL"],
                    "elapsed_ms": round((self.last_end_ns["SQL"] - self.first_start_ns["SQL"]) / 1_000_000, 6) if self.counts["SQL"] else None,
                    "execution_ms": stages["SQL"]["total_ms"],
                },
                "sql_statement_count": self.counts["SQL"],
                "exclusive_breakdown_ms": {
                    stage: round(self.exclusive_ns[stage] / 1_000_000, 6)
                    for stage in (*_EXCLUSIVE_PRIORITY, "OTHER")
                },
                "exclusive_priority": list(_EXCLUSIVE_PRIORITY),
                "checkpoints": self.snapshots,
                "stages": stages,
                "timeline": records,
                "timeline_limit": _MAX_SPANS,
                "timeline_truncated": bool(self.dropped),
            }

    def _other_intervals(self, records: list[dict[str, Any]], end_ns: int) -> list[dict[str, Any]]:
        cursor_ns = self.start_ns
        intervals = []
        for record in records:
            if record["stage"] not in _EXCLUSIVE_PRIORITY:
                continue
            start_ns = max(self.start_ns, record["start_ns"])
            stop_ns = min(end_ns, record["end_ns"])
            if start_ns > cursor_ns:
                intervals.append({"stage": "OTHER", "start_ns": cursor_ns, "end_ns": start_ns, "outcome": "ok"})
            cursor_ns = max(cursor_ns, stop_ns)
        if cursor_ns < end_ns:
            intervals.append({"stage": "OTHER", "start_ns": cursor_ns, "end_ns": end_ns, "outcome": "ok"})
        return intervals


def _start(stage: str) -> _Span | None:
    try:
        trace = _current_trace.get()
        return trace.begin(stage) if trace is not None else None
    except BaseException:
        return None


def _end(span: _Span | None, outcome: str = "ok") -> None:
    try:
        if span is not None:
            span.trace.end(span, outcome)
    except BaseException:
        pass


def _checkpoint(name: str) -> None:
    try:
        trace = _current_trace.get()
        if trace is not None:
            trace.checkpoint(name)
    except BaseException:
        pass


@contextmanager
def diagnostic_span(stage: str) -> Iterator[None]:
    span = _start(stage) if ENABLED else None
    try:
        yield
    except BaseException:
        _end(span, "error")
        raise
    else:
        _end(span)


def observed(stage: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Preserve callable/signature and propagate every original exception."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        if not ENABLED:
            return function
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with diagnostic_span(stage):
                    return await function(*args, **kwargs)
            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with diagnostic_span(stage):
                return function(*args, **kwargs)
        return sync_wrapper
    return decorate


class _AdmissionTicket:
    def __init__(self, span: _Span | None) -> None:
        self.span = span
        self.state = "waiting"


class _ProcessState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.inflight = 0
        self.requests_started = 0
        self.requests_completed = 0
        self.admission_waiters = 0
        self.admission_active = 0
        self.admission_started = 0
        self.admission_acquired_count = 0
        self.admission_released_count = 0
        self.admission_cancelled_count = 0
        self.pool_acquiring = 0
        self.pool_attempts = 0
        self.pool_errors = 0
        self.primary_engine: Any = None
        self.admission_snapshot: Any = None
        self.samplers: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


_state = _ProcessState()


def admission_wait_start() -> _AdmissionTicket | None:
    if not ENABLED:
        return None
    try:
        ticket = _AdmissionTicket(_start("ADMISSION"))
        with _state.lock:
            _state.admission_waiters += 1
            _state.admission_started += 1
        _checkpoint("ADMISSION_WAIT_START")
        return ticket
    except BaseException:
        return None


def admission_grant_recorder() -> Callable[[int], None] | None:
    """Bind an exact permit-grant timestamp to the enqueuing request.

    Dispatch can run in another request's task when that task releases its
    permit. Capture the trace now rather than reading its ContextVar there.
    ADMISSION_ACQUIRED remains the later wake-up checkpoint, preserving the
    existing span while allowing grant wait and event-loop resume delay to be
    measured separately. This recorder never affects admission policy.
    """
    if not ENABLED:
        return None
    trace = _current_trace.get()
    if trace is None:
        return None

    def record(granted_ns: int) -> None:
        try:
            if type(granted_ns) is not int:
                return
            with trace.lock:
                if trace.finished or granted_ns < trace.start_ns:
                    return
                trace.milestones.setdefault("ADMISSION_GRANTED_MONOTONIC", granted_ns)
        except BaseException:
            # A diagnostic failure must not take or release a permit.
            pass

    return record


def admission_acquired(ticket: _AdmissionTicket | None) -> None:
    if not ENABLED or ticket is None:
        return
    try:
        with _state.lock:
            if ticket.state != "waiting":
                return
            ticket.state = "active"
            _state.admission_waiters -= 1
            _state.admission_active += 1
            _state.admission_acquired_count += 1
        _end(ticket.span)
        _checkpoint("ADMISSION_ACQUIRED")
    except BaseException:
        pass


def admission_released(ticket: _AdmissionTicket | None) -> None:
    """Call in an outer finally, including cancellation before acquisition."""
    if not ENABLED or ticket is None:
        return
    try:
        with _state.lock:
            if ticket.state == "active":
                _state.admission_active -= 1
                _state.admission_released_count += 1
            elif ticket.state == "waiting":
                _state.admission_waiters -= 1
                _state.admission_cancelled_count += 1
            else:
                return
            previous_state = ticket.state
            ticket.state = "released"
        if previous_state == "waiting":
            _end(ticket.span, "cancelled")
        _checkpoint("ADMISSION_RELEASED")
    except BaseException:
        pass


def _pool_snapshot(engine: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        pool = engine.pool
        for key in ("size", "checkedin", "checkedout", "overflow"):
            method = getattr(pool, key, None)
            if callable(method):
                value = method()
                if isinstance(value, int):
                    result[key] = value
    except BaseException:
        result["snapshot_unavailable"] = True
    return result


def _process_resources() -> dict[str, Any]:
    result: dict[str, Any] = {"python_thread_count": threading.active_count()}
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            process_handle = ctypes.windll.kernel32.GetCurrentProcess()
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            memory_info.restype = wintypes.BOOL
            if memory_info(process_handle, ctypes.byref(counters), counters.cb):
                result["rss_bytes"] = int(counters.WorkingSetSize)
            # Python threads are always available. Native thread count is read
            # from the OS snapshot without opening another process or handle.
            class ThreadEntry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                ]

            kernel = ctypes.windll.kernel32
            kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
            kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
            if snapshot not in (None, ctypes.c_void_p(-1).value):
                try:
                    entry = ThreadEntry()
                    entry.dwSize = ctypes.sizeof(entry)
                    count = 0
                    more = kernel.Thread32First(snapshot, ctypes.byref(entry))
                    while more:
                        if entry.th32OwnerProcessID == os.getpid():
                            count += 1
                        more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
                    result["thread_count"] = count
                finally:
                    kernel.CloseHandle(snapshot)
        elif sys.platform.startswith("linux"):
            with open("/proc/self/status", "r", encoding="ascii") as status_file:
                for line in status_file:
                    if line.startswith("VmRSS:"):
                        result["rss_bytes"] = int(line.split()[1]) * 1024
                    elif line.startswith("Threads:"):
                        result["thread_count"] = int(line.split()[1])
    except BaseException:
        result["resource_snapshot_unavailable"] = True
    return result


def _counter_snapshot(engine: Any = None) -> dict[str, Any]:
    with _state.lock:
        result = {
            "event": "player_process_diagnostics",
            "version": 1,
            "pid": os.getpid(),
            "monotonic_ns": time.monotonic_ns(),
            "inflight": _state.inflight,
            "requests_started": _state.requests_started,
            "requests_completed": _state.requests_completed,
            "admission_capacity": _ADMISSION_CAPACITY,
            "admission_waiters": _state.admission_waiters,
            "admission_active": _state.admission_active,
            "admission_started": _state.admission_started,
            "admission_acquired": _state.admission_acquired_count,
            "admission_released": _state.admission_released_count,
            "admission_cancelled": _state.admission_cancelled_count,
            "pool_acquiring": _state.pool_acquiring,
            "pool_attempts": _state.pool_attempts,
            "pool_errors": _state.pool_errors,
            "python_thread_count": threading.active_count(),
        }
        selected_engine = engine if engine is not None else _state.primary_engine
    result["primary_pool"] = _pool_snapshot(selected_engine)
    provider = _state.admission_snapshot
    if provider is not None:
        try:
            lanes = provider()
            # Keep HTTP ticket accounting separately from the actual resource
            # gate. Durable materialization also occupies that four-slot gate;
            # fast ingress has its own small persistence lane.
            result["http_admission"] = {key: result[key] for key in (
                "admission_active", "admission_waiters", "admission_started",
                "admission_acquired", "admission_released", "admission_cancelled")}
            result["player_admission"] = lanes
            result["admission_capacity"] = lanes["expensive"]["capacity"]
            result["admission_active"] = lanes["expensive"]["active"]
            result["admission_waiters"] = lanes["expensive"]["waiters"]
        except Exception:
            # Observability must never change the HTTP or worker outcome.
            pass
    return result


def _process_snapshot(engine: Any = None) -> dict[str, Any]:
    result = _counter_snapshot(engine)
    result.update(_process_resources())
    return result


def _limiter_snapshot() -> dict[str, Any]:
    try:
        import anyio.to_thread
        limiter = anyio.to_thread.current_default_thread_limiter()
        stats = limiter.statistics()
        return {
            "total_tokens": limiter.total_tokens,
            "borrowed_tokens": limiter.borrowed_tokens,
            "tasks_waiting": stats.tasks_waiting,
        }
    except BaseException:
        return {"snapshot_unavailable": True}


async def _sample_process() -> None:
    tick_seconds = 0.1
    previous_cpu = time.process_time()
    previous_sample = time.monotonic()
    expected = previous_sample + tick_seconds
    lag_samples: list[float] = []
    lag_sample_count = 0
    lag_sum_ms = 0.0
    lag_max_ms = 0.0
    while True:
        await asyncio.sleep(tick_seconds)
        now = time.monotonic()
        lag_ms = max(0.0, now - expected) * 1000
        lag_samples.append(lag_ms)
        if len(lag_samples) > 16:
            lag_samples.pop(0)
        lag_sample_count += 1
        lag_sum_ms += lag_ms
        lag_max_ms = max(lag_max_ms, lag_ms)
        expected = now + tick_seconds
        if now - previous_sample < 1.0:
            continue
        cpu = time.process_time()
        try:
            event = _process_snapshot()
            event["sample_interval_ms"] = round((now - previous_sample) * 1000, 3)
            event["process_cpu_percent"] = round((cpu - previous_cpu) / (now - previous_sample) * 100, 3)
            event["loop_lag_ms"] = {
                "max": round(lag_max_ms, 3),
                "mean": round(lag_sum_ms / lag_sample_count, 3) if lag_sample_count else 0.0,
                "samples": lag_sample_count,
                "sample_values_ms": [round(value, 6) for value in lag_samples],
                "sample_values_truncated": lag_sample_count > len(lag_samples),
                "tick_interval_ms": 100,
            }
            event["anyio_limiter"] = _limiter_snapshot()
            _emit(event)
        except BaseException:
            pass
        lag_samples.clear()
        lag_sample_count = 0
        lag_sum_ms = 0.0
        lag_max_ms = 0.0
        previous_cpu = cpu
        previous_sample = now


def _ensure_sampler() -> None:
    try:
        loop = asyncio.get_running_loop()
        with _state.lock:
            task = _state.samplers.get(loop)
            if task is None or task.done():
                # An ASGI server may disable lifespan; first request starts this
                # loop-local task. No startup hook or extra thread is required.
                _state.samplers[loop] = loop.create_task(_sample_process(), name="player-diagnostics-sampler")
                def sampler_done(finished_task: Any) -> None:
                    with _state.lock:
                        if _state.samplers.get(loop) is finished_task:
                            _state.samplers.pop(loop, None)
                _state.samplers[loop].add_done_callback(sampler_done)
    except BaseException:
        pass


def _request_uuid(headers: list[tuple[bytes, bytes]]) -> str:
    values = [value for key, value in headers if key.lower() == b"x-request-id"]
    if len(values) == 1 and len(values[0]) == 36:
        try:
            raw = values[0].decode("ascii")
            parsed = uuid.UUID(raw)
            if str(parsed) == raw.lower():
                return str(parsed)
        except (ValueError, UnicodeError, AttributeError):
            pass
    return str(uuid.uuid4())


class PlayerDiagnosticsMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            not ENABLED
            or scope.get("type") != "http"
            or _METHODS_BY_PATH.get(scope.get("path")) != scope.get("method")
        ):
            await self.app(scope, receive, send)
            return
        accepted_ns = time.monotonic_ns()
        _ensure_sampler()
        request_id = _request_uuid(scope.get("headers", []))
        trace = _Trace(request_id, scope["path"], scope.get("method", "OTHER"), accepted_ns)
        token = _current_trace.set(trace)
        status_code = 500
        response_started = False
        response_completed = False
        failed = False
        with _state.lock:
            _state.inflight += 1
            _state.requests_started += 1
        _checkpoint("MIDDLEWARE_ENTER")

        async def measured_send(message: dict[str, Any]) -> None:
            nonlocal status_code, response_started, response_completed
            if message.get("type") == "http.response.start":
                status_code = int(message["status"])
                response_started = True
                _checkpoint("RESPONSE_HEADERS_READY")
            elif message.get("type") == "http.response.body" and not message.get("more_body", False):
                response_completed = True
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        except BaseException:
            failed = True
            raise
        finally:
            _current_trace.reset(token)
            with _state.lock:
                _state.inflight -= 1
                _state.requests_completed += 1
            try:
                event = trace.finish(status_code, response_started, response_completed, failed)
                event["process_at_completion"] = _counter_snapshot()
                event["process_at_completion"]["anyio_limiter"] = _limiter_snapshot()
                _emit(event)
            except BaseException:
                pass


def _install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import Pool
    import fastapi.routing
    from starlette.responses import JSONResponse

    original_pool_connect = Pool.connect

    @wraps(original_pool_connect)
    def measured_pool_connect(pool: Any, *args: Any, **kwargs: Any) -> Any:
        span = _start("POOL_ACQUIRE")
        tracked = span is not None
        if tracked:
            with _state.lock:
                _state.pool_acquiring += 1
                _state.pool_attempts += 1
        outcome = "ok"
        try:
            return original_pool_connect(pool, *args, **kwargs)
        except BaseException:
            outcome = "error"
            if tracked:
                with _state.lock:
                    _state.pool_errors += 1
            raise
        finally:
            if tracked:
                with _state.lock:
                    _state.pool_acquiring -= 1
            _end(span, outcome)

    Pool.connect = measured_pool_connect

    def before_sql(connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
        try:
            context._player_diagnostic_span = _start("SQL")
        except BaseException:
            pass

    def after_sql(connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
        try:
            span = getattr(context, "_player_diagnostic_span", None)
            context._player_diagnostic_span = None
            _end(span)
        except BaseException:
            pass

    def sql_error(error_context: Any) -> None:
        try:
            context = error_context.execution_context
            if context is not None:
                span = getattr(context, "_player_diagnostic_span", None)
                context._player_diagnostic_span = None
                _end(span, "error")
        except BaseException:
            pass

    transaction_key = "_player_diagnostic_transaction_span"

    def transaction_begin(connection: Any) -> None:
        try:
            setattr(connection, transaction_key, _start("TRANSACTION"))
        except BaseException:
            pass

    def transaction_end(connection: Any, outcome: str) -> None:
        try:
            span = getattr(connection, transaction_key, None)
            if span is not None:
                setattr(connection, transaction_key, None)
                _end(span, outcome)
        except BaseException:
            pass

    def transaction_wrapper(original: Any) -> Any:
        @wraps(original)
        def measured_end(connection: Any, *args: Any, **kwargs: Any) -> Any:
            outcome = "ok"
            try:
                return original(connection, *args, **kwargs)
            except BaseException:
                outcome = "error"
                raise
            finally:
                transaction_end(connection, outcome)
        return measured_end

    event.listen(Engine, "before_cursor_execute", before_sql)
    event.listen(Engine, "after_cursor_execute", after_sql)
    event.listen(Engine, "handle_error", sql_error)
    event.listen(Engine, "begin", transaction_begin)
    # Engine commit/rollback events fire *before* their DBAPI operations. Wrap
    # the implementation boundary so TRANSACTION ends after the actual call,
    # including Connection.close()'s implicit rollback and error paths. No
    # connection.info access is used here: it could reconnect an invalidated
    # connection merely to read diagnostic state.
    Connection._commit_impl = transaction_wrapper(Connection._commit_impl)
    Connection._rollback_impl = transaction_wrapper(Connection._rollback_impl)

    original_commit = Session.commit

    @wraps(original_commit)
    def measured_commit(session: Any, *args: Any, **kwargs: Any) -> Any:
        with diagnostic_span("COMMIT"):
            return original_commit(session, *args, **kwargs)

    Session.commit = measured_commit
    original_serialize_response = fastapi.routing.serialize_response

    @wraps(original_serialize_response)
    async def measured_serialize_response(*args: Any, **kwargs: Any) -> Any:
        with diagnostic_span("SERIALIZATION"):
            return await original_serialize_response(*args, **kwargs)

    fastapi.routing.serialize_response = measured_serialize_response
    original_render = JSONResponse.render

    @wraps(original_render)
    def measured_render(response: Any, content: Any) -> bytes:
        with diagnostic_span("JSON_RENDER"):
            return original_render(response, content)

    JSONResponse.render = measured_render
    _hooks_installed = True


def install(app: Any, engine: Any, *, admission_snapshot: Any = None) -> None:
    """Install once before serving; disabled mode does not import hook targets."""
    if not ENABLED:
        return
    with _install_lock:
        if getattr(app.state, "player_diagnostics_installed", False):
            return
        _install_hooks()
        _state.primary_engine = engine
        _state.admission_snapshot = admission_snapshot
        app.add_middleware(PlayerDiagnosticsMiddleware)
        app.state.player_diagnostics_installed = True
