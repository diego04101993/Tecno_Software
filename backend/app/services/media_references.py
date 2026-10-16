"""Cooperative media reference protocol shared by all application writers.

PostgreSQL transaction advisory locks live on a dedicated, unpooled connection,
so committing the application's Session does not release them. Its transaction
is always rolled back and connection closed in finally; no session locks leak.
SQLite coordination is process-local and is only a unit-test substitute.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
from threading import RLock

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.entities import ContentItem
from app.services.media_storage import MediaPathError, resolve_stored_media_path, validate_media_file


_STATE = "media_reference_operation"
_SQLITE_LOCK = RLock()


def media_lock_key(path: Path) -> int:
    canonical = os.path.normcase(str(path.resolve()))
    return int.from_bytes(sha256(("tecno-media-v1\0" + canonical).encode("utf-8")).digest()[:8],
                          "big", signed=True)


@contextmanager
def media_reference_operation(db: Session):
    """Own the DB transaction through commit AND physical cleanup.

    Call lock_media_paths with the complete batch before any mutation. Nested
    helpers can reuse that batch but cannot expand it (avoids lock-order inversion).
    Errors roll back DB changes before releasing locks. No unlink before commit.
    """
    if _STATE in db.info:
        yield
        return
    state = {"paths": None, "connection": None, "engine": None, "sqlite": False, "committed": False}
    db.info[_STATE] = state

    def after_commit(session):
        if not session.in_nested_transaction():
            state["committed"] = True

    def before_flush(*_):
        state["committed"] = False

    def before_statement(execution):
        if execution.is_insert or execution.is_update or execution.is_delete:
            state["committed"] = False

    listeners = (("after_commit", after_commit), ("before_flush", before_flush),
                 ("do_orm_execute", before_statement))
    for name, listener in listeners:
        event.listen(db, name, listener)
    try:
        yield
    finally:
        try:
            # Also ends the post-commit reference-check read transaction.
            db.rollback()
        finally:
            try:
                if state["connection"] is not None:
                    # Transaction-scoped locks, not pg_advisory_lock session locks.
                    state["connection"].close()  # rollback, release, physically close (NullPool)
            finally:
                if state["engine"] is not None:
                    state["engine"].dispose()
                if state["sqlite"]:
                    _SQLITE_LOCK.release()
                db.info.pop(_STATE, None)
                for name, listener in listeners:
                    event.remove(db, name, listener)


def require_media_commit(db: Session) -> None:
    state = db.info.get(_STATE)
    if not state or not state["committed"] or db.new or db.dirty or db.deleted:
        raise MediaPathError("MEDIA_CLEANUP_REQUIRES_COMMIT")


def lock_media_paths(db: Session, paths: list[Path]) -> None:
    state = db.info.get(_STATE)
    if state is None:
        raise MediaPathError("MEDIA_LOCK_REQUIRED")
    root = get_settings().MEDIA_ROOT
    canonical = {validate_media_file(root, path, must_exist=False) for path in paths}
    if state["paths"] is not None:
        if not canonical.issubset(state["paths"]):
            raise MediaPathError("MEDIA_LOCK_SCOPE_CHANGED")
        if state["connection"] is not None:
            # Fail closed if coordination was lost while the data Session was
            # committing/checking references. Never silently reconnect/retry.
            state["connection"].execute(text("SELECT 1"))
        return
    dialect = db.get_bind().dialect.name
    if canonical and dialect == "postgresql":
        if db.connection().get_isolation_level() != "READ COMMITTED":
            raise MediaPathError("MEDIA_LOCK_REQUIRES_READ_COMMITTED")
        # NullPool avoids starving the application's pool while requests hold a
        # data connection and wait for their separate coordination connection.
        engine = create_engine(db.get_bind().engine.url, poolclass=NullPool,
                               connect_args={"connect_timeout": 10})
        state["engine"] = engine
        connection = engine.connect()
        state["connection"] = connection
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        for key in sorted({media_lock_key(path) for path in canonical}):
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    elif canonical and dialect == "sqlite":
        _SQLITE_LOCK.acquire()
        state["sqlite"] = True
    elif canonical:
        raise MediaPathError("MEDIA_LOCK_DATABASE_UNSUPPORTED")
    state["paths"] = canonical


def save_content_reference(content: ContentItem, db: Session) -> None:
    """Commit a create/file_path update under the same lock used by deletion.

    Current API writers only create; this also covers updates by locking both the
    old and new paths and rejecting a stale update. No implicit filesystem delete.
    """
    root = get_settings().MEDIA_ROOT
    with db.no_autoflush:
        old = db.scalar(select(ContentItem.file_path).where(ContentItem.id == content.id)) if content.id else None
        new = content.file_path
        paths = [path for value in (old, new)
                 if (path := resolve_stored_media_path(root, value, must_exist=False)) is not None]
        with media_reference_operation(db):
            lock_media_paths(db, paths)
            if content.id:
                current = db.scalar(select(ContentItem.file_path).where(ContentItem.id == content.id))
                if current != old:
                    raise MediaPathError("MEDIA_REFERENCE_CHANGED")
            # A deleter may have finished while this writer waited for its lock.
            # Reject missing targets before inserting/updating any reference.
            resolve_stored_media_path(root, new)
            db.add(content)
            db.commit()
