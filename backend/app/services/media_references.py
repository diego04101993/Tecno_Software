"""Cooperative media reference protocol shared by all application writers.

PostgreSQL transaction advisory locks live on a dedicated, unpooled connection,
so committing the application's Session does not release them. Its transaction
is always rolled back and connection closed in finally; no session locks leak.
SQLite coordination is process-local and is only a unit-test substitute.
"""
import logging
from collections import defaultdict
from enum import Enum
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit, urljoin

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
from threading import RLock

from sqlalchemy import JSON, create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.base import Base
from app.models.entities import Client, ContentItem, Dataset, DatasetImport
from app.services.media_storage import MediaPathError, StoredUpload, resolve_stored_media_path, validate_media_file


logger = logging.getLogger(__name__)
_STATE = "media_reference_operation"
_SQLITE_LOCK = RLock()
_GENERATED_REFS = "vw1_generated_reference_batch"
_OWNERSHIP_VALIDATION_PHASE = "media_ownership_validation_phase"


class _OwnershipValidationPhase:
    """Read-only proof lifetime, never a Session/request/transaction-wide cache.

    READ COMMITTED alone cannot justify reuse. Ownership indexes may be reused
    only while the exact complete canonical path batch remains coordinated.
    Public index builders and lookups outside that batch remain fresh.
    """
    def __init__(self, db):
        self.db = db
        self.connection = db.connection()
        self.transaction = db.get_transaction()
        self.connection_transaction = self.connection.get_transaction()
        self.media_state = db.info.get(_STATE)
        self.paths = frozenset((self.media_state or {}).get("paths") or ())
        self.coordinator = (self.media_state or {}).get("connection")
        self.media_engine = (self.media_state or {}).get("engine")
        self.sqlite_coordinator = (self.media_state or {}).get("sqlite")
        self.coordinator_transaction = self.coordinator.get_transaction() if self.coordinator is not None else None
        self.indexes = {}
        self.invalidate_callbacks = []
        self.tainted = False
        self.active = True
        self.reusable = True
        self.listeners = []

    def invalidate(self):
        self.tainted = True
        self.indexes.clear()
        for callback in self.invalidate_callbacks:
            callback()

    def decline(self):
        self.reusable = False
        self.indexes.clear()
        for callback in self.invalidate_callbacks:
            callback()

    def deny(self, *_args):
        from app.services.tenant_relations import conflict
        self.invalidate()
        raise conflict()

    def check(self):
        from app.services.tenant_relations import conflict
        if (not self.active or self.tainted or self.db.info.get(_OWNERSHIP_VALIDATION_PHASE) is not self
                or self.db.get_transaction() is not self.transaction
                or self.connection.closed or self.connection.get_transaction() is not self.connection_transaction
                or self.db.new or self.db.dirty or self.db.deleted):
            self.invalidate()
            raise conflict()
        if self.db.info.get(_STATE) is not self.media_state:
            self.invalidate()
            raise conflict()
        if self.media_state is not None and (
                self.media_state.get("connection") is not self.coordinator
                or self.media_state.get("engine") is not self.media_engine
                or self.media_state.get("sqlite") != self.sqlite_coordinator
                or frozenset(self.media_state.get("paths") or ()) != self.paths
                or self.media_state.get("committed")):
            self.invalidate()
            raise conflict()
        if self.coordinator is not None and (self.coordinator.closed
                or self.coordinator.get_transaction() is not self.coordinator_transaction):
            self.invalidate()
            raise conflict()

    def before_cursor(self, _conn, _cursor, _statement, _parameters, context, _many):
        """Only structured read statements are valid during a proof phase.

        Inspect the entire SQL expression, including writable CTEs. Unknown
        driver/text SQL cannot silently mutate authority behind a cached proof.
        The normal writer's DML runs after this scope has removed its listeners.
        SQL function names remain reviewed backend code; this is not a general
        SQL sandbox for user-supplied statements or arbitrary stored functions.
        """
        from sqlalchemy.sql.ddl import DDLElement
        from sqlalchemy.sql.elements import TextClause
        self.check()
        statement = context.compiled.statement if context.compiled is not None else None
        if statement is None or isinstance(statement, TextClause):
            # Preserve these exact, known read operations, without assuming a
            # general SQL parser or retaining any proof across unknown code.
            import re
            if re.fullmatch(r"\s*SELECT\s+(?:1|current_database\s*\(\s*\)|current_schema\s*\(\s*\))\s*;?\s*",
                            _statement, flags=re.IGNORECASE):
                self.decline()
                return
            self.deny()
        if statement is None or not getattr(statement, "is_select", False):
            self.deny()
        pending, seen = [statement], set()
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if getattr(node, "is_dml", False) or isinstance(node, (DDLElement, TextClause)):
                self.deny()
            pending.extend(node.get_children())

    def transaction_ended(self, *_args):
        self.invalidate()

    def install(self):
        for target, name, callback in (
            (self.db, "before_flush", self.deny),
            (self.db, "before_commit", self.deny),
            (self.db, "after_transaction_end", self.transaction_ended),
            (self.connection, "before_cursor_execute", self.before_cursor),
            (self.connection, "commit", self.deny),
            (self.connection, "rollback", self.transaction_ended),
        ):
            event.listen(target, name, callback)
            self.listeners.append((target, name, callback))

    def close(self):
        self.active = False
        self.indexes.clear()
        for target, name, callback in reversed(self.listeners):
            event.remove(target, name, callback)
        self.listeners.clear()
        self.invalidate_callbacks.clear()

    def owners(self, root, path):
        self.check()
        if (not self.reusable or not self.paths or path not in self.paths or self.media_state is None
                or (self.db.get_bind().dialect.name == "postgresql" and self.coordinator is None)
                or (self.db.get_bind().dialect.name == "sqlite" and not self.media_state.get("sqlite"))):
            return media_reference_owners(self.db, root)
        key = root.resolve()
        if key not in self.indexes:
            self.indexes[key] = MappingProxyType({p: frozenset(owners)
                for p, owners in media_reference_owners(self.db, root).items()})
        self.check()
        return self.indexes[key]


@contextmanager
def media_ownership_validation_phase(db):
    """One pure preflight only; exit/rollback/exception cannot retain authority."""
    from app.services.tenant_relations import conflict
    existing = db.info.get(_OWNERSHIP_VALIDATION_PHASE)
    if existing is not None:
        if type(existing) is _OwnershipValidationPhase:
            existing.invalidate()
        raise conflict()
    if db.new or db.dirty or db.deleted:
        raise conflict()
    phase = _OwnershipValidationPhase(db)
    db.info[_OWNERSHIP_VALIDATION_PHASE] = phase
    failed = False
    try:
        phase.install()
        try:
            yield phase
        except BaseException:
            failed = True
            raise
        finally:
            # An unsafe mutation followed by no further cache lookup must also
            # fail closed. Do not mask an exception already rejecting the proof.
            if not failed:
                phase.check()
    finally:
        phase.close()
        db.info.pop(_OWNERSHIP_VALIDATION_PHASE, None)


