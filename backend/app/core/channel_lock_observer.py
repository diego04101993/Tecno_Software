"""Read-only PostgreSQL wait edges for an external, explicitly started observer."""
from sqlalchemy import text


_MAX_WAITERS = 16
_WAIT_EDGES = text("""
WITH waiting AS MATERIALIZED (
    SELECT pid, backend_start, xact_start, backend_xid, wait_event
    FROM pg_stat_activity
    WHERE pid = ANY(CAST(:pids AS integer[]))
      AND pid <> pg_backend_pid()
      AND datname = current_database()
      AND wait_event_type = 'Lock'
)
SELECT clock_timestamp() AS observed_at,
       w.pid AS waiter_pid, w.backend_start AS waiter_backend_start,
       w.xact_start AS waiter_xact_start, CAST(w.backend_xid AS text) AS waiter_xid,
       w.wait_event, l.wait_started_at,
       b.pid AS blocker_pid, h.backend_start AS blocker_backend_start,
       h.xact_start AS blocker_xact_start, CAST(h.backend_xid AS text) AS blocker_xid
FROM waiting AS w
CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS b(pid)
LEFT JOIN pg_stat_activity AS h ON h.pid = b.pid
LEFT JOIN LATERAL (
    SELECT min(waitstart) AS wait_started_at
    FROM pg_locks WHERE pid = w.pid AND NOT granted
) AS l ON true
""")


def sample_channel_waits(connection, waiter_pids):
    """One SELECT on a dedicated observer connection, never an application Session.

    Use only PIDs from open channel_lock_attempt events. Poll in short read-only
    snapshots so pg_stat_activity is refreshed. An empty result is no witness,
    not evidence that a lock was never held. A blocker with no app trace stays
    unattributed. No raw SQL, user, address, or business data is selected.
    """
    pids = tuple(waiter_pids)
    if len(pids) > _MAX_WAITERS or any(type(pid) is not int or not 0 < pid <= 2**31 - 1 for pid in pids):
        raise ValueError("Expected at most 16 PostgreSQL waiter PIDs")
    if not pids:
        return []
    if connection.dialect.name != "postgresql":
        raise ValueError("The observer requires PostgreSQL")
    return [dict(row) for row in connection.execute(_WAIT_EDGES, {"pids": list(dict.fromkeys(pids))}).mappings()]
