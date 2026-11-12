"""Transient read results owned by one runtime build and its database session."""
from contextlib import contextmanager
from dataclasses import dataclass, field


_KEY = "runtime_read_scope"
# Each pair expands into two SQL parameters. Leave space for the rest of the
# eligibility predicate, including on SQLite builds with a 999-variable limit.
_CONTENT_BATCH_SIZE = 400


@dataclass
class RuntimeReadScope:
    owners: dict = field(default_factory=dict)
    content_media: dict = field(default_factory=dict)
    file_ownership: dict = field(default_factory=dict)
    contents: dict = field(default_factory=dict)
    sequences: dict = field(default_factory=dict)
    predicates: dict = field(default_factory=dict)


def runtime_reads(db) -> RuntimeReadScope | None:
    return getattr(db, "info", {}).get(_KEY)


def content_batches(keys):
    if len(keys) <= _CONTENT_BATCH_SIZE:
        if keys:
            yield keys
        return
    ordered = sorted(keys)
    for start in range(0, len(ordered), _CONTENT_BATCH_SIZE):
        yield set(ordered[start:start + _CONTENT_BATCH_SIZE])


@contextmanager
def runtime_read_scope(db):
    previous = db.info.get(_KEY)
    db.info[_KEY] = RuntimeReadScope()
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_KEY, None)
        else:
            db.info[_KEY] = previous