def _private_identity(path: Path):
    import stat
    if path.is_symlink():
        raise MediaPathError("GENERATED_MEDIA_SOURCE_INVALID")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise MediaPathError("GENERATED_MEDIA_SOURCE_INVALID")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _GeneratedReferenceBatch:
    """Backend-only capability for a complete, guarded publication batch.

    No schema accepts this object. Reservations do not write public files or
    establish ownership; the fresh tenant guard must authorize every CREATE.
    """
    def __init__(self, db, pairs):
        from sqlalchemy import inspect
        from uuid import uuid4
        self.db, self.plan = db, None
        self.entries = []
        self.contents = tuple(content for content, _ in pairs)
        self.transaction = None
        root = get_settings().MEDIA_ROOT.resolve()
        seen = set()
        for content, private in pairs:
            if (type(content) is not ContentItem or not inspect(content).transient
                    or not content.id or content.id in seen or content.file_path is not None
                    or db.get(Client, content.client_id) is None):
                raise MediaPathError("GENERATED_REFERENCE_INVALID")
            seen.add(content.id)
            private = Path(private)
            info = _private_identity(private)
            private = private.resolve()
            if private.is_relative_to(root) or private.suffix.lower() not in {".png", ".mp4"}:
                raise MediaPathError("GENERATED_MEDIA_SOURCE_INVALID")
            digest = sha256()
            with private.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if _private_identity(private) != info:
                raise MediaPathError("GENERATED_MEDIA_CHANGED")
            from app.services.media_storage import contained_media_path, normalize_upload_filename
            if normalize_upload_filename(content.client_id) != content.client_id:
                raise MediaPathError("GENERATED_REFERENCE_INVALID")
            destination = contained_media_path(root, root / content.client_id / (uuid4().hex + private.suffix.lower()))
            if destination.exists() or destination.is_symlink():
                raise MediaPathError("GENERATED_MEDIA_TARGET_EXISTS")
            content.file_path = "/media/" + destination.relative_to(root).as_posix()
            self.entries.append(dict(content=content, private=private, identity=info,
                                     destination=destination, checksum=digest.hexdigest(), published=None))

    def authorizes(self, identity, client_id, path):
        for entry in self.entries:
            content = entry["content"]
            if (content.id, content.client_id, entry["destination"]) != (identity, client_id, path):
                continue
            try:
                if _private_identity(entry["private"]) != entry["identity"]:
                    return False
                if entry["published"] is None:
                    return not path.exists() and not path.is_symlink()
                return _private_identity(path) == entry["published"]
            except (OSError, MediaPathError):
                return False
        return False

    def publish(self):
        from app.services.media_storage import promote_generated_media
        from app.services.tenant_relations import conflict
        if (self.plan is None or not self.plan._gate.active
                or self.db.get_transaction() is not self.plan._transaction
                or self.transaction is not self.db.get_transaction()):
            raise conflict()
        state = self.db.info.get(_STATE)
        if state is None or any(e["destination"] not in (state["paths"] or set()) for e in self.entries):
            raise MediaPathError("MEDIA_LOCK_REQUIRED")
        root = get_settings().MEDIA_ROOT
        for entry in self.entries:
            content = entry["content"]
            if not self.authorizes(content.id, content.client_id, entry["destination"]):
                raise MediaPathError("GENERATED_MEDIA_CHANGED")
            if entry["published"] is not None:
                raise MediaPathError("GENERATED_REFERENCE_ALREADY_PUBLISHED")
            uploaded = promote_generated_media(root, content.client_id, entry["private"],
                                               entry["destination"], entry["checksum"])
            entry["published"] = _private_identity(uploaded.physical_path)
            # All mutations remain in the caller's one transaction. Complete
            # orphans after a failed commit follow the existing upload policy.
            self.db.add(content)


@contextmanager
def generated_content_references(db, pairs):
    """Reserve private worker results; caller builds intents, guards, then publish()."""
    if _GENERATED_REFS in db.info or db.new or db.dirty or db.deleted:
        raise MediaPathError("GENERATED_REFERENCE_SCOPE_INVALID")
    pairs = tuple(pairs)
    if not pairs:
        raise MediaPathError("GENERATED_REFERENCE_EMPTY")
    batch = _GeneratedReferenceBatch(db, pairs)
    db.info[_GENERATED_REFS] = batch
    try:
        yield batch
    finally:
        batch.plan = None
        db.info.pop(_GENERATED_REFS, None)


def generated_media_reference_authorized(db, identity, client_id, path):
    batch = db.info.get(_GENERATED_REFS)
    return (type(batch) is _GeneratedReferenceBatch
            and batch.authorizes(identity, client_id, path))


@contextmanager
def generated_reference_guard(db, plan):
    """Bind the capability to the actual, already checked A6/A7 write plan."""
    from app.services.tenant_relations import conflict
    batch = db.info.get(_GENERATED_REFS)
    if batch is None:
        yield
        return
    if type(batch) is not _GeneratedReferenceBatch or batch.plan is not None:
        raise conflict()
    created = {intent.reference.identity for intent in plan.intents
               if intent.action == "CREATE" and intent.reference.model == "ContentItem"}
    proposals = {p.reference.identity: p.values for p in plan.proposed
                 if p.reference.model == "ContentItem"}
    if created != {c.id for c in batch.contents}:
        raise conflict()
    for content in batch.contents:
        values = proposals.get(content.id)
        if (values is None or values.get("file_path") != content.file_path
                or values.get("client_id") != content.client_id):
            raise conflict()
    batch.plan, batch.transaction = plan, db.get_transaction()
    try:
        yield
    finally:
        batch.plan = None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def _local_url(value: str) -> str | None:
    if value.startswith(("/media/", "media/")):
        return value
    # Only our configured public host is a local media URL; no HTTP request.
    try:
        parsed = urlsplit(value)
        public = urlsplit(get_settings().PUBLIC_BASE_URL or "")
    except ValueError as exc:
        raise MediaPathError("UNRESOLVED_SURVIVING_REFERENCE") from exc
    if parsed.scheme in {"http", "https"} and parsed.netloc == public.netloc and parsed.path.startswith("/media/"):
        return parsed.path
    return None


def media_reference_owners(db: Session, root: Path, *, strict: bool = False) -> dict[Path, set[str | None]]:
    """Only persisted typed references establish ownership; free JSON never does."""
    references = defaultdict(set)
    clients = set(db.scalars(select(Client.id)))
    rows = list(db.execute(select(ContentItem.client_id, ContentItem.file_path)))
    for stored, client_id in db.execute(select(DatasetImport.storage_path, Dataset.client_id)
                                       .outerjoin(Dataset, Dataset.id == DatasetImport.dataset_id)):
        if stored:
            rows.append((client_id, stored if stored.startswith(("/media/", "media/")) else "/media/" + stored))
    for client_id, stored in rows:
        if stored is None:
            continue
        try:
            path = resolve_stored_media_path(root, stored, must_exist=False)
        except MediaPathError:
            if strict:
                raise
            logger.warning("INVALID_AUTHORITATIVE_MEDIA_REFERENCE")
            continue
        references[path].add(client_id if client_id in clients else None)
    return dict(references)


def media_protected_paths(db: Session, root: Path) -> set[Path]:
    """Valid non-authoritative references prevent deletion, never grant ownership."""
    paths = set(media_reference_owners(db, root, strict=True))
    for mapper in Base.registry.mappers:
        columns = [column for column in mapper.columns
                   if column.name.endswith("_url") or isinstance(column.type, JSON)]
        if not columns:
            continue
        for row in db.execute(select(*columns)):
            for value in row:
                for string in _strings(value):
                    try:
                        local = _local_url(string)
                        if local:
                            paths.add(resolve_stored_media_path(root, local, must_exist=False))
                    except MediaPathError:
                        logger.warning("INVALID_NONAUTHORITATIVE_MEDIA_REFERENCE")
    return paths


def media_owned_by(db: Session, root: Path, path: Path, client_id: str,
                   references: dict[Path, set[str | None]] | None = None) -> bool:
    phase = db.info.get(_OWNERSHIP_VALIDATION_PHASE)
    if phase is not None:
        phase.check()
    refs = references if references is not None else (
        phase.owners(root, path) if phase is not None else media_reference_owners(db, root))
    owners = set(refs.get(path, set()))
    # A directory can veto contradictory historical records, never establish ownership.
    parts = path.relative_to(root.resolve()).parts
    namespace = parts[1] if len(parts) > 2 and parts[0] == "datasets" else parts[0]
    if len(parts) > 1 and namespace != client_id and db.get(Client, namespace) is not None:
        return False
    return owners == {client_id}


