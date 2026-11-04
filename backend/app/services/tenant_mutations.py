"""Immutable proposals and dependency preflight. Execution stays in explicit handlers.

No add/delete/setattr/flush/commit or filesystem operation occurs here. Registry
metadata describes effects; it is never used as a reflective mutation executor.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from app.services.tenant_relations import (
    OPERATIONS, RELATION_SPECS, conflict, model_class, model_spec,
)
from app.services.tenant_ownership import RowRef, TenantGraph, freeze, ContextIntegrityState


def _clean(session):
    if session.new or session.dirty or session.deleted:
        raise conflict()


@dataclass(frozen=True)
class ProposedState:
    reference: RowRef
    previous: object
    values: object
    supplied_fields: frozenset[str]


@dataclass(frozen=True)
class MutationIntent:
    action: str
    reference: RowRef
    patch: tuple[tuple[str, object], ...] = ()

    def __post_init__(self):
        if self.action not in {"CREATE", "UPDATE", "DELETE"} or not isinstance(self.reference, RowRef):
            raise conflict()
        if type(self.patch) is not tuple or len({k for k, _ in self.patch}) != len(self.patch):
            raise conflict()
        object.__setattr__(self, "patch", tuple((k, freeze(v)) for k, v in self.patch))
        if self.action == "DELETE" and self.patch:
            raise conflict()


@dataclass(frozen=True)
class Dependency:
    reference: RowRef
    parent: RowRef
    relation_key: str
    database_effect: str
    orm_effect: str


@dataclass(frozen=True)
class SemanticAdapter:
    """Reviewed backend validator for paths/URLs/opaque payloads, never request data."""
    model: str
    validate: object

    def __post_init__(self):
        model_spec(self.model)
        if not callable(self.validate):
            raise conflict()


class _ProposedGraph(TenantGraph):
    def __init__(self, graph, proposals):
        self.session = graph.session
        self._persisted = graph
        self._proposals = MappingProxyType({p.reference: p.values for p in proposals})

    def snapshot(self, ref):
        ref = self.reference(ref)
        if ref in self._proposals:
            return self._proposals[ref]
        return self._persisted.snapshot(ref)

    def authorize_row(self, ref, context):
        if type(context) is _InternalDeleteScope:
            return _DeleteGraph.authorize_row(self, ref, context)
        if isinstance(self._persisted, _WriteGraph):
            return _WriteGraph.authorize_row(self, ref, context)
        return super().authorize_row(ref, context)

    def row_shape_valid(self, ref, row):
        if isinstance(self._persisted, _WriteGraph):
            return _WriteGraph.row_shape_valid(self, ref, row)
        return super().row_shape_valid(ref, row)

    def collection_children(self, ref, profile):
        from app.services.tenant_relations import COLLECTION_DEPENDENCIES, relation_spec
        refs = set(super().collection_children(ref, profile))
        for key in COLLECTION_DEPENDENCIES:
            edge = relation_spec(key)
            if edge.target != ref.model or profile not in edge.operations:
                continue
            for child, values in self._proposals.items():
                if child.model == edge.model:
                    refs.discard(child)
                    if values.get(edge.field) == ref.identity:
                        refs.add(child)
        return tuple(sorted(refs, key=lambda x: (x.model, x.identity)))


def _semantics(graph, ref, context, adapters):
    if type(adapters) is not tuple or len({a.model for a in adapters}) != len(adapters):
        raise conflict()
    row = graph.snapshot(ref)
    # Scalar credentials/labels are not ownership-bearing media references.
    exempt = {"password_hash", "player_token_hash", "source_filename", "sample_value", "fallback_value"}
    present = any(row.get(k) for k in model_spec(ref.model).opaque_fields if k not in exempt)
    if present:
        adapter = next((a for a in adapters if a.model == ref.model), None)
        if adapter is None or adapter.validate(graph, ref, context, row) is not True:
            raise conflict()


@dataclass(frozen=True)
class MutationPlan:
    context: object
    operation: object
    intents: tuple[MutationIntent, ...]
    proposed: tuple[ProposedState, ...]
    dependencies: tuple[Dependency, ...]
    checked: tuple[RowRef, ...]
    _gate: object
    _transaction: object
    _gate_token: object

    def assert_usable(self, gate, context):
        if (gate is not self._gate or not gate.active or context != self.context
                or gate.graph.session.get_transaction() is not self._transaction
                or gate.token is not self._gate_token):
            raise conflict()
        _clean(gate.graph.session)
        return True


class MutationGate:
    """Transaction-local cooperative writer gate, acquired before fresh snapshots.

    All future writers must participate. This does not protect against external,
    uncooperative SQL writers. Caller owns commit/rollback; this scope does neither.
    """
    def __init__(self, graph, context):
        self.graph, self.context = graph, context
        self.active = False
        self.transaction = None
        self.token = None

    def __enter__(self):
        from sqlalchemy import text
        _clean(self.graph.session)
        if self.active or self.context.client_id is None:
            raise conflict()
        client = RowRef("Client", self.context.client_id)
        self.graph.authorize_row(client, self.context)
        session = self.graph.session
        connection = session.connection()
        dialect = connection.dialect.name
        if dialect == "postgresql":
            digest = sha256(("tecno.tenant.write.v1:" + self.context.client_id).encode()).digest()[:8]
            key = int.from_bytes(digest, "big", signed=True)
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        elif dialect == "sqlite":
            raw = connection.connection.driver_connection
            if raw.in_transaction:
                # An unknown already-started write transaction is not a clean gate.
                raise conflict()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise conflict()
        self.transaction = session.get_transaction()
        self.token = object()
        self.active = True
        return self

    def __exit__(self, *exc):
        self.active = False


def _write_models(graph, operation):
    # A6's endpoint inventory lists directly named models. These explicit A8
    # batches also include rows materialized by the reviewed writer helpers.
    derived = {
        "create_channel": ("DeviceOutput",),
        "update_channel": ("DeviceOutput",),
        "create_revision": ("Layout", "LayoutRevision"),
        "patch_revision": ("Layout", "LayoutRevision"),
        "publish_revision": ("Layout", "LayoutRevision"),
    } if isinstance(graph, _WriteGraph) else {}
    if isinstance(graph, _WriteGraph) and operation.function in {
            "add_playlist_item", "update_playlist_item", "reorder_playlist", "duplicate_playlist_item",
            "add_sequence_item", "update_sequence_item", "reorder_sequence", "duplicate_sequence_item"}:
        derived[operation.function] = ("Campaign", "CampaignPlaylistItem", "CampaignSequenceItem")
    return frozenset(operation.models) | frozenset(derived.get(operation.function, ()))


def _write_method_allowed(graph, operation):
    return (operation.method in {"POST", "PUT", "PATCH", "DELETE"}
            or (isinstance(graph, _WriteGraph) and type(graph.player_scope) is _PlayerWriteScope
                and operation.function == "get_runtime_config" and operation.method == "GET"))


def _build_proposal(graph, ref, patch, context, operation, *, create=False, adapters=()):
    """Persisted old state and copied proposed state, never a partially edited ORM."""
    _clean(graph.session)
    if operation not in OPERATIONS or not _write_method_allowed(graph, operation):
        raise conflict()
    ref = graph.reference(ref)
    if ref.model not in _write_models(graph, operation):
        raise conflict()
    current = graph.snapshot(ref)
    if create:
        if current is not None or (operation.method != "POST" and not isinstance(graph, _WriteGraph)):
            raise conflict()
    else:
        graph.assert_integrity(ref, context)
        _semantics(graph, ref, context, adapters)
    spec = model_spec(ref.model)
    if not isinstance(patch, dict) or any(k not in spec.columns for k in patch):
        raise conflict()
    # Reparenting an authoritative owner is never an incidental update/repair.
    if "id" in patch or (not create and spec.owner_field in patch and patch[spec.owner_field] != current[spec.owner_field]):
        raise conflict()
    proposed = dict(current) if current is not None else {"id": ref.identity}
    proposed.update(patch)
    _clean(graph.session)
    return ProposedState(ref, current, freeze(proposed), frozenset(patch))


def validate_proposed_state(graph, ref, patch, context, operation, *, create=False,
                            semantic_adapters=()):
    proposal = _build_proposal(graph, ref, patch, context, operation, create=create,
                               adapters=semantic_adapters)
    view = _ProposedGraph(graph, (proposal,))
    view.assert_integrity(proposal.reference, context)
    _semantics(view, proposal.reference, context, semantic_adapters)
    return proposal


def preflight_relationship_graph(gate, intents, operation, *, semantic_adapters=()):
    from sqlalchemy import select
    if not isinstance(gate, MutationGate) or not gate.active:
        raise conflict()
    graph, context = gate.graph, gate.context
    _clean(graph.session)
    if (graph.session.get_transaction() is not gate.transaction or type(intents) is not tuple
            or not intents or any(not isinstance(i, MutationIntent) for i in intents)
            or len({i.reference for i in intents}) != len(intents)
            or operation not in OPERATIONS
            or not _write_method_allowed(graph, operation)):
        raise conflict()
    proposed = []
    roots = {i.reference for i in intents}
    for intent in intents:
        if intent.reference.model not in _write_models(graph, operation):
            raise conflict()
        proposed.append(_build_proposal(
            graph, intent.reference, dict(intent.patch), context, operation,
            create=intent.action == "CREATE", adapters=semantic_adapters,
        ))
    view = _ProposedGraph(graph, proposed)
    for proposal in proposed:
        view.assert_integrity(proposal.reference, context)
        _semantics(view, proposal.reference, context, semantic_adapters)
    # Incoming discovery deliberately has NO tenant predicate: filtering by owner
    # here would hide exactly the foreign dependencies this guard must catch.
    queue = list(sorted(roots, key=lambda r: (r.model, r.identity)))
    checked, context_checked, dependencies = set(), set(), []
    with graph.session.no_autoflush:
        while queue:
            ref = queue.pop(0)
            if ref in checked:
                continue
            if graph.snapshot(ref) is not None:
                graph.assert_integrity(ref, context, profile="DELETE")
                _semantics(graph, ref, context, semantic_adapters)
            view.assert_integrity(ref, context, profile="DELETE")
            proof = view.context_integrity(ref, context, profile="DELETE")
            if proof.state != ContextIntegrityState.CONSISTENT:
                raise conflict()
            context_checked.update(proof.checked)
            if isinstance(graph, _WriteGraph):
                for related in proof.checked:
                    if graph.snapshot(related) is not None:
                        _semantics(graph, related, context, semantic_adapters)
                    _semantics(view, related, context, semantic_adapters)
            _semantics(view, ref, context, semantic_adapters)
            checked.add(ref)
            for edge in RELATION_SPECS:
                if edge.target != ref.model:
                    continue
                if (isinstance(graph, _WriteGraph) and edge.key == "LayoutRevision.created_by_user_id"
                        and ref.model == "User"
                        and graph.resolve_owner(ref).state.value == "GLOBAL_PRINCIPAL"
                        and view.resolve_owner(ref).state.value == "GLOBAL_PRINCIPAL"):
                    # Attribution to a global principal remains valid after a
                    # name/status/role change within global roles. These revisions
                    # belong to their layouts, not to the User being updated.
                    continue
                cls = model_class(edge.model)
                ids = set(graph.session.scalars(
                    select(cls.id).where(getattr(cls, edge.field) == ref.identity).order_by(cls.id)
                ).all())
                ids.update(p.reference.identity for p in proposed
                           if p.reference.model == edge.model and p.values.get(edge.field) == ref.identity)
                for identity in sorted(ids):
                    child = RowRef(edge.model, identity)
                    if graph.snapshot(child) is not None:
                        graph.assert_integrity(child, context, profile="DELETE")
                    view.assert_integrity(child, context, profile="DELETE")
                    dependencies.append(Dependency(
                        child, ref, edge.key, edge.on_delete or "RESTRICT", edge.orm_cascade,
                    ))
                    if child not in checked:
                        queue.append(child)
    _clean(graph.session)
    return MutationPlan(
        context, operation, intents, tuple(proposed), tuple(dependencies),
        tuple(sorted(checked | context_checked, key=lambda r: (r.model, r.identity))), gate, gate.transaction, gate.token,
    )


# A7 execution boundary. Discovery uses the registry; execution remains in the
# explicit SQL/ORM handlers. Internal maintenance proves relationships, never
# impersonates an authenticated User or supplies HTTP authorization.
from contextlib import contextmanager, ExitStack


def _prepared_row_versions(db, intents):
    """Capture only identities already used by the handler's pure preparation.

    A copied proposal may derive several values from an ORM snapshot. Reject a
    stale copy after locking instead of overwriting a concurrent committed edit.
    This does not refresh, assign or flush any ORM attribute.
    """
    from sqlalchemy import inspect
    wanted = {intent.reference for intent in intents if intent.action != "CREATE"}
    versions = {}
    for row in db.identity_map.values():
        state = inspect(row)
        ref = RowRef(state.mapper.class_.__name__, state.identity[0])
        if ref in wanted:
            columns = model_spec(ref.model).columns
            versions[ref] = freeze({key: state.dict[key] for key in columns if key in state.dict})
    return versions


def _same_prepared_value(left, right):
    from datetime import datetime, UTC
    if isinstance(left, datetime) and isinstance(right, datetime):
        left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
        right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return left == right


@dataclass(frozen=True)
class _InternalDeleteScope:
    client_id: str
    branch_id: str | None = None
    parent: object = None


class _DeleteGraph(TenantGraph):
    def __init__(self, session):
        super().__init__(session)
        self.snapshots = {}
        self.locking = False

    def snapshot(self, row):
        from sqlalchemy import select
        ref = self.reference(row)
        if ref not in self.snapshots:
            cls = model_class(ref.model)
            statement = select(*(getattr(cls, k) for k in model_spec(ref.model).columns)).where(cls.id == ref.identity)
            if self.locking and self.session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update()
            with self.session.no_autoflush:
                value = self.session.execute(statement).mappings().one_or_none()
            self.snapshots[ref] = freeze(dict(value)) if value is not None else None
        return self.snapshots[ref]

    def authorize_row(self, ref, context):
        if type(context) is _InternalDeleteScope:
            from app.services.tenant_ownership import OwnerState
            owner = self.resolve_owner(ref, context.client_id)
            if owner.state != OwnerState.OWNED:
                raise conflict()
            return owner
        return super().authorize_row(ref, context)


@contextmanager
def write_guard(db, *, actor, client_id, operation, intents, semantic_adapters=()):
    """Approve a complete write batch before the handler's first ORM assignment.

    Uses the A6 tenant gate and A7 fresh, locking snapshots. Locks live until
    transaction end, including after this Python scope exits. This function
    does not execute the plan or repair historical relationships.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.services.tenant_ownership import context_for_actor, enum_value

    _clean(db)
    spec = next((item for item in OPERATIONS if item.function == operation), None)
    if spec is None or spec.method not in {"POST", "PUT", "PATCH"}:
        raise conflict()
    actor_before = (enum_value(actor.role), actor.client_id, actor.branch_id)
    context = context_for_actor(db, actor, client_id=client_id)
    if actor_before != (context.actor.role, context.actor.client_id, context.actor.branch_id):
        raise conflict()
    graph = _WriteGraph(db)
    graph.new_content_refs = frozenset(intent.reference for intent in intents
                                      if intent.action == "CREATE" and intent.reference.model == "ContentItem")
    prepared_versions = _prepared_row_versions(db, intents)
    if operation == "create_client" and len(intents) == 1:
        intent = intents[0]
        if intent.action == "CREATE" and intent.reference.model == "Client" and context.client_id is None:
            graph.new_roots = frozenset((intent.reference,))
    supplied = {adapter.model: adapter for adapter in semantic_adapters}
    if len(supplied) != len(semantic_adapters):
        raise conflict()
    semantic_adapters = tuple(supplied.get(adapter.model, adapter) for adapter in _write_adapters())
    actor_ref = graph.reference(actor)
    if db.get_bind().dialect.name == "postgresql":
        connection = db.connection()
        if connection.get_isolation_level() != "READ COMMITTED":
            raise conflict()
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
    try:
        if any(intent.reference.model == "User" for intent in intents):
            from app.services.user_admin import lock_super_admin_delete_set
            lock_super_admin_delete_set(db)
        gate_type = _GlobalWriteGate if context.client_id is None else MutationGate
        with gate_type(graph, context) as gate:
            graph.snapshots.clear()
            graph.locking = True
            graph.snapshot(actor_ref)
            if context_for_actor(db, actor, client_id=client_id) != context:
                raise conflict()
            for ref, previous in prepared_versions.items():
                current = graph.snapshot(ref)
                if current is None or any(not _same_prepared_value(value, current.get(key))
                                          for key, value in previous.items()):
                    raise conflict()
            # Each snapshot is SELECT ... FOR UPDATE on PostgreSQL. Incoming
            # references committed before their anchor lock enter discovery;
            # subsequent FK writers wait until this transaction ends.
            plan = preflight_relationship_graph(gate, intents, spec,
                                                semantic_adapters=semantic_adapters)
            with ExitStack() as media_locks:
                paths = frozenset(graph.media_paths)
                if paths:
                    from app.services.media_references import media_reference_operation, lock_media_paths
                    media_locks.enter_context(media_reference_operation(db))
                    lock_media_paths(db, list(paths))
                graph.snapshots.clear()
                graph.media_paths.clear()
                graph.media_validation_locked = True
                confirmed = preflight_relationship_graph(gate, intents, spec,
                                                         semantic_adapters=semantic_adapters)
                if (plan.proposed != confirmed.proposed or plan.checked != confirmed.checked
                        or plan.dependencies != confirmed.dependencies or paths != frozenset(graph.media_paths)):
                    raise conflict()
                confirmed.assert_usable(gate, context)
                db.expire_all()
                yield confirmed
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01", "55P03"}:
            raise
        db.rollback()
        raise conflict() from exc


