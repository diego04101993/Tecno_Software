"""Always-on, response-neutral reasons for playback HTTP 503 only.

Only fixed enums, a validated request UUID and response metadata are logged.
The durable ingress, its retries and HTTP response remain owned by their
existing code. A failure in this observer must never affect them.
"""
import asyncio
from datetime import UTC, datetime
import json
import logging

from sqlalchemy.exc import SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError

from app.core.player_diagnostics import _current_trace, _request_uuid
from app.core.request_pg_diagnostics import capture_failure, failure_metadata, request_uuid as correlated_request_uuid


REASONS = frozenset({
    "PLAYBACK_INBOX_FULL", "PLAYBACK_DB_LOCK_UNAVAILABLE", "PLAYBACK_DB_ERROR",
    "PLAYBACK_ADMISSION_REJECT", "PLAYBACK_TIMEOUT_OR_CANCEL",
    "PLAYBACK_DEPENDENCY_UNAVAILABLE", "PLAYBACK_OTHER",
})
_ADMISSION_CAUSES = frozenset({
    "ADMISSION_DEADLINE_AT_ENTRY", "ADMISSION_QUEUE_FULL", "ADMISSION_WAIT_DEADLINE",
    "ADMISSION_EXPIRED_HEAD", "ADMISSION_EXPIRED_AT_GRANT", "ADMISSION_EXPIRED_AFTER_GRANT",
})
_INTERNAL_CAUSES = _ADMISSION_CAUSES | frozenset({
    "COUNT_OR_BYTE_CAPACITY", "PARENT_SCOPE_LOCK_RETRY_EXHAUSTED",
    "SQLSTATE_55P03", "SQLALCHEMY_ERROR", "SQLALCHEMY_POOL_TIMEOUT",
    "TIMEOUT", "CANCELLED", "DEPENDENCY_HTTP_503", "HANDLER_HTTP_503",
    "UNCLASSIFIED_HTTP_503", "RESPONSE_HTTP_503",
})
_SCOPE_REASON = "_player_playback_503_metadata"
_SCOPE_HANDLER = "_player_playback_handler_entered"
_SCOPE_DATABASE = "_player_playback_503_database"


def tagged_503(error, reason, internal_cause, *, db=None, source_error=None):
    """Return the SAME exception, with no changes to status/body/headers."""
    try:
        if reason in REASONS and internal_cause in _INTERNAL_CAUSES:
            error._playback_503_reason = reason
            error._playback_503_internal_cause = internal_cause
            capture_failure(error, db=db, source_error=source_error)
    except Exception:
        pass
    return error


def storage_503(error, storage_error, *, db=None):
    """Classify an existing storage rejection without exposing exception text."""
    try:
        capture_failure(error, db=db, source_error=storage_error)
        if getattr(getattr(storage_error, "orig", None), "sqlstate", None) == "55P03":
            return tagged_503(error, "PLAYBACK_DB_LOCK_UNAVAILABLE", "SQLSTATE_55P03")
        cause = "SQLALCHEMY_POOL_TIMEOUT" if isinstance(storage_error, SQLAlchemyTimeoutError) else "SQLALCHEMY_ERROR"
        return tagged_503(error, "PLAYBACK_DB_ERROR", cause)
    except Exception:
        return error


def handler_entered(request):
    try:
        request.scope[_SCOPE_HANDLER] = True
    except Exception:
        pass


def _classify(error, handler_started):
    reason = getattr(error, "_playback_503_reason", None)
    cause = getattr(error, "_playback_503_internal_cause", None)
    if type(reason) is str and reason in REASONS and type(cause) is str and cause in _INTERNAL_CAUSES:
        return reason, cause
    admission = getattr(error, "_player_503_reason", None)
    if type(admission) is str and admission in _ADMISSION_CAUSES:
        return "PLAYBACK_ADMISSION_REJECT", admission
    # A handled HTTPException may retain the internal failure as its cause.
    # Inspect types only. Never stringify errors, parameters or their context.
    seen = set()
    current = error
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, SQLAlchemyError):
            if getattr(getattr(current, "orig", None), "sqlstate", None) == "55P03":
                return "PLAYBACK_DB_LOCK_UNAVAILABLE", "SQLSTATE_55P03"
            return "PLAYBACK_DB_ERROR", ("SQLALCHEMY_POOL_TIMEOUT" if isinstance(current, SQLAlchemyTimeoutError)
                                         else "SQLALCHEMY_ERROR")
        if isinstance(current, asyncio.CancelledError):
            return "PLAYBACK_TIMEOUT_OR_CANCEL", "CANCELLED"
        if isinstance(current, TimeoutError):
            return "PLAYBACK_TIMEOUT_OR_CANCEL", "TIMEOUT"
        current = current.__cause__ or current.__context__
    if not handler_started:
        return "PLAYBACK_DEPENDENCY_UNAVAILABLE", "DEPENDENCY_HTTP_503"
    return "PLAYBACK_OTHER", "HANDLER_HTTP_503"


def exception_observed(request, error):
    """Observe only a 503 that existing code already raised; do not convert it."""
    try:
        if error.status_code == 503:
            request.scope[_SCOPE_REASON] = _classify(error, request.scope.get(_SCOPE_HANDLER, False))
            request.scope[_SCOPE_DATABASE] = failure_metadata(error)
    except Exception:
        pass


class Playback503DiagnosticsMiddleware:
    """Capture every actual playback 503 response, including unknown producers."""
    def __init__(self, app, *, api_prefix="/api"):
        self.app = app
        self.path = api_prefix.rstrip("/") + "/player/playback-events"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        logged = False

        async def observed_send(message):
            nonlocal logged
            if message.get("type") == "http.response.start" and message.get("status") == 503 and not logged:
                logged = True
                try:
                    request_id = correlated_request_uuid(scope)
                    reason, cause = scope.get(_SCOPE_REASON, ("PLAYBACK_OTHER", "RESPONSE_HTTP_503"))
                    if (type(reason) is not str or reason not in REASONS
                            or type(cause) is not str or cause not in _INTERNAL_CAUSES):
                        reason, cause = "PLAYBACK_OTHER", "UNCLASSIFIED_HTTP_503"
                    logging.getLogger(__name__).warning(json.dumps({
                        "event": "playback_http_503", "request_id": request_id,
                        "playback_503_reason": reason, "status_code": 503,
                        "retry_after_present": any(key.lower() == b"retry-after" for key, _ in message.get("headers", [])),
                        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "internal_cause": cause,
                        **scope.get(_SCOPE_DATABASE, failure_metadata()),
                    }, separators=(",", ":"), allow_nan=False))
                except Exception:
                    pass
            await send(message)

        await self.app(scope, receive, observed_send)