def content_file_owned(db: Session, content: ContentItem) -> bool:
    if content.file_path is None:
        return True
    root = get_settings().MEDIA_ROOT
    try:
        path = resolve_stored_media_path(root, content.file_path, must_exist=False)
        return path is not None and media_owned_by(db, root, path, content.client_id)
    except MediaPathError:
        logger.warning("INVALID_RUNTIME_MEDIA_REFERENCE")
        return False


# Security keys belong to URL READ projections only. Persisted file_path values
# and the existing write/cleanup protocol deliberately retain literal semantics.
_MEDIA_URL_LIMIT = 16_384
_MEDIA_TEXT_LIMIT = 1_048_576
_MEDIA_REFERENCE_LIMIT = 2048
_HTML_SPACE = "\t\n\f\r "
_CSS_SPACE = "\t\n\f\r "
_URL_BOUNDARY_SPACE = "\t\n\f\r "


def _effective_url(value: str, *, base: str | None = None) -> str:
    """Normalize only URL boundary ASCII whitespace, never Unicode/path space.

    Embedded controls are rejected instead of approximating URL error recovery.
    Percent-encoded spaces and NBSP remain part of the path. HTML entities and
    CSS escapes have already been handled by the explicitly selected context.
    """
    if not isinstance(value, str) or len(value) > _MEDIA_URL_LIMIT:
        raise MediaPathError("INVALID_MEDIA_URL")
    value = value.strip(_URL_BOUNDARY_SPACE)
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
           for c in value) or "\\" in value:
        raise MediaPathError("AMBIGUOUS_MEDIA_URL")
    if not value:
        raise MediaPathError("UNKNOWN_MEDIA_URL")
    if base is not None:
        # The caller has already selected the document's single effective base.
        value = urljoin(base, value)
    return value


def _media_origin(parsed):
    """Syntactic origin; no DNS, HTTP, or filesystem lookup."""
    import ipaddress
    host = parsed.hostname
    if not host or "%" in host or "\\" in host:
        raise ValueError("Ambiguous media host")
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or any(not part or not all(c.isalnum() or c == "-" for c in part)
                           for part in host.split(".")):
            raise ValueError("Ambiguous media host")
        # Browser-specific numeric IPv4 spellings are not DNS hostnames.
        if all(part.isdigit() or part.lower().startswith("0x") for part in host.split(".")):
            raise ValueError("Ambiguous numeric media host")
    scheme = parsed.scheme.lower()
    port = parsed.port if parsed.port is not None else {"http": 80, "https": 443}.get(scheme)
    return scheme, host, port


def canonical_media_reference(value: str) -> tuple[str, str | None]:
    """LOCAL + canonical /media/key, EXTERNAL, OTHER, or AMBIGUOUS.

    One strict percent decode matches the ASGI URL path boundary. A residual
    encoded octet, invalid encoding, or uncertain delimiter is never external.
    Query/fragment are not part of ownership. Original response URLs are intact.
    """
    import re
    from urllib.parse import unquote
    try:
        value = _effective_url(value)
        if len(value) > _MEDIA_URL_LIMIT or any(ord(c) < 32 or 127 <= ord(c) <= 159
                or 0xD800 <= ord(c) <= 0xDFFF for c in value) or "\\" in value:
            raise ValueError("Ambiguous media URL")
        parsed = urlsplit(value)
        public = urlsplit(get_settings().PUBLIC_BASE_URL or "")
        if parsed.netloc or value.startswith("//"):
            if value.startswith("///"):
                raise ValueError("Ambiguous authority")
            if not parsed.scheme:
                parsed = parsed._replace(scheme=public.scheme)
            if parsed.scheme.lower() not in {"http", "https"}:
                return "AMBIGUOUS", None
            if _media_origin(parsed) != _media_origin(public):
                return "EXTERNAL", None
        elif parsed.scheme:
            # HTTP without an authority has browser-dependent relative semantics.
            if parsed.scheme.lower() in {"http", "https"}:
                raise ValueError("Missing HTTP authority")
            return "OTHER", None
        raw_path = parsed.path
        if re.search(r"%(?![0-9a-fA-F]{2})", raw_path):
            raise ValueError("Malformed percent encoding")
        path = unquote(raw_path, encoding="utf-8", errors="strict")
        if re.search(r"%[0-9a-fA-F]{2}", path):
            raise ValueError("Nested percent encoding")
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
               for c in path) or "\\" in path or "//" in path:
            raise ValueError("Ambiguous decoded path")
        segments = []
        for segment in path.split("/"):
            if segment in {"", "."}:
                continue
            if segment == "..":
                if segments:
                    segments.pop()
            else:
                segments.append(segment)
        key = "/" + "/".join(segments)
        if key == "/media" or key.startswith("/media/"):
            return "LOCAL", key
        return "OTHER", None
    except (ValueError, UnicodeError, MediaPathError):
        return "AMBIGUOUS", None


def _css_source_closed(value: str) -> None:
    """Reject implicit EOF recovery; recognize delimiters without decoding them.

    This is a closure check, not a second CSS parser. tinycss2 alone supplies
    token values. In particular an escaped quote/paren never closes a block.
    """
    stack, quote, i = [], None, 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    while i < len(value):
        char = value[i]
        if char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF:
            raise MediaPathError("INVALID_CSS_CODEPOINT")
        if char == "\\":
            if i + 1 == len(value):
                raise MediaPathError("AMBIGUOUS_CSS_ESCAPE")
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif value.startswith("/*", i):
            end = value.find("*/", i + 2)
            if end < 0:
                raise MediaPathError("AMBIGUOUS_CSS_COMMENT")
            i = end + 2
            continue
        elif char in "\"'":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                raise MediaPathError("AMBIGUOUS_CSS_BLOCK")
        i += 1
    if quote or stack:
        raise MediaPathError("AMBIGUOUS_CSS_EOF")