@dataclass(frozen=True)
class _PlayerWriteScope:
    client_id: str
    branch_id: str | None
    parent: object = None
    parent_relation: object = None


@contextmanager
def player_write_guard(db, *, channel, player_device, operation, intents):
    """Relationship proof for the already authenticated Player/code entrypoints.

    Never constructs a User or accepts actor/tenant identity from HTTP payloads.
    The channel and existing device must belong to this exact database session.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError
    from app.models.entities import Channel, PlayerDevice

    _clean(db)
    if type(channel) is not Channel:
        raise conflict()
    channel_state = inspect(channel)
    if not channel_state.persistent or channel_state.session is not db:
        raise conflict()
    models = {
        "activate_player": {"Channel", "PlayerDevice"},
        "create_player_heartbeat": {"Channel", "PlayerDevice", "PlayerHeartbeat"},
        "create_player_playback_event": {"PlayerDevice", "PlayerPlaybackEvent"},
        "get_runtime_config": {"PlayerDevice"},
    }
    if operation not in models or any(i.reference.model not in models[operation] for i in intents):
        raise conflict()
    if player_device is None and operation != "activate_player":
        raise conflict()
    if player_device is not None:
        state = inspect(player_device)
        if type(player_device) is not PlayerDevice or not state.persistent or state.session is not db:
            raise conflict()
        if (player_device.channel_id != channel.id or player_device.client_id != channel.client_id
                or player_device.branch_id != channel.branch_id or not player_device.is_active):
            raise conflict()
    graph = _WriteGraph(db)
    context = _PlayerWriteScope(channel.client_id, channel.branch_id)
    graph.player_scope = context
    spec = next(o for o in OPERATIONS if o.function == operation)
    prepared = _prepared_row_versions(db, intents)
    channel_values = (channel.client_id, channel.branch_id, channel.channel_code)
    device_hash = player_device.player_token_hash if player_device else None
    if db.get_bind().dialect.name == "postgresql":
        conn = db.connection()
        if conn.get_isolation_level() != "READ COMMITTED":
            raise conflict()
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    try:
        with MutationGate(graph, context) as gate:
            graph.snapshots.clear()
            graph.locking = True
            current = graph.snapshot(RowRef("Channel", channel.id))
            if current is None or tuple(current[key] for key in ("client_id", "branch_id", "channel_code")) != channel_values:
                raise conflict()
            # ORM identities may have been loaded before a concurrent suspension.
            # Recheck active scope from the locked database snapshots as well.
            client_row = graph.snapshot(RowRef("Client", current["client_id"]))
            branch_row = graph.snapshot(RowRef("Branch", current["branch_id"]))
            if (client_row is None or client_row["status"] != "active" or branch_row is None
                    or not branch_row["is_active"] or branch_row["client_id"] != current["client_id"]):
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Player operation unavailable")
            if player_device is not None:
                current_device = graph.snapshot(RowRef("PlayerDevice", player_device.id))
                if (current_device is None or not current_device["is_active"]
                        or current_device["player_token_hash"] != device_hash):
                    raise conflict()
            for ref, previous in prepared.items():
                current = graph.snapshot(ref)
                if current is None or any(not _same_prepared_value(value, current.get(key)) for key, value in previous.items()):
                    raise conflict()
            plan = preflight_relationship_graph(gate, intents, spec, semantic_adapters=_write_adapters())
            with ExitStack() as media_locks:
                paths = frozenset(graph.media_paths)
                if paths:
                    from app.services.media_references import media_reference_operation, lock_media_paths
                    media_locks.enter_context(media_reference_operation(db))
                    lock_media_paths(db, list(paths))
                graph.snapshots.clear()
                graph.media_paths.clear()
                graph.media_validation_locked = True
                confirmed = preflight_relationship_graph(gate, intents, spec, semantic_adapters=_write_adapters())
                if (plan.proposed != confirmed.proposed or plan.checked != confirmed.checked
                        or plan.dependencies != confirmed.dependencies or paths != frozenset(graph.media_paths)):
                    raise conflict()
                confirmed.assert_usable(gate, context)
                db.expire_all()
                yield confirmed
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01", "55P03"}:
            raise
        db.rollback()
        raise conflict() from exc


class _WriteGraph(_DeleteGraph):
    def __init__(self, session):
        super().__init__(session)
        self.media_paths = set()
        self.new_roots = frozenset()
        self.player_scope = None
        self.new_content_refs = frozenset()
        self.media_validation_locked = False

    def authorize_row(self, ref, context):
        from app.services.tenant_ownership import OwnerState
        from app.services.tenant_relations import GLOBAL_ROLES
        owner = self.resolve_owner(ref, context.client_id)
        graph = self._persisted if isinstance(self, _ProposedGraph) else self
        if type(context) is _PlayerWriteScope:
            if context is not graph.player_scope or owner.state != OwnerState.OWNED:
                raise conflict()
            branch = self.branch(ref)
            if branch is not None and branch != context.branch_id:
                raise conflict()
            return owner
        if (ref in graph.new_roots and ref.model == "Client" and context.client_id is None
                and context.actor.role in GLOBAL_ROLES and owner.state == OwnerState.OWNED):
            return owner
        if owner.state == OwnerState.GLOBAL_PRINCIPAL:
            if context.actor.role not in GLOBAL_ROLES:
                raise conflict()
            return owner
        return TenantGraph.authorize_row(self, ref, context)

    def row_shape_valid(self, ref, row):
        from app.services.tenant_ownership import enum_value
        from app.services.tenant_relations import BRANCH_ROLES
        if ref.model == "User" and enum_value(row.get("role")) in BRANCH_ROLES:
            if not row.get("branch_id"):
                return False
        return TenantGraph.row_shape_valid(self, ref, row)


def _write_semantics(graph, ref, context, row):
    """Explicit opaque-reference consumers, independent of actor privileges.

    Labels, telemetry and tabular data never establish ownership. Only typed
    persisted media references can prove local media ownership. No HTTP fetch.
    """
    from collections.abc import Mapping
    from app.core.config import get_settings
    from app.services.media_references import _local_url, media_owned_by
    from app.services.media_storage import MediaPathError, resolve_stored_media_path

    owner = graph.resolve_owner(ref)
    root = get_settings().MEDIA_ROOT
    base = graph._persisted if isinstance(graph, _ProposedGraph) else graph

    def local_media(value, *, stored=False):
        if not value:
            return True
        if not isinstance(value, str):
            return False
        try:
            local = value if stored else _local_url(value)
            if not local:
                return True
            path = resolve_stored_media_path(root, local, must_exist=False)
            if path is None:
                return False
            base.media_paths.add(path)
            # Discovery collects the complete lock batch. Ownership is decided
            # again only after those locks, before the plan can be yielded.
            return (not base.media_validation_locked
                    or media_owned_by(graph.session, root, path, owner.client_id))
        except MediaPathError:
            return False

    def references(value, *, layout_id=None):
        if isinstance(value, Mapping):
            for key, child in value.items():
                target_model = {"content_id": "ContentItem", "dataset_id": "Dataset",
                                "layout_id": "Layout", "binding_id": "LayoutDataBinding"}.get(key)
                if target_model and child:
                    if type(child) is not str:
                        return False
                    target = RowRef(target_model, child)
                    target_owner = graph.resolve_owner(target)
                    if target_owner.client_id != owner.client_id or not target_owner.client_id:
                        return False
                    graph.assert_integrity(target, context)
                    target_row = graph.snapshot(target)
                    if target_model == "LayoutDataBinding" and layout_id and target_row["layout_id"] != layout_id:
                        return False
                    if target_model == "ContentItem" and not local_media(target_row.get("file_path"), stored=True):
                        return False
                if not references(child, layout_id=layout_id):
                    return False
        elif isinstance(value, (list, tuple)):
            return all(references(child, layout_id=layout_id) for child in value)
        elif isinstance(value, str):
            return local_media(value)
        return True

    if ref.model == "ContentItem":
        if ref in base.new_content_refs and base.media_validation_locked:
            from fastapi import HTTPException
            from app.services.media_references import require_media_owner
            try:
                require_media_owner(graph.session, root, row.get("file_path"), owner.client_id)
            except MediaPathError as exc:
                raise HTTPException(status_code=409, detail=exc.code) from exc
        # Free metadata/source_url remain non-authoritative input. They never
        # grant ownership to file_path or serve as a trusted upload capability.
        return local_media(row.get("file_path"), stored=True)
    if ref.model == "DatasetImport":
        path = row.get("storage_path")
        if path and not path.startswith(("/media/", "media/")):
            path = "/media/" + path
        return local_media(path, stored=True)
    if ref.model == "LayoutRevision":
        return references((row.get("editor_state_json"), row.get("preview_state_json")), layout_id=row["layout_id"])
    if ref.model == "Layout":
        return references(row.get("zones"), layout_id=ref.identity)
    if ref.model == "KioskButton":
        if not _delete_semantics(graph, ref, context, row):
            return False
        return references((row.get("action_value"), row.get("action_payload_json"), row.get("style_json")))
    if ref.model in {"KioskScreen", "TouchLocation", "TouchMap", "TouchExperience",
                     "TouchExperienceAssignment", "CampaignSequenceItem", "LayoutDataBinding", "LayoutBindingField"}:
        return all(references(row.get(field)) for field in model_spec(ref.model).opaque_fields)
    if ref.model == "Schedule":
        days = row.get("days_of_week") or ()
        return isinstance(days, (list, tuple)) and all(type(day) is int for day in days)
    # These values are settings, telemetry, scalar credentials or tabular data;
    # none is interpreted as a resource owner or an executable relationship.
    return ref.model in {"Channel", "DataSource", "DatasetColumn", "DatasetRow",
                         "PlayerDevice", "PlayerHeartbeat", "PlayerPlaybackEvent", "User"}


def _write_adapters():
    from app.services.tenant_relations import MODEL_SPECS
    return tuple(SemanticAdapter(spec.name, _write_semantics) for spec in MODEL_SPECS if spec.opaque_fields)


class _GlobalWriteGate(MutationGate):
    """Explicit global-principal/root operations; never a tenant bypass."""
    def __enter__(self):
        from sqlalchemy import text
        from app.services.tenant_relations import GLOBAL_ROLES
        _clean(self.graph.session)
        if self.active or self.context.client_id is not None or self.context.actor.role not in GLOBAL_ROLES:
            raise conflict()
        connection = self.graph.session.connection()
        if connection.dialect.name == "postgresql":
            key = int.from_bytes(sha256(b"tecno.global.write.v1").digest()[:8], "big", signed=True)
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        elif connection.dialect.name == "sqlite":
            if connection.connection.driver_connection.in_transaction:
                raise conflict()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise conflict()
        self.transaction = self.graph.session.get_transaction()
        self.token, self.active = object(), True
        return self


def _delete_semantics(graph, ref, context, row):
    """DELETE never interprets opaque JSON as a command, owner or unlink target.

    Physical candidates come only from typed Content/DatasetImport references
    and are separately ownership checked by the post-commit media protocol.
    A legacy screen action is the one non-FK relational target used by handlers.
    """
    if ref.model == "KioskButton" and row.get("action_type") == "switch_screen":
        value = row.get("action_value")
        if value:
            target = RowRef("KioskScreen", value)
            source = graph.snapshot(RowRef("KioskScreen", row["screen_id"]))
            destination = graph.snapshot(target)
            if (destination is None or source is None
                    or destination["experience_id"] != source["experience_id"]):
                return False
            graph.assert_integrity(target, context, profile="DELETE")
    return True


def _delete_adapters():
    from app.services.tenant_relations import MODEL_SPECS
    return tuple(SemanticAdapter(s.name, _delete_semantics) for s in MODEL_SPECS if s.opaque_fields)


def _effect_roots(graph, roots, operation):
    """Explicit manual effects not implied by an incoming FK traversal."""
    from sqlalchemy import select
    from app.models.entities import Channel, Videowall, VideowallNode
    result = set(roots)
    with graph.session.no_autoflush:
        for ref in roots:
            row = graph.snapshot(ref)
            if row is None:
                raise conflict()
            if ref.model == "Branch":
                channels = set(graph.session.scalars(select(Channel.id).where(Channel.branch_id == ref.identity)))
                walls = set(graph.session.scalars(select(VideowallNode.videowall_id).where(VideowallNode.channel_id.in_(channels))))
                walls.update(graph.session.scalars(select(Videowall.id).where(Videowall.primary_channel_id.in_(channels))))
                result.update(RowRef("Channel", value) for value in channels)
                result.update(RowRef("Videowall", value) for value in walls)
            if ref.model == "Videowall" and operation == "delete_videowall":
                result.update(RowRef("Channel", value) for value in graph.session.scalars(
                    select(VideowallNode.channel_id).where(VideowallNode.videowall_id == ref.identity)))
                if row["primary_channel_id"]:
                    result.add(RowRef("Channel", row["primary_channel_id"]))
            if ref.model == "VideowallNode" and operation == "delete_node_screen":
                result.add(RowRef("Channel", row["channel_id"]))
    return tuple(sorted(result, key=lambda r: (r.model, r.identity)))


def _lock_delete_anchors(graph, references):
    """Lock each preliminary FK anchor before rediscovery, in model/id order.

    FOR UPDATE conflicts with PostgreSQL's FK KEY SHARE. Inserts committed
    before this lock are visible to the following READ COMMITTED discovery;
    inserts/updates referring to a locked anchor must wait until transaction end.
    Newly discovered anchors are locked by the graph before their incoming scan.
    """
    from sqlalchemy import select
    if graph.session.get_bind().dialect.name == "postgresql":
        for ref in sorted(set(references), key=lambda r: (r.model, r.identity)):
            cls = model_class(ref.model)
            if graph.session.scalar(select(cls.id).where(cls.id == ref.identity).with_for_update()) is None:
                raise conflict()


def _locked_delete_plan(gate, initial, operation, discovery_spec):
    graph = gate.graph
    preliminary = _effect_roots(graph, initial, operation)
    _lock_delete_anchors(graph, preliminary)
    graph.locking = True
    # A new structural child may have committed before its parent's lock. Its
    # manual effects must be rediscovered after that child is locked as well.
    for _ in range(16):
        graph.snapshots.clear()
        expanded = _effect_roots(graph, initial, operation)
        intents = tuple(MutationIntent("DELETE", ref) for ref in expanded)
        plan = preflight_relationship_graph(gate, intents, discovery_spec, semantic_adapters=_delete_adapters())
        graph.snapshots.clear()
        rediscovered = _effect_roots(graph, initial, operation)
        if rediscovered != expanded:
            continue
        confirmed = preflight_relationship_graph(gate, tuple(MutationIntent("DELETE", ref) for ref in rediscovered),
                                                  discovery_spec, semantic_adapters=_delete_adapters())
        if (plan.checked != confirmed.checked or plan.dependencies != confirmed.dependencies
                or plan.proposed != confirmed.proposed):
            raise conflict()
        confirmed.assert_usable(gate, gate.context)
        return confirmed
    raise conflict()


_DELETE_STATE = "tenant_a7_delete_plan"


@dataclass(frozen=True)
class DeleteApproval:
    roots: tuple
    plans: tuple
    covered: frozenset
    transaction: object


def require_delete_coverage(db, rows):
    """Nested explicit cleanup may consume, never expand, the approved batch."""
    approval = db.info.get(_DELETE_STATE)
    graph = TenantGraph(db)
    if (not isinstance(approval, DeleteApproval)
            or approval.transaction is not db.get_transaction()
            or any(graph.reference(row) not in approval.covered for row in rows)):
        raise conflict()
    return approval


@contextmanager
def _approved_delete_execution(db, approval):
    """Explicit handlers execute; this boundary cannot expand their target set.

    Bulk DELETE/UPDATE is checked and constrained to immutable approved IDs.
    ORM cascades are checked before flush. FK anchors remain locked through
    commit, including for DB cascades/SET NULL that do not emit ORM statements.
    New playlist mirror INSERTs keep their existing contract (A8 write scope).
    """
    from sqlalchemy import event, inspect, select
    from app.services.tenant_relations import MODEL_SPECS
    by_table = {s.table: s.name for s in MODEL_SPECS}

    def bulk(state):
        if not (state.is_delete or state.is_update):
            return
        if db.get_transaction() is not approval.transaction:
            raise conflict()
        statement = state.statement
        name = by_table.get(statement.table.name)
        if name is None or state.is_executemany:
            raise conflict()
        cls = model_class(name)
        allowed = tuple(r.identity for r in approval.covered if r.model == name)
        query = select(cls.id).where(*statement._where_criteria)
        with db.no_autoflush:
            actual = set(db.scalars(query, state.parameters or {}))
        if not actual.issubset(allowed):
            raise conflict()
        state.statement = statement.where(cls.id.in_(allowed))

    def flush(session, *_):
        if db.get_transaction() is not approval.transaction:
            raise conflict()
        changed = tuple(session.deleted) + tuple(
            row for row in session.dirty
            if any(inspect(row).attrs[attr.key].history.has_changes() for attr in inspect(row).mapper.column_attrs)
        )
        require_delete_coverage(db, changed)

    event.listen(db, "do_orm_execute", bulk)
    event.listen(db, "before_flush", flush)
    try:
        yield
    finally:
        event.remove(db, "do_orm_execute", bulk)
        event.remove(db, "before_flush", flush)


@contextmanager
def delete_guard(db, rows, *, actor=None, operation="delete_client", branch_id=None):
    """Authorize once, lock, prove the full batch, then allow explicit effects.

    Tenant advisory gates precede concrete FOR UPDATE locks, followed by media
    locks in callers. Parent row locks prevent new FK references; dependent and
    context row locks prevent ownership changes until commit. No reflective DML.
    """
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError
    from fastapi import HTTPException
    from app.services.tenant_ownership import OwnerState, context_for_actor
    from app.services.tenant_relations import GLOBAL_ROLES
    rows = tuple(rows)
    if _DELETE_STATE in db.info:
        yield require_delete_coverage(db, rows)
        return
    _clean(db)
    if not rows:
        yield None
        return
    if db.get_bind().dialect.name == "postgresql":
        connection = db.connection()
        if connection.get_isolation_level() != "READ COMMITTED":
            if operation in {"delete_content", "delete_content_folder", "delete_client"}:
                # Preserve the existing media isolation error while rejecting it
                # earlier, before graph discovery or any possible mutation.
                raise HTTPException(status_code=409, detail="MEDIA_LOCK_REQUIRES_READ_COMMITTED")
            raise conflict()
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
    plain = TenantGraph(db)
    roots = tuple(sorted({plain.reference(row) for row in rows}, key=lambda r: (r.model, r.identity)))
    owners = {ref: plain.resolve_owner(ref) for ref in roots}
    if any(o.state not in {OwnerState.OWNED, OwnerState.GLOBAL_PRINCIPAL} for o in owners.values()):
        raise conflict()
    # Internal jobs get relationship-only scopes. HTTP callers must pass their
    # authenticated user, after preserving the existing primary permission check.
    contexts = {}
    for owner in owners.values():
        if owner.state == OwnerState.OWNED and owner.client_id not in contexts:
            contexts[owner.client_id] = (context_for_actor(db, actor, client_id=owner.client_id, branch_id=branch_id)
                                        if actor is not None else _InternalDeleteScope(owner.client_id))
    operation_spec = next((o for o in OPERATIONS if o.function == operation), None)
    if operation_spec is None:
        raise conflict()
    # The root delete operation catalog covers maintenance's complete model set.
    discovery_spec = next(o for o in OPERATIONS if o.function == "delete_client")
    try:
        with ExitStack() as stack:
            # Acquire the shared invariant rows BEFORE locking an individual
            # principal, otherwise two own-target locks could invert this order.
            if any(ref.model == "User" for ref in roots):
                from app.services.user_admin import lock_super_admin_delete_set
                lock_super_admin_delete_set(db)
            gates = {}
            for client_id, context in sorted(contexts.items()):
                graph = _DeleteGraph(db)
                gate = MutationGate(graph, context)
                if gates and db.get_bind().dialect.name == "sqlite":
                    # The first gate already owns BEGIN IMMEDIATE for this exact
                    # session/transaction; all scopes share that database lock.
                    graph.authorize_row(RowRef("Client", client_id), context)
                    gate.transaction, gate.token, gate.active = db.get_transaction(), object(), True
                    stack.callback(setattr, gate, "active", False)
                    gates[client_id] = gate
                else:
                    gates[client_id] = stack.enter_context(gate)
                graph.snapshots.clear()  # reread after the tenant gate
            principals = [r for r in roots if owners[r].state == OwnerState.GLOBAL_PRINCIPAL]
            if principals:
                if actor is not None and context_for_actor(db, actor).actor.role not in GLOBAL_ROLES:
                    raise conflict()
                connection = db.connection()
                if connection.dialect.name == "postgresql":
                    for ref in principals:
                        key = int.from_bytes(sha256(("tecno.principal.delete.v1:" + ref.identity).encode()).digest()[:8], "big", signed=True)
                        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                elif not gates and connection.dialect.name == "sqlite":
                    raw = connection.connection.driver_connection
                    if raw.in_transaction:
                        raise conflict()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                principal_graph = _DeleteGraph(db)
                principal_graph.locking = True
                for ref in principals:
                    if principal_graph.resolve_owner(ref).state != OwnerState.GLOBAL_PRINCIPAL:
                        raise conflict()
                    for edge in RELATION_SPECS:
                        if edge.target == ref.model:
                            cls = model_class(edge.model)
                            if db.scalar(select(cls.id).where(getattr(cls, edge.field) == ref.identity).limit(1)):
                                raise conflict()
            plans = []
            for client_id, gate in gates.items():
                initial = tuple(r for r in roots if owners[r].client_id == client_id)
                plans.append(_locked_delete_plan(gate, initial, operation, discovery_spec))
            # Context ancestors were read/locked to prove ownership, not approved
            # for deletion. Nested cleanup may consume only actual planned effects.
            covered = frozenset(principals).union(*(
                frozenset(i.reference for i in p.intents) | frozenset(d.reference for d in p.dependencies)
                for p in plans
            ))
            approval = DeleteApproval(roots, tuple(plans), covered, db.get_transaction())
            _clean(db)
            db.expire_all()  # handlers cannot reuse identity-map values from before the locks
            db.info[_DELETE_STATE] = approval
            try:
                with _approved_delete_execution(db, approval):
                    yield approval
            finally:
                db.info.pop(_DELETE_STATE, None)
    except HTTPException as exc:
        if exc.status_code == 409 and exc.detail == "TENANT_RELATION_CONFLICT":
            import logging
            logging.getLogger("app.services.tenancy").warning("TENANT_RELATION_CONFLICT")
        raise
    except DBAPIError as exc:
        # Lock/deadlock/serialization failures are controlled conflicts, never a
        # retry with a stale plan. Unexpected database errors remain visible.
        if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01", "55P03"}:
            raise
        db.rollback()
        raise conflict() from exc
