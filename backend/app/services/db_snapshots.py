"""Read snapshot ownership without two simultaneous pool checkouts per request."""
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session, SessionTransactionOrigin

from app.services.tenant_relations import conflict


@contextmanager
def readonly_snapshot_session(source: Session):
    """Own fresh sessions/connections; never end an externally owned transaction.

    Callers capture required ORM identities before entering: ending a clean
    request read transaction expires its ORM objects and lazy access would take
    the just-released checkout again. PostgreSQL's assigned-XID check also
    protects already-flushed/Core writes that Session.dirty cannot detect.
    """
    if source.new or source.dirty or source.deleted:
        raise conflict()
    bind = source.get_bind()
    if bind.dialect.name != "postgresql":
        with Session(bind=bind, autoflush=False) as snapshot:
            yield snapshot
        return
    if isinstance(bind, Connection):
        # A caller's connection is borrowed, never closed/rolled back here.
        if (bind.closed or not bind.in_transaction()
                or bind.get_isolation_level() != "REPEATABLE READ"
                or bind.exec_driver_sql("SHOW transaction_read_only").scalar_one() != "on"):
            raise conflict()
        with Session(bind=bind, autoflush=False, join_transaction_mode="rollback_only") as snapshot:
            yield snapshot
        return
    if not isinstance(bind, Engine):
        raise RuntimeError("Unsupported database snapshot bind")
    transaction = source.get_transaction()
    if source.in_nested_transaction() or (transaction is not None
                                          and transaction.origin is not SessionTransactionOrigin.AUTOBEGIN):
        raise conflict()
    if transaction is not None:
        if not source.connection().exec_driver_sql("SELECT pg_current_xact_id_if_assigned() IS NULL").scalar_one():
            raise conflict()
        # Release the authentication/read checkout before obtaining the MVCC
        # snapshot. Holding it while waiting for another checkout can deadlock
        # a full QueuePool even though both sessions eventually have close().
        source.rollback()
    with bind.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=connection, autoflush=False) as snapshot:
            yield snapshot