def _css_media_urls(value: str, *, _with_context=False):
    """CSS source -> tinycss2 tokens -> contextual URL values, never ownership."""
    import tinycss2
    if len(value) > _MEDIA_TEXT_LIMIT:
        raise MediaPathError("MEDIA_TEXT_LIMIT")
    _css_source_closed(value)
    tokens = tinycss2.parse_component_value_list(value, skip_comments=True)
    safe_functions = frozenset((
        "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix",
        "linear-gradient", "radial-gradient", "conic-gradient", "repeating-linear-gradient",
        "repeating-radial-gradient", "repeating-conic-gradient", "calc", "min", "max", "clamp",
        "translate", "translatex", "translatey", "translatez", "translate3d", "scale", "scalex",
        "scaley", "scalez", "scale3d", "rotate", "rotatex", "rotatey", "rotatez", "rotate3d",
        "skew", "skewx", "skewy", "matrix", "matrix3d", "perspective", "cubic-bezier", "steps",
        "blur", "brightness", "contrast", "drop-shadow", "grayscale", "hue-rotate", "invert",
        "opacity", "saturate", "sepia", "not", "is", "where", "nth-child", "nth-of-type", "local", "format"))

    def emit(value, context="CSS_URL"):
        return MediaURLCandidate(value, context, provenance=(context,)) if _with_context else value

    def significant(items):
        return [t for t in items if t.type not in {"whitespace", "comment"}]

    def url_token(token):
        if token.type == "url":
            return token.value
        if token.type == "function" and token.lower_name == "url":
            args = significant(token.arguments)
            if len(args) == 1 and args[0].type == "string":
                return args[0].value
        raise MediaPathError("AMBIGUOUS_CSS_URL")

    def image_set(items):
        # Split token-level separators only: a comma inside a string/url token
        # cannot become a candidate delimiter after escape decoding.
        groups, group = [], []
        for token in significant(items):
            if token.type == "literal" and token.value == ",":
                groups.append(group)
                group = []
            else:
                group.append(token)
        groups.append(group)
        for group in groups:
            if not group:
                raise MediaPathError("AMBIGUOUS_IMAGE_SET")
            first, *descriptors = group
            candidate = first.value if first.type == "string" else url_token(first)
            resolution = media_type = False
            for token in descriptors:
                if (token.type == "dimension" and token.lower_unit in {"x", "dppx", "dpi", "dpcm"}
                        and token.value > 0 and not resolution):
                    resolution = True
                elif token.type == "function" and token.lower_name == "type" and not media_type:
                    args = significant(token.arguments)
                    if len(args) != 1 or args[0].type != "string":
                        raise MediaPathError("AMBIGUOUS_IMAGE_SET_TYPE")
                    media_type = True
                else:
                    raise MediaPathError("AMBIGUOUS_IMAGE_SET_DESCRIPTOR")
            yield emit(candidate, "CSS_IMAGE_SET")

    def walk(items, depth=0):
        if depth > 32:
            raise MediaPathError("CSS_DEPTH_LIMIT")
        for index, token in enumerate(items):
            if token.type == "error":
                raise MediaPathError("INVALID_CSS_TOKEN")
            if token.type == "url":
                yield emit(url_token(token))
            elif token.type == "function":
                if token.lower_name == "url":
                    yield emit(url_token(token))
                elif token.lower_name in {"image-set", "-webkit-image-set"}:
                    yield from image_set(token.arguments)
                elif token.lower_name in safe_functions:
                    yield from walk(token.arguments, depth + 1)
                else:
                    # var()/attr()/image() and unknown future URL-producing
                    # grammars cannot borrow the decision of a partial token.
                    raise MediaPathError("UNSUPPORTED_CSS_FUNCTION")
            elif token.type in {"{} block", "[] block", "() block"}:
                yield from walk(token.content, depth + 1)
            elif token.type == "at-keyword" and token.lower_value == "import":
                rest = significant(items[index + 1:])
                if not rest:
                    raise MediaPathError("AMBIGUOUS_CSS_IMPORT")
                if rest[0].type == "string":
                    yield emit(rest[0].value)
                elif rest[0].type not in {"url", "function"}:
                    raise MediaPathError("AMBIGUOUS_CSS_IMPORT")
    yield from walk(tokens)


def _srcset_media_urls(value: str):
    """Bounded srcset candidate grammar; commas inside URL tokens stay intact.

    HTML ASCII whitespace delimits the URL. A trailing comma terminates a
    descriptorless candidate; descriptor commas separate candidates. Ambiguous
    or unsupported descriptors reject the whole attribute, never a subset.
    """
    import re
    position, candidates, descriptor_kind = 0, [], None
    while position < len(value):
        while position < len(value) and value[position] in _HTML_SPACE:
            position += 1
        if position == len(value):
            break
        if value[position] == ",":
            raise MediaPathError("AMBIGUOUS_SRCSET_CANDIDATE")
        start = position
        while position < len(value) and value[position] not in _HTML_SPACE:
            position += 1
        url = value[start:position]
        if url.endswith(","):
            if url.endswith(",,"):
                raise MediaPathError("AMBIGUOUS_SRCSET_SEPARATOR")
            url, descriptor = url[:-1], ""
        else:
            start = position
            while position < len(value) and value[position] != ",":
                position += 1
            descriptor = value[start:position].strip(_HTML_SPACE)
            if position < len(value):
                position += 1
        if not url:
            raise MediaPathError("INVALID_SRCSET_URL")
        if descriptor:
            if re.fullmatch(r"[1-9][0-9]*w", descriptor):
                kind = "w"
            elif re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)x", descriptor) and float(descriptor[:-1]) > 0:
                kind = "x"
            else:
                raise MediaPathError("AMBIGUOUS_SRCSET_DESCRIPTOR")
        else:
            kind = "x"
        if descriptor_kind is not None and descriptor_kind != kind:
            raise MediaPathError("AMBIGUOUS_SRCSET_MIXED_DESCRIPTORS")
        descriptor_kind = kind
        candidates.append(url)
        if len(candidates) > _MEDIA_REFERENCE_LIMIT:
            raise MediaPathError("MEDIA_REFERENCE_LIMIT")
    if not candidates:
        raise MediaPathError("UNKNOWN_SRCSET")
    yield from candidates


@dataclass(frozen=True)
class MediaURLCandidate:
    """Semantic token with its grammar provenance, before canonicalization."""
    parsed_token: str
    context: str
    parse_mode: str = ""
    base: str | None = None
    effective_url: str | None = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class HTMLMediaView:
    parse_mode: str
    base: str | None
    attributes: tuple
    candidates: tuple[MediaURLCandidate, ...]
    template_seen: bool = False
    ambiguous_character_reference: bool = False
    inert_markup_seen: bool = False


def _validate_html_base_attributes(attrs):
    """An unsupported base attribute rejects the document, never repairs it."""
    import re
    if set(attrs) - {"href", "target"}:
        raise MediaPathError("UNSUPPORTED_HTML_BASE_ATTRIBUTE")
    target = attrs.get("target", "")
    # A browsing-context name is not a resource URL. Support the conventional
    # keywords and plain named contexts; do not return arbitrary opaque text.
    if (target and target.lower() not in {"_blank", "_self", "_parent", "_top"}
            and not re.fullmatch(r"[^\W_][\w.-]*", target)):
        raise MediaPathError("UNSUPPORTED_HTML_BASE_TARGET")


def _html_media_view(string: str, *, mode: str, _depth: int = 0):
    """HTML5 attribute semantics, with scripting enabled in both consumers.

    Runtime HTML has no declared insertion mode. Check both a document and a
    div fragment; the ownership projection must approve every interpretation.
    html5lib 1.1 does not implement the template insertion mode. We never use
    its template descendants as active nodes, and reject templates at the
    projection boundary rather than claim general browser equivalence.
    """
    import html5lib
    if len(string) > _MEDIA_TEXT_LIMIT or _depth > 8:
        raise MediaPathError("MEDIA_HTML_LIMIT")
    parser = html5lib.HTMLParser(tree=html5lib.getTreeBuilder("etree", fullTree=True), namespaceHTMLElements=True)
    if mode == "document":
        root = parser.parse(string, scripting=True)
    elif mode == "fragment":
        root = parser.parseFragment(string, container="div", scripting=True)
    else:
        raise MediaPathError("UNKNOWN_HTML_PARSE_MODE")
    # Recover only tokenization cases whose resulting attributes are defined
    # by HTML5. All other recovery, including unsupported template/table
    # interactions, is uncertain and cannot authorize a reference.
    harmless = {"expected-doctype-but-got-start-tag", "expected-doctype-but-got-chars",
                "expected-doctype-but-got-eof", "named-entity-without-semicolon",
                "unexpected-character-in-unquoted-attribute-value"}
    if any(code not in harmless for _, code, _ in parser.errors):
        raise MediaPathError("AMBIGUOUS_HTML_RECOVERY")
    values, bases, attributes, texts, refresh_values = [], [], [], [], []
    template_seen = inert_markup_seen = False
    html_namespace = "{http://www.w3.org/1999/xhtml}"

    def append(value, context):
        values.append(MediaURLCandidate(value, context, mode, provenance=(context,)))
        if len(values) > _MEDIA_REFERENCE_LIMIT:
            raise MediaPathError("MEDIA_REFERENCE_LIMIT")

    def css(value):
        values.extend(_css_media_urls(value, _with_context=True))

    def walk(node, depth=0):
        nonlocal template_seen, inert_markup_seen
        if depth > 64:
            raise MediaPathError("MEDIA_HTML_DEPTH")
        if not isinstance(node.tag, str):
            if node.text:
                texts.append(node.text)
            return
        if node.tag == "<!DOCTYPE>":
            return
        if node.tag in {"DOCUMENT_ROOT", "DOCUMENT_FRAGMENT", html_namespace + "html"}:
            tag = "html"
        elif node.tag.startswith(html_namespace):
            tag = node.tag[len(html_namespace):]
        else:
            raise MediaPathError("UNSUPPORTED_FOREIGN_MARKUP")
        if tag == "template":
            template_seen = True
            # Template content is a distinct inert document fragment. In
            # particular, no descendant <base> changes this document's base.
            return
        if tag == "script":
            raise MediaPathError("UNSUPPORTED_DYNAMIC_MARKUP")
        attrs = node.attrib
        attributes.extend((tag, name, value) for name, value in attrs.items())
        if tag == "base":
            _validate_html_base_attributes(attrs)
            href = attrs.get("href")
            if not href or bases:
                raise MediaPathError("AMBIGUOUS_HTML_BASE")
            bases.append(href)
        else:
            for name, value in attrs.items():
                if name.startswith("on") or name == "srcdoc":
                    # Event handlers execute code. srcdoc creates a separate
                    # document with inherited base/sandbox state not declared
                    # by this runtime contract. Neither is plain HTML text.
                    raise MediaPathError("UNDECLARED_NESTED_OR_DYNAMIC_CONTEXT")
                if name == "style":
                    css(value)
                elif name == "srcset" or name == "imagesrcset":
                    for url in _srcset_media_urls(value):
                        append(url, "HTML_SRCSET")
                elif name in {"src", "href", "poster", "data", "data-src", "data-href"}:
                    append(value, "HTML_ATTRIBUTE")
                elif tag == "meta" and name == "content" and attrs.get("http-equiv", "").strip(_HTML_SPACE).lower() == "refresh":
                    target = _html_refresh_url(value)
                    if target is not None:
                        refresh_values.append(target)
                        append(target, "HTML_ATTRIBUTE")
                elif value:
                    texts.append(value)
        if tag == "style":
            css(node.text or "")
        elif node.text:
            if tag in {"textarea", "title", "noscript", "iframe", "xmp", "noembed", "noframes"} and ("<" in node.text or ">" in node.text):
                # Inert markup cannot set a base or create active URL tokens.
                # Returning it verbatim could still disclose a foreign path.
                # Omit it as opaque data, without a second HTML parse.
                inert_markup_seen = True
            else:
                texts.append(node.text)
        for child in node:
            walk(child, depth + 1)
            if child.tail:
                texts.append(child.tail)

    walk(root)
    for value in texts:
        for candidate in _html_text_candidates(value):
            values.append(MediaURLCandidate(candidate, "OPAQUE_TEXT", mode, provenance=("OPAQUE_TEXT",)))
    if len(values) > _MEDIA_REFERENCE_LIMIT:
        raise MediaPathError("MEDIA_REFERENCE_LIMIT")
    base = None
    if bases and refresh_values:
        raise MediaPathError("AMBIGUOUS_META_REFRESH_BASE")
    if bases:
        public = get_settings().PUBLIC_BASE_URL
        if not public:
            raise MediaPathError("UNKNOWN_DOCUMENT_ORIGIN")
        base_reference = _effective_url(bases[0])
        if not (base_reference.startswith("/") or urlsplit(base_reference).scheme):
            raise MediaPathError("UNKNOWN_RELATIVE_HTML_BASE")
        base = _effective_url(bases[0], base=public.rstrip("/") + "/")
        if not urlsplit(base).netloc or urlsplit(base).scheme.lower() not in {"http", "https"}:
            raise MediaPathError("AMBIGUOUS_HTML_BASE")
        values.insert(0, MediaURLCandidate(bases[0], "HTML_BASE", mode, None, base, ("HTML_BASE",)))
    resolved = []
    for candidate in values:
        token = candidate.parsed_token
        effective = candidate.effective_url or _effective_url(token, base=base)
        if base is None and not (urlsplit(effective).scheme or effective.startswith(("/", "media/", "./media/"))):
            raise MediaPathError("UNKNOWN_RELATIVE_DOCUMENT_BASE")
        provenance = candidate.provenance
        if base and not urlsplit(token).scheme and candidate.context != "HTML_BASE":
            provenance += ("BASE_RESOLVED_RELATIVE",)
        resolved.append(MediaURLCandidate(token, candidate.context, mode, base, effective, provenance))
    return HTMLMediaView(mode, base, tuple(attributes), tuple(resolved), template_seen,
                         any(code == "named-entity-without-semicolon" for _, code, _ in parser.errors), inert_markup_seen)


def _html_refresh_url(value):
    import re
    space = r"[\t\n\f\r ]*"
    delay = r"[0-9]+(?:\.[0-9]+)?"
    if re.fullmatch(space + delay + space, value):
        return None
    refresh = re.fullmatch(space + delay + space + ";" + space
                           + "url" + space + "=" + space + r"(?P<url>.+)",
                           value, flags=re.IGNORECASE | re.DOTALL)
    if not refresh:
        raise MediaPathError("AMBIGUOUS_META_REFRESH")
    target = refresh.group("url").strip(_HTML_SPACE)
    if target[:1] in {"'", '"'}:
        if len(target) < 2 or target[-1] != target[0] or target[0] in target[1:-1]:
            raise MediaPathError("AMBIGUOUS_META_REFRESH_QUOTE")
        target = target[1:-1]
    if not target or not (target.startswith("/") or urlsplit(target).scheme):
        raise MediaPathError("UNKNOWN_META_REFRESH_BASE")
    return target


def _html_text_candidates(text_value, *, literal_json=False):
    """Bounded text references; JSON keys never acquire HTML/CSS semantics."""
    import re
    if not literal_json:
        if "<" in text_value or ">" in text_value:
            raise MediaPathError("OPAQUE_ENCODED_MARKUP")
        css_urls = list(_css_media_urls(text_value))
        if css_urls:
            yield from css_urls
            return
    stripped = text_value.strip(_HTML_SPACE)
    if (re.match(r"^(?:https?://|//|/|\./|media/|m%[0-9a-fA-F])", stripped, re.IGNORECASE)
            and (not literal_json or not re.search(r'''[<>"']''', stripped))):
        yield stripped
        return
    pattern = re.compile(
        r'''(?P<q>["'])(?P<quoted>(?:https?://|//|/|\./|media/)[^"']*)(?P=q)'''
        r'''|(?P<bare>(?:https?://|//|(?<![\w:/])(?:/|\./|media/))[^\t\n\f\r "'<>]+)''', re.IGNORECASE)
    for match in pattern.finditer(stripped):
        candidate = match.group("quoted") or match.group("bare")
        if match.group("bare"):
            classification, _ = canonical_media_reference(candidate)
            if classification in {"LOCAL", "AMBIGUOUS"}:
                raise MediaPathError("AMBIGUOUS_HTML_TEXT_MEDIA_REFERENCE")
        yield candidate


def _html_candidates(string):
    """No entity predecode, reparsing of attributes, or context selection."""
    views = tuple(_html_media_view(string, mode=mode) for mode in ("document", "fragment"))
    # Chrome fragment fast paths can disagree with the HTML5 attribute rule
    # for missing-semicolon named references. Parser diagnostics identify the
    # ambiguous grammar; do not maintain or decode a separate entity table.
    if any(view.ambiguous_character_reference for view in views):
        raise MediaPathError("AMBIGUOUS_CONSUMER_CHARACTER_REFERENCE")
    if any(view.inert_markup_seen for view in views):
        raise MediaPathError("OPAQUE_INERT_MARKUP")
    if any(view.template_seen for view in views):
        raise MediaPathError("UNSUPPORTED_TEMPLATE_INSERTION_MODE")
    signatures = [{(c.parsed_token, c.context, c.effective_url) for c in v.candidates} for v in views]
    if views[0].base != views[1].base or signatures[0] != signatures[1]:
        raise MediaPathError("AMBIGUOUS_HTML_CONSUMER_CONTEXT")
    yield from views[0].candidates


def _read_url_candidates(string: str, *, _depth: int = 0):
    """Compatibility iterator; provenance remains available until this boundary."""
    for candidate in _html_candidates(string):
        yield candidate.effective_url


class MediaReadContext(Enum):
    TYPED_URL = "typed_url"
    HTML = "html"
    CSS = "css"
    STRUCTURED = "structured"
    WIDGET = "widget"
    JSON_KEY = "json_key"
    OPAQUE = "opaque"
    HTML_ATTRIBUTE = "html_attribute"
    HTML_SRCSET = "html_srcset"
    HTML_BASE = "html_base"
    CSS_URL = "css_url"
    CSS_IMAGE_SET = "css_image_set"
    CSS_STRING_URL = "css_string_url"
    OTHER_STRUCTURED_URL = "other_structured_url"


class MediaReadDecision(Enum):
    SAFE_EXTERNAL = "SAFE_EXTERNAL"
    SAFE_LOCAL_OWNED = "SAFE_LOCAL_OWNED"
    FOREIGN = "FOREIGN"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"


@dataclass(frozen=True)
class EffectiveMediaReference:
    raw_input: str
    context: MediaReadContext
    parsed_token: str
    effective_url: str | None
    canonical_media_key: str | None
    owner: tuple
    decision: MediaReadDecision


def resolve_effective_media_reference(db, client_id, value, *, context, references=None):
    """One terminal decision; parsers cannot manufacture authoritative owners."""
    root = get_settings().MEDIA_ROOT
    owners = media_reference_owners(db, root) if references is None else references
    if not isinstance(context, MediaReadContext):
        return EffectiveMediaReference(value, context, value, None, None, (), MediaReadDecision.UNKNOWN)
    try:
        effective = _effective_url(value)
        classification, key = canonical_media_reference(effective)
        if classification == "AMBIGUOUS":
            decision, found = MediaReadDecision.AMBIGUOUS, ()
        elif classification == "LOCAL":
            path = resolve_stored_media_path(root, key, must_exist=False)
            found = tuple(sorted(owners.get(path, ()), key=str))
            decision = (MediaReadDecision.SAFE_LOCAL_OWNED
                        if media_owned_by(db, root, path, client_id, owners)
                        else MediaReadDecision.FOREIGN if found else MediaReadDecision.UNKNOWN)
        elif classification == "EXTERNAL":
            decision, found = MediaReadDecision.SAFE_EXTERNAL, ()
        else:
            # A relative path with no document base and active/opaque schemes
            # cannot be proven non-local merely because urlsplit accepts it.
            parsed = urlsplit(effective)
            safe = effective.startswith("/") and not parsed.scheme and not parsed.netloc
            decision, found = (MediaReadDecision.SAFE_EXTERNAL if safe else MediaReadDecision.UNKNOWN), ()
        return EffectiveMediaReference(value, context, value, effective, key, found, decision)
    except (MediaPathError, ValueError, TypeError):
        return EffectiveMediaReference(value, context, value, None, None, (), MediaReadDecision.INVALID)


def _typed_media_keys(value):
    """Interpret an already structured URL without HTML/CSS transformations."""
    classification, key = canonical_media_reference(value)
    if classification == "AMBIGUOUS":
        raise MediaPathError("AMBIGUOUS_MEDIA_REFERENCE")
    if classification == "OTHER" and value and not _effective_url(value).startswith("/"):
        raise MediaPathError("UNKNOWN_EFFECTIVE_MEDIA_REFERENCE")
    return {key} if classification == "LOCAL" else set()


def _opaque_media_keys(value):
    """Unknown text cannot choose the more convenient of two interpretations.

    Legacy free JSON is not a URL schema. Only representation-invariant media
    keys may survive; differing literal/text and embedded keys fail closed.
    This checks ambiguity, never establishes ownership.
    """
    import re
    literal = set()
    stripped = value.strip(_HTML_SPACE)
    whole_url = bool(re.match(r"^(?:https?://|//|/|\./|media/)", stripped)
                     and not re.search(r"[<>]", stripped))
    complete_tokens = True
    if whole_url:
        literal.update(_typed_media_keys(stripped))
    else:
        # A quoted token has explicit bounds independent of HTML/CSS parsing.
        # Validate the complete token, retaining commas and other path data.
        # An escaped/encoded token whose embedded meaning differs still fails
        # the comparison below. Never authorize an unquoted mixed-text match.
        def quoted_url(match):
            token = match.group(2)
            if re.match(r"^(?:https?://|//|/|\./|media/)", token):
                literal.update(_typed_media_keys(token))
                return " " * len(match.group())
            return match.group()

        remainder = re.sub(r'''(["'])(.*?)\1''', quoted_url, value, flags=re.DOTALL)
        for match in re.finditer(r'''(?:https?://|//|/|\./|media/)[^\t\n\f\r "'<>]+''', remainder):
            keys = _typed_media_keys(match.group())
            literal.update(keys)
            if keys:
                complete_tokens = False
    embedded = set(_read_local_media_paths(value, context=MediaReadContext.HTML))
    # Agreement between substring extractors cannot establish the full URL.
    # Only whole URLs / explicitly bounded tokens with invariant keys survive.
    if literal != embedded or (literal and not complete_tokens):
        raise MediaPathError("UNKNOWN_MEDIA_CONTEXT")
    return literal


def _json_key_potential_local(value):
    """Lexical uncertainty only; this never grants ownership or decodes a URI."""
    import re
    components = re.split(r"[/ :]", value)
    return any(re.match(r"^(?:media$|media%|(?:m|me|med|medi|media)%|%[0-9a-fA-F]{2})", component)
               or (component.startswith("%") and "/" in value)
               for component in components)


def _json_key_external_paths(value):
    """Mask only externally owned URI paths in a temporary lexical view.

    Balanced URI parentheses remain path bytes; an unmatched closer belongs to
    surrounding text. Raw query/fragment spans remain available for inspection.
    The original key and exact URI passed to canonicalization are unchanged.
    """
    import re
    chunks, end, candidates = [], 0, 0
    starts = re.compile(r"(?<![A-Za-z0-9._~/-])(?:https?://|//)", re.IGNORECASE)
    pairs = {"(": ")", "[": "]", "{": "}"}
    for match in starts.finditer(value):
        if match.start() < end:
            continue
        candidates += 1
        if candidates > _MEDIA_REFERENCE_LIMIT:
            raise MediaPathError("MEDIA_REFERENCE_LIMIT")
        position, stack = match.start(), []
        while position < len(value):
            if position - match.start() > _MEDIA_URL_LIMIT:
                raise MediaPathError("INVALID_MEDIA_URL")
            char = value[position]
            if char in _HTML_SPACE + "\"'<>&;,=":
                break
            if char in pairs:
                stack.append(pairs[char])
            elif char in ")]}":
                if not stack or stack[-1] != char:
                    break
                stack.pop()
            position += 1
        raw = value[match.start():position]
        if canonical_media_reference(raw)[0] != "EXTERNAL":
            continue
        tail = min((raw.find(c) for c in "?#" if c in raw), default=len(raw))
        protected_end = match.start() + tail
        chunks.extend((value[end:match.start()], " " * (protected_end - match.start())))
        end = protected_end
    return "".join(chunks) + value[end:]


def _json_key_has_embedded_media(value):
    """Detect unbounded local-media spans without inventing a substring URL.

    An unquoted embedded reference has uncertain literal bounds, so the caller
    omits its container. Exact whole/quoted URIs retain canonical ownership.
    At most two decoded views are detection-only and never passed to the
    ownership resolver. Residual URI-shaped encoding remains uncertain.
    """
    import re
    from urllib.parse import unquote
    atoms = re.compile(r'''[^\t\n\f\r "'<>()\[\]{},;=&?#]+''')
    namespace = re.compile(r"(?:^|[^A-Za-z0-9._~-])media(?:/|$)")
    for match in atoms.finditer(_json_key_external_paths(value)):
        token = match.group()
        potential = _json_key_potential_local(token)
        literal_media = bool(namespace.search(token))
        if potential or literal_media or ("%" in token and "/" in token):
            classification, _ = canonical_media_reference(token)
            if classification == "EXTERNAL":
                # Its path is atomic; query/fragment are separate lexical spans.
                continue
            if classification == "LOCAL":
                return True
            if classification == "AMBIGUOUS" and potential:
                return True
            if literal_media:
                return True
        if "%" not in token:
            continue
        detection_view = token
        for _ in range(2):
            if "%" not in detection_view:
                break
            try:
                detection_view = unquote(detection_view, encoding="utf-8", errors="strict")
            except UnicodeError:
                if potential or "/" in detection_view or re.search(r"%(?:25)*2f", detection_view, re.IGNORECASE):
                    return True
                break
            for decoded_match in atoms.finditer(detection_view):
                piece = decoded_match.group()
                if namespace.search(piece):
                    return True
                if "%" in piece and _json_key_potential_local(piece):
                    return True
        # A URI-shaped residual after the fixed detection budget is uncertain;
        # never decode until a desired namespace appears or authorize its result.
        if re.search(r"%[0-9a-fA-F]{2}", detection_view):
            if (("/" in detection_view and "%" in detection_view)
                    or re.search(r"%(?:25)*2f", detection_view, re.IGNORECASE)):
                return True
    return False


def _json_key_check_uri_tail(value, canonical_key=None):
    """Inspect additional references without reinterpreting the main URI path."""
    import re
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE") from exc
    # The main path has already had its single canonical decode. Detect another
    # local namespace in its remaining text without decoding/canonicalizing it.
    if canonical_key and canonical_key.startswith("/media/"):
        remaining_path = canonical_key[len("/media/"):]
        if re.search(r"(?:^|[^A-Za-z0-9._~-])media/", remaining_path):
            raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE")
    # Field boundaries belong to the enclosing raw URI text. An external URI
    # in one field cannot consume the next field before candidate inspection.
    fields = [field for tail in (parsed.query, parsed.fragment) for field in re.split(r"[&;]", tail)]
    if any(_json_key_has_embedded_media(field) for field in fields):
        raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE")


def _json_key_media_keys(value):
    """Literal JSON text: lexical scan plus the shared canonical/owner rule."""
    import re
    if len(value) > _MEDIA_TEXT_LIMIT:
        raise MediaPathError("MEDIA_TEXT_LIMIT")
    if any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise MediaPathError("INVALID_JSON_KEY")
    classification, canonical_key = canonical_media_reference(value)
    if classification == "LOCAL":
        _json_key_check_uri_tail(value, canonical_key)
        return _typed_media_keys(value)
    if classification == "EXTERNAL":
        _json_key_check_uri_tail(value)
        if not re.search(r'''[\t\n\f\r "'<>]''', value.strip(_HTML_SPACE)):
            return set()
    keys = set()

    def quoted_token(match):
        token = match.group(2)
        classification, canonical_key = canonical_media_reference(token)
        stripped = token.strip(_HTML_SPACE)
        prefix = stripped.split("/", 1)[0]
        url_shaped = bool(re.match(r"^(?:https?://|//|/|\./|media/|m%[0-9a-f]|%[0-9a-f]{2})", stripped, re.IGNORECASE)
                          or re.search(r"%2f", stripped, re.IGNORECASE)
                          or ("/" in stripped and "%" in prefix and not re.search(r"\s", prefix)))
        if classification in {"LOCAL", "EXTERNAL"}:
            _json_key_check_uri_tail(token, canonical_key)
            if (classification == "EXTERNAL" and any(c in _HTML_SPACE for c in token.strip(_HTML_SPACE))
                    and _json_key_has_embedded_media(token)):
                raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE")
        if classification == "LOCAL" or (classification == "AMBIGUOUS" and url_shaped):
            keys.update(_typed_media_keys(token))
        elif classification != "EXTERNAL" and _json_key_has_embedded_media(token):
            raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE")
        return " " * len(match.group())

    remainder = re.sub(r'''(["'])(.*?)\1''', quoted_token, value, flags=re.DOTALL)
    if _json_key_has_embedded_media(remainder):
        raise MediaPathError("AMBIGUOUS_JSON_KEY_MEDIA_REFERENCE")
    return keys


def _structured_media_keys(value, *, widget=False, _depth=0):
    """Use widget schema fields only at their declared location, not JSON labels."""
    if _depth > 64 and isinstance(value, (dict, list, tuple)):
        raise MediaPathError("MEDIA_JSON_DEPTH")
    if widget and isinstance(value, dict):
        for field, child in value.items():
            yield from _read_local_media_paths(field, context=MediaReadContext.JSON_KEY)
            if field == "props_json" and isinstance(child, dict):
                for prop, prop_value in child.items():
                    yield from _read_local_media_paths(prop, context=MediaReadContext.JSON_KEY)
                    context = {"sourceUrl": MediaReadContext.TYPED_URL,
                               "htmlContent": MediaReadContext.HTML}.get(prop, MediaReadContext.STRUCTURED)
                    yield from _read_local_media_paths(prop_value, context=context)
            else:
                yield from _read_local_media_paths(child, context=MediaReadContext.STRUCTURED)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _read_local_media_paths(key, context=MediaReadContext.JSON_KEY)
            yield from _structured_media_keys(child, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _structured_media_keys(child, _depth=_depth + 1)
    elif isinstance(value, str):
        yield from _opaque_media_keys(value)


def _read_local_media_paths(value, *, context=None):
    """Explicit consumption context, followed by the one canonical URL rule."""
    if not isinstance(context, MediaReadContext):
        raise MediaPathError("UNKNOWN_MEDIA_CONTEXT")
    if context in {MediaReadContext.STRUCTURED, MediaReadContext.WIDGET}:
        for count, key in enumerate(_structured_media_keys(value, widget=context is MediaReadContext.WIDGET), 1):
            if count > _MEDIA_REFERENCE_LIMIT:
                raise MediaPathError("MEDIA_REFERENCE_LIMIT")
            yield key
        return
    if value is None:
        return
    if not isinstance(value, str):
        raise MediaPathError("INVALID_MEDIA_CONTEXT_VALUE")
    if context is MediaReadContext.JSON_KEY:
        yield from _json_key_media_keys(value)
        return
    if context in {MediaReadContext.TYPED_URL, MediaReadContext.OTHER_STRUCTURED_URL,
                   MediaReadContext.HTML_ATTRIBUTE, MediaReadContext.HTML_BASE,
                   MediaReadContext.CSS_URL, MediaReadContext.CSS_STRING_URL}:
        yield from _typed_media_keys(value)
        return
    if context is MediaReadContext.OPAQUE:
        yield from _opaque_media_keys(value)
        return
    count = 0
    for string in _strings(value):
        candidates = (_srcset_media_urls(string) if context is MediaReadContext.HTML_SRCSET
                      else _css_media_urls(string) if context in {MediaReadContext.CSS, MediaReadContext.CSS_IMAGE_SET}
                      else _html_candidates(string))
        for candidate in candidates:
            count += 1
            if count > _MEDIA_REFERENCE_LIMIT:
                raise MediaPathError("MEDIA_REFERENCE_LIMIT")
            effective = candidate.effective_url if isinstance(candidate, MediaURLCandidate) else candidate
            classification, key = canonical_media_reference(effective)
            if classification == "AMBIGUOUS":
                raise MediaPathError("AMBIGUOUS_MEDIA_REFERENCE")
            if classification == "OTHER" and not _effective_url(effective).startswith("/"):
                raise MediaPathError("UNKNOWN_EFFECTIVE_MEDIA_REFERENCE")
            if classification == "LOCAL":
                yield key


def readable_media_ids(db: Session, model_name: str) -> list[str]:
    """Authoritative media ownership allowlist, built before a response query.

    Free URL/metadata references can disqualify a row; they never add an owner.
    Reads use column snapshots, not ORM setters or mutable response repair.
    """
    root = get_settings().MEDIA_ROOT
    references = media_reference_owners(db, root)
    if model_name == "ContentItem":
        rows = db.execute(select(ContentItem.id, ContentItem.client_id, ContentItem.file_path,
                                 ContentItem.source_url, ContentItem.metadata_json,
                                 ContentItem.html_content, ContentItem.text_content))
    elif model_name == "DatasetImport":
        from sqlalchemy import literal
        rows = db.execute(select(DatasetImport.id, Dataset.client_id, DatasetImport.storage_path,
                                 DatasetImport.source_filename, DatasetImport.summary_json,
                                 literal(None), literal(None)).outerjoin(
                                     Dataset, Dataset.id == DatasetImport.dataset_id))
    else:
        raise ValueError("Unsupported authoritative media model")
    allowed = []
    for identity, owner, stored, source, metadata, html_content, text_content in rows:
        try:
            if not owner:
                continue
            paths = []
            if stored:
                if model_name == "DatasetImport" and not stored.startswith(("/media/", "media/")):
                    stored = "/media/" + stored
                paths.append(stored)
            source_context = (MediaReadContext.TYPED_URL if model_name == "ContentItem"
                              else MediaReadContext.OPAQUE)
            if not all(read_payload_media_owned(db, owner, value, context=context, references=references)
                       for value, context in ((source, source_context), (metadata, MediaReadContext.STRUCTURED),
                           (html_content, MediaReadContext.HTML), (text_content, MediaReadContext.OPAQUE))):
                continue
            if all(media_owned_by(db, root, resolve_stored_media_path(root, value, must_exist=False),
                                  owner, references) for value in paths):
                allowed.append(identity)
        except (MediaPathError, ValueError, TypeError):
            continue
    return allowed


def read_payload_media_decision(db: Session, client_id: str, value, *, context=None, references=None):
    """Terminal aggregate result: any uncertain/foreign candidate rejects all."""
    from urllib.parse import quote
    root = get_settings().MEDIA_ROOT
    owners = media_reference_owners(db, root) if references is None else references
    if not isinstance(context, MediaReadContext):
        return MediaReadDecision.UNKNOWN
    found_local = False
    try:
        for local in _read_local_media_paths(value, context=context):
            # local is already the canonical key, not another URL to parse:
            # encoded '?'/'#' in a filename must not become URL delimiters.
            result = resolve_effective_media_reference(db, client_id, quote(local, safe="/"),
                context=MediaReadContext.OTHER_STRUCTURED_URL, references=owners)
            if result.canonical_media_key != local:
                return MediaReadDecision.AMBIGUOUS
            if result.decision is not MediaReadDecision.SAFE_LOCAL_OWNED:
                return result.decision
            found_local = True
    except MediaPathError:
        return MediaReadDecision.AMBIGUOUS
    except (ValueError, TypeError):
        return MediaReadDecision.INVALID
    return MediaReadDecision.SAFE_LOCAL_OWNED if found_local else MediaReadDecision.SAFE_EXTERNAL


def read_payload_media_owned(db: Session, client_id: str, value, *, context=None, references=None) -> bool:
    """Only the two explicitly safe terminal states can survive a projection."""
    return read_payload_media_decision(db, client_id, value, context=context, references=references) in {
        MediaReadDecision.SAFE_EXTERNAL, MediaReadDecision.SAFE_LOCAL_OWNED}


def readable_payload_ids(db: Session, model_name: str, fields) -> list[str]:
    """Complete opaque-field media allowlist, applied in SQL before pagination.

    Layout revisions are handled widget by widget by the editor adapter, not by
    this row predicate. User text and arbitrary dataset keys are not FK grants.
    """
    from app.services.tenant_queries import _owner
    from app.services.tenant_relations import model_class
    model = model_class(model_name)
    owners = media_reference_owners(db, get_settings().MEDIA_ROOT)
    rows = db.connection().execute(select(model.id, _owner(model, model_name),
                                         *(getattr(model, field) for field in fields)))
    return [row[0] for row in rows if row[1] and all(read_payload_media_owned(
        db, row[1], value, references=owners,
        context=(MediaReadContext.TYPED_URL if field.endswith("_url")
                 else MediaReadContext.STRUCTURED)) for field, value in zip(fields, row[2:]))]


def require_media_owner(db: Session, root: Path, stored_path: str | None, client_id: str) -> None:
    path = resolve_stored_media_path(root, stored_path)
    if path is not None and not media_owned_by(db, root, path, client_id):
        raise MediaPathError("MEDIA_OWNERSHIP_UNVERIFIED")


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
    state = {"paths": None, "connection": None, "engine": None, "sqlite": False, "committed": False,
             "cleanup_paths": set()}
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


def approve_media_cleanup(db: Session, rows) -> list[Path]:
    """Remember typed, unambiguous ownership before the approved rows disappear.

    This evidence exists only in the backend operation; HTTP payloads, directory
    names and arbitrary cleanup path lists cannot create it.
    """
    from app.services.tenant_mutations import require_delete_coverage
    from app.services.tenant_ownership import TenantGraph, OwnerState
    from app.services.tenant_relations import conflict
    rows = tuple(rows)
    if not rows:
        return []
    require_delete_coverage(db, rows)
    state = db.info.get(_STATE)
    if state is None or state["committed"]:
        raise MediaPathError("MEDIA_LOCK_REQUIRED")
    root = get_settings().MEDIA_ROOT
    graph = TenantGraph(db)
    owners = media_reference_owners(db, root)
    approved = []
    for row in rows:
        ref = graph.reference(row)
        if ref.model not in {"ContentItem", "DatasetImport"}:
            raise conflict()
        data = graph.snapshot(ref)
        stored = data["file_path" if ref.model == "ContentItem" else "storage_path"]
        if not stored:
            continue
        if ref.model == "DatasetImport" and not stored.startswith(("/media/", "media/")):
            stored = "/media/" + stored
        path = resolve_stored_media_path(root, stored)
        owner = graph.resolve_owner(ref)
        if path not in (state["paths"] or set()):
            continue  # Do not expand the explicit handler's physical cleanup scope.
        lock_media_paths(db, [path])
        if owner.state == OwnerState.OWNED and media_owned_by(db, root, path, owner.client_id, owners):
            approved.append(path)
    state["cleanup_paths"].update(approved)
    return approved


def approved_media_cleanup_paths(db: Session) -> frozenset[Path]:
    require_media_commit(db)
    return frozenset(db.info[_STATE]["cleanup_paths"])


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


def save_content_reference(content: ContentItem, db: Session, *, uploaded: StoredUpload | None = None) -> None:
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
            if uploaded is None:
                require_media_owner(db, root, new, content.client_id)
            else:
                # Only the authenticated upload route passes this server-created object.
                # It is never read from a request schema, metadata or a URL.
                actual = validate_media_file(root, uploaded.physical_path)
                expected = resolve_stored_media_path(root, new)
                owners = media_reference_owners(db, root).get(actual, set())
                if (not isinstance(uploaded, StoredUpload) or actual != expected
                        or actual.parent != root.resolve() / content.client_id
                        or db.get(Client, content.client_id) is None
                        or owners - {content.client_id}):
                    raise MediaPathError("MEDIA_OWNERSHIP_UNVERIFIED")
            db.add(content)
            db.commit()
