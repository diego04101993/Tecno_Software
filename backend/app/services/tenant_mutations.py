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
from app.services.tenant_ownership import RowRef, TenantGraph, freeze, ContextIntegrityState, OperationContext


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


class _SnapshotStore(dict):
    """Observe explicit snapshot replacement without changing stored facts."""
    def __init__(self):
        super().__init__()
        self.generation = 0

    def __setitem__(self, key, value):
        if key in self and self[key] is not value:
            self.generation += 1
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self.generation += 1
        super().__delitem__(key)

    def clear(self):
        self.generation += 1
        super().clear()

    def pop(self, key, *default):
        if key in self:
            self.generation += 1
        return super().pop(key, *default)

    def popitem(self):
        self.generation += 1
        return super().popitem()

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value


class _GraphValidationMemo:
    """Pure facts over locked, immutable snapshots in one preflight only.

    Incoming discovery, semantics, file validation and authorization still run.
    No cache exists while the handler is assigning/flushing/committing rows.
    """
    def __init__(self, graph, authority, gate):
        self.graph, self.authority = graph, authority
        self.gate = gate
        self.gate_token = gate.token if gate is not None else None
        self.media_validation_locked = getattr(graph, "media_validation_locked", None)
        self.generation = graph.snapshots.generation
        self.caches = {}
        self.views = {}
        # Replay obligations survive DECLINE: clearing a reusable result must
        # never erase a file validation already coalesced during this phase.
        self.semantic_obligations = {}

    def clear(self):
        self.caches.clear()
        self.views.clear()

    def check(self):
        self.authority.check()
        if (self.graph.snapshots.generation != self.generation
                or getattr(self.graph, "media_validation_locked", None) is not self.media_validation_locked
                or (self.gate is not None and (not self.gate.active or self.gate.graph is not self.graph
                    or self.gate.transaction is not self.authority.transaction
                    or self.gate.token is not self.gate_token))):
            self.authority.invalidate()
            raise conflict()

    def locked(self, ref):
        row = self.graph.snapshots.get(ref)
        proof = self.graph._snapshot_lock_proofs.get(ref)
        return (self.graph.locking and type(row) is MappingProxyType
                and proof is not None and proof[0] is row
                and proof[1] is self.authority.transaction
                and self.gate is not None and self.gate.graph is self.graph
                and self.gate.active and self.gate.transaction is self.authority.transaction)

    def owner_chain_locked(self, view, ref):
        from app.services.tenant_relations import GLOBAL_ROLES
        from app.services.tenant_ownership import enum_value
        visited = set()
        while ref not in visited:
            visited.add(ref)
            if not self.locked(ref):
                return False
            row = view.snapshot(ref)
            if row is None:
                return False
            spec = model_spec(ref.model)
            if spec.owner_kind == "root" or (ref.model == "User" and enum_value(row.get("role")) in GLOBAL_ROLES):
                return True
            identity = row.get(spec.owner_field)
            if type(identity) is not str or not identity:
                return False
            ref = RowRef(spec.owner_target, identity)
        return False

    def cache(self, view, kind):
        self.check()
        proposals = getattr(view, "_proposals", None)
        removed = getattr(view, "_removed_playback_members", None)
        signature = proposals, removed
        previous = self.views.get(view)
        if previous is not None and (previous[0] is not proposals or previous[1] != removed):
            self.caches.pop(view, None)
        self.views[view] = signature
        return self.caches.setdefault(view, {}).setdefault(kind, {})

    def _semantic_key(self, view, ref, context, row, adapter):
        authority = self.authority
        if (not authority.reusable or adapter.validate is not _write_semantics
                or ref.model not in {"ContentItem", "DatasetImport"}
                or not _PhaseGraphFacts._immutable_context(context)
                or row is not self.graph.snapshots.get(ref) or not self.locked(ref)
                or not self.owner_chain_locked(view, ref)
                or not isinstance(self.graph, _WriteGraph)):
            return None
        if self.graph.media_validation_locked and (not authority.paths or authority.media_state is None
                or (authority.coordinator is None and not authority.sqlite_coordinator)
                or not self.graph.media_paths.issubset(authority.paths)):
            return None
        return ref, context, id(row)

    def semantic_reused(self, view, ref, context, row, adapter):
        key = self._semantic_key(view, ref, context, row, adapter)
        return key is not None and key in self.cache(view, "semantic")

    def remember_semantic(self, view, ref, context, row, adapter):
        key = self._semantic_key(view, ref, context, row, adapter)
        if key is not None:
            self.cache(view, "semantic")[key] = True
            self.semantic_obligations[view, key] = view, ref, context, row, adapter

    def revalidate_semantics(self):
        """Fresh full builtin checks at the phase boundary, before any yield.

        No adapter call is reused here. Discovery only collects the union of
        paths from the initial and final fresh checks; it grants no ownership.
        The next fresh confirmation phase holds the complete batch and the
        guard still requires identical paths/plans before any handler yield.
        Proposals, custom validators and other models never qualify.
        """
        for view, ref, context, row, adapter in tuple(self.semantic_obligations.values()):
            self.check()
            if row is not view.snapshot(ref) or not self.locked(ref):
                raise conflict()
            if adapter.validate(view, ref, context, row) is not True:
                raise conflict()
            self.check()
            if (self.graph.media_validation_locked
                    and not self.graph.media_paths.issubset(self.authority.paths)):
                raise conflict()


class _PhaseGraphFacts:
    """Opt-in memoization; direct foundation/read graph callers stay unchanged."""
    def _phase_memo(self):
        base = getattr(self, "_persisted", self)
        memo = getattr(base, "_validation_memo", None)
        if memo is not None:
            memo.check()
            if not memo.authority.reusable:
                return None
        return memo

    @staticmethod
    def _immutable_context(context):
        return context is None or type(context) in {OperationContext, _PlayerWriteScope, _InternalDeleteScope}

    def resolve_owner(self, row, expected_client=None, *, _visited=frozenset()):
        memo = self._phase_memo()
        if memo is None or _visited or not isinstance(row, RowRef):
            return super().resolve_owner(row, expected_client, _visited=_visited)
        key = row, expected_client
        cache = memo.cache(self, "owner")
        if key in cache:
            return cache[key]
        result = super().resolve_owner(row, expected_client, _visited=_visited)
        if memo.owner_chain_locked(self, row):
            cache[key] = result
        return result

    def branch(self, ref, *, _visited=frozenset()):
        memo = self._phase_memo()
        if memo is None or _visited or not isinstance(ref, RowRef):
            return super().branch(ref, _visited=_visited)
        cache = memo.cache(self, "branch")
        if ref in cache and memo.locked(ref):
            return cache[ref]
        result = super().branch(ref, _visited=_visited)
        if memo.locked(ref):
            cache[ref] = result
        return result

    def _relation_local_valid(self, ref, edge, *, snapshot=None, profile="WRITE", context=None):
        memo = self._phase_memo()
        if memo is None or not isinstance(ref, RowRef) or not self._immutable_context(context):
            return super()._relation_local_valid(ref, edge, snapshot=snapshot, profile=profile, context=context)
        from app.services.tenant_relations import relation_spec
        policy = relation_spec(edge.key if hasattr(edge, "key") else edge)
        canonical = self.snapshot(ref)
        if (snapshot is not None and snapshot is not canonical) or not memo.locked(ref):
            return super()._relation_local_valid(ref, edge, snapshot=snapshot, profile=profile, context=context)
        key = ref, policy, profile, context
        cache = memo.cache(self, "relation")
        if key in cache:
            return cache[key]
        result = super()._relation_local_valid(ref, edge, snapshot=snapshot, profile=profile, context=context)
        target = canonical.get(policy.field) if canonical else None
        if result and (target is None or memo.locked(RowRef(policy.target, target))):
            cache[key] = result
        return result

    def collection_children(self, ref, profile):
        memo = self._phase_memo()
        if memo is None or not isinstance(ref, RowRef) or not memo.locked(ref):
            return super().collection_children(ref, profile)
        cache = memo.cache(self, "collection")
        key = ref, profile
        if key not in cache:
            cache[key] = super().collection_children(ref, profile)
        return cache[key]

    def context_integrity(self, ref, context, *, profile="WRITE", snapshot=None):
        memo = self._phase_memo()
        if (memo is None or not isinstance(ref, RowRef)
                or not self._immutable_context(context) or not memo.locked(ref)
                or (snapshot is not None and snapshot is not self.snapshot(ref))):
            return super().context_integrity(ref, context, profile=profile, snapshot=snapshot)
        cache = memo.cache(self, "context")
        key = ref, profile, context
        if key in cache:
            return cache[key]
        result = super().context_integrity(ref, context, profile=profile, snapshot=snapshot)
        if result.state == ContextIntegrityState.CONSISTENT and all(memo.locked(child) for child in result.checked):
            cache[key] = result
        return result


class _ProposedGraph(_PhaseGraphFacts, TenantGraph):
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
    if ref in getattr(graph, "_removed_playback_members", ()):
        return  # explicit membership DELETE; persisted old state is checked separately
    if type(adapters) is not tuple or len({a.model for a in adapters}) != len(adapters):
        raise conflict()
    row = graph.snapshot(ref)
    # Scalar credentials/labels are not ownership-bearing media references.
    exempt = {"password_hash", "player_token_hash", "source_filename", "sample_value", "fallback_value"}
    present = (ref.model in {"VideowallPublication", "VideowallPublicationAsset", "VideowallPlaybackGroup", "VideowallPlaybackGroupMember"}
               or any(row.get(k) for k in model_spec(ref.model).opaque_fields if k not in exempt))
    if present:
        adapter = next((a for a in adapters if a.model == ref.model), None)
        memo = graph._phase_memo() if isinstance(graph, _PhaseGraphFacts) else None
        if adapter is not None and memo is not None and memo.semantic_reused(graph, ref, context, row, adapter):
            return
        if adapter is None or adapter.validate(graph, ref, context, row) is not True:
            raise conflict()
        if memo is not None:
            memo.remember_semantic(graph, ref, context, row, adapter)


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
    telemetry: tuple = ()

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


class _PlayerTelemetryGate(MutationGate):
    """Compatible readers of the structural tenant barrier, never a writer bypass."""
    def __enter__(self):
        from sqlalchemy import text
        if self.graph.session.get_bind().dialect.name != "postgresql":
            return super().__enter__()
        _clean(self.graph.session)
        if self.active or self.context.client_id is None:
            raise conflict()
        self.graph.authorize_row(RowRef("Client", self.context.client_id), self.context)
        connection = self.graph.session.connection()
        digest = sha256(("tecno.tenant.write.v1:" + self.context.client_id).encode()).digest()[:8]
        key = int.from_bytes(digest, "big", signed=True)
        connection.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key})
        self.transaction = self.graph.session.get_transaction()
        self.token, self.active = object(), True
        return self


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
    if operation.function == "update_playback_groups":
        view._removed_playback_members = frozenset(i.reference for i in intents
            if i.action == "DELETE" and i.reference.model == "VideowallPlaybackGroupMember")
    for proposal in proposed:
        view.assert_integrity(proposal.reference, context)
        _semantics(view, proposal.reference, context, semantic_adapters)
    # Incoming discovery deliberately has NO tenant predicate: filtering by owner
    # here would hide exactly the foreign dependencies this guard must catch.
    queue = list(sorted(roots, key=lambda r: (r.model, r.identity)))
    checked, context_checked, dependencies, telemetry = set(), set(), [], []
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
                if (isinstance(graph, _WriteGraph) and edge.key in {
                        "LayoutRevision.created_by_user_id", "VideowallPublication.requested_by_user_id"}
                        and ref.model == "User"
                        and graph.resolve_owner(ref).state.value == "GLOBAL_PRINCIPAL"
                        and view.resolve_owner(ref).state.value == "GLOBAL_PRINCIPAL"):
                    # Attribution to a global principal remains valid after a
                    # name/status/role change within global roles. These revisions
                    # belong to their layouts, not to the User being updated.
                    continue
                skip_history = (edge.model in {"PlayerHeartbeat", "PlayerPlaybackEvent"}
                                and type(graph) is _PlayerTelemetryGraph
                                and graph._skip_prior_telemetry(gate, context, operation, intents))
                if skip_history:
                    # Existing report rows are not authority for an activity-only
                    # operation. Its approved patches cannot change their FKs or
                    # parent ownership/context. New CREATE roots still receive
                    # every normal scope, closure, opaque and two-pass check.
                    ids = set()
                else:
                    from app.services.tenant_telemetry import validate_telemetry_incoming
                    builtin = (_TELEMETRY_WRITE_SEMANTICS if isinstance(graph, _WriteGraph)
                               else _TELEMETRY_DELETE_SEMANTICS)
                    adapter = next((a for a in semantic_adapters if a.model == edge.model), None)
                    certificate = (validate_telemetry_incoming(graph, view, ref, edge, proposed, context)
                                   if adapter is not None and adapter.validate is builtin else None)
                    if certificate is not None:
                        telemetry.append(certificate)
                        # History models are reviewed leaves. Their outgoing parent
                        # closure is checked with the original graph and adapters;
                        # these parents must not become new incoming-discovery roots.
                        for parent_ref in certificate.references:
                            for parent_graph in (graph, view):
                                parent_proof = parent_graph.context_integrity(parent_ref, context, profile="DELETE")
                                if parent_proof.state != ContextIntegrityState.CONSISTENT:
                                    raise conflict()
                            context_checked.update(parent_proof.checked)
                            for related in parent_proof.checked:
                                if graph.snapshot(related) is not None:
                                    _semantics(graph, related, context, semantic_adapters)
                                _semantics(view, related, context, semantic_adapters)
                        ids = set()
                    else:
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
        tuple(telemetry),
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


class _DeleteGraph(_PhaseGraphFacts, TenantGraph):
    def __init__(self, session):
        super().__init__(session)
        self.snapshots = _SnapshotStore()
        self._snapshot_lock_proofs = {}
        self.locking = False
        self._validation_memo = None

    def _row_lock_mode(self, ref):
        return "update"

    def snapshot(self, row):
        from sqlalchemy import select
        ref = self.reference(row)
        if ref not in self.snapshots:
            cls = model_class(ref.model)
            statement = select(*(getattr(cls, k) for k in model_spec(ref.model).columns)).where(cls.id == ref.identity)
            dialect = self.session.get_bind().dialect.name
            locked = self.locking and dialect == "postgresql"
            if locked:
                if self._row_lock_mode(ref) == "share":
                    statement = statement.with_for_update(read=True)
                else:
                    statement = statement.with_for_update()
            with self.session.no_autoflush:
                value = self.session.execute(statement).mappings().one_or_none()
            self.snapshots[ref] = freeze(dict(value)) if value is not None else None
            # Eligibility records the actual locked load, not merely a mutable
            # ``locking`` flag or a caller-prepopulated immutable dictionary.
            # SQLite's active gate owns BEGIN IMMEDIATE in this transaction;
            # the phase also binds that exact gate before reusing any fact.
            if self.locking and dialect == "sqlite":
                locked = self.session.connection().connection.driver_connection.in_transaction
            if locked and value is not None:
                self._snapshot_lock_proofs[ref] = (self.snapshots[ref], self.session.get_transaction())
        result = self.snapshots[ref]
        memo = self._validation_memo
        if memo is not None and result is not None and not memo.locked(ref):
            memo.authority.decline()
        return result

    def authorize_row(self, ref, context):
        if type(context) is _InternalDeleteScope:
            from app.services.tenant_ownership import OwnerState
            owner = self.resolve_owner(ref, context.client_id)
            if owner.state != OwnerState.OWNED:
                raise conflict()
            return owner
        return super().authorize_row(ref, context)


@contextmanager
def _graph_validation_phase(graph, gate=None):
    """Caches stop before the next fresh pass or any handler-side mutation."""
    from app.services.media_references import media_ownership_validation_phase
    if getattr(graph, "_validation_memo", None) is not None:
        raise conflict()
    with media_ownership_validation_phase(graph.session) as authority:
        memo = _GraphValidationMemo(graph, authority, gate)
        graph._validation_memo = memo
        authority.invalidate_callbacks.append(memo.clear)
        failed = False
        try:
            try:
                yield
            except BaseException:
                failed = True
                raise
            finally:
                if not failed:
                    memo.revalidate_semantics()
                    memo.check()
        finally:
            graph._validation_memo = None
            memo.clear()
            memo.semantic_obligations.clear()


@contextmanager
def write_guard(db, *, actor, client_id, operation, intents=None, semantic_adapters=(), intent_factory=None):
    """Approve a complete write batch before the handler's first ORM assignment.

    Uses the A6 tenant gate and A7 fresh, locking snapshots. Locks live until
    transaction end, including after this Python scope exits. This function
    does not execute the plan or repair historical relationships.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.services.tenant_ownership import context_for_actor, enum_value

    _clean(db)
    if ((intents is None) == (intent_factory is None)
            or (intent_factory is not None and (not callable(intent_factory)
                or operation != "activate_videowall_publication"))):
        raise conflict()
    spec = next((item for item in OPERATIONS if item.function == operation), None)
    if spec is None or spec.method not in {"POST", "PUT", "PATCH"}:
        raise conflict()
    actor_before = (enum_value(actor.role), actor.client_id, actor.branch_id)
    context = context_for_actor(db, actor, client_id=client_id)
    if actor_before != (context.actor.role, context.actor.client_id, context.actor.branch_id):
        raise conflict()
    graph = _WriteGraph(db)
    graph.new_content_refs = frozenset(intent.reference for intent in (intents or ())
                                      if intent.action == "CREATE" and intent.reference.model == "ContentItem")
    prepared_versions = _prepared_row_versions(db, intents or ())
    if operation == "create_client" and intents is not None and len(intents) == 1:
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
        if any(intent.reference.model == "User" for intent in (intents or ())):
            from app.services.user_admin import lock_super_admin_delete_set
            lock_super_admin_delete_set(db)
        gate_type = _GlobalWriteGate if context.client_id is None else MutationGate
        with gate_type(graph, context) as gate:
            graph.snapshots.clear()
            graph.locking = True
            graph.snapshot(actor_ref)
            if context_for_actor(db, actor, client_id=client_id) != context:
                raise conflict()
            if intent_factory is not None:
                # Backend-only pure preparation after the tenant gate. In VW1
                # this fixes activation's persisted clock after lock waits.
                # Request data never supplies a callable or a mutation plan.
                intents = intent_factory()
                _clean(db)
                if type(intents) is not tuple or not intents:
                    raise conflict()
                graph.new_content_refs = frozenset(i.reference for i in intents
                    if i.action == "CREATE" and i.reference.model == "ContentItem")
                prepared_versions = _prepared_row_versions(db, intents)
            for ref, previous in prepared_versions.items():
                current = graph.snapshot(ref)
                if current is None or any(not _same_prepared_value(value, current.get(key))
                                          for key, value in previous.items()):
                    raise conflict()
            # Each snapshot is SELECT ... FOR UPDATE on PostgreSQL. Incoming
            # references committed before their anchor lock enter discovery;
            # subsequent FK writers wait until this transaction ends.
            with _graph_validation_phase(graph, gate):
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
                with _graph_validation_phase(graph, gate):
                    confirmed = preflight_relationship_graph(gate, intents, spec,
                                                             semantic_adapters=semantic_adapters)
                if (plan.proposed != confirmed.proposed or plan.checked != confirmed.checked
                        or plan.telemetry != confirmed.telemetry
                        or plan.dependencies != confirmed.dependencies or paths != frozenset(graph.media_paths)):
                    raise conflict()
                confirmed.assert_usable(gate, context)
                db.expire_all()
                from app.services.media_references import generated_reference_guard
                with generated_reference_guard(db, confirmed):
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


def _player_telemetry_shape(channel, device, operation, intents):
    """Only backend-authored activity patches may use the shared tenant barrier."""
    from datetime import datetime
    if device is None or type(intents) is not tuple:
        return False
    expected = {
        "create_player_heartbeat": ("PlayerHeartbeat", {"status", "last_ping_at", "current_playback"},
                                    {"last_seen_at", "app_version"}),
        "create_player_playback_event": ("PlayerPlaybackEvent", None, set()),
        "get_runtime_config": (None, None, {"last_seen_at"}),
    }.get(operation)
    if expected is None or any(not isinstance(i, MutationIntent) for i in intents):
        return False
    history, channel_fields, device_fields = expected
    wanted = {RowRef("PlayerDevice", device.id)}
    if channel_fields is not None:
        wanted.add(RowRef("Channel", channel.id))
    updates, creates = set(), []
    for intent in intents:
        values = dict(intent.patch)
        if intent.action == "UPDATE" and intent.reference in wanted:
            if intent.reference in updates:
                return False
            fields = device_fields if intent.reference.model == "PlayerDevice" else channel_fields
            if not set(values) <= fields:
                return False
            if any(key in values and not isinstance(values[key], datetime)
                   for key in ("last_seen_at", "last_ping_at")):
                return False
            updates.add(intent.reference)
        elif intent.action == "CREATE" and intent.reference.model == history:
            if not set(values) <= (set(model_spec(history).columns) - {"id", "created_at", "updated_at"}):
                return False
            if any(values.get(key) != value for key, value in (
                    ("player_device_id", device.id), ("channel_id", channel.id),
                    ("client_id", channel.client_id), ("branch_id", channel.branch_id))):
                return False
            creates.append(intent.reference)
        else:
            return False
    return updates == wanted and len(creates) == (1 if history else 0)


def _monotonic_player_activity(graph, intents):
    """Keep receipt counters monotonic after waiting for the authoritative roots."""
    from datetime import datetime, UTC
    normalized = []
    for intent in intents:
        patch = dict(intent.patch)
        current = graph.snapshot(intent.reference) if intent.action == "UPDATE" else None
        for field in ("last_seen_at", "last_ping_at"):
            if field not in patch or current is None:
                continue
            requested, previous = patch[field], current.get(field)
            if not isinstance(requested, datetime):
                raise conflict()
            requested = requested.replace(tzinfo=UTC) if requested.tzinfo is None else requested.astimezone(UTC)
            if isinstance(previous, datetime):
                previous = previous.replace(tzinfo=UTC) if previous.tzinfo is None else previous.astimezone(UTC)
                requested = max(requested, previous)
            patch[field] = requested
        normalized.append(MutationIntent(intent.action, intent.reference, tuple(patch.items())))
    return tuple(normalized)


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
    telemetry = _player_telemetry_shape(channel, player_device, operation, intents)
    exclusive = {RowRef("PlayerDevice", player_device.id)} if telemetry else set()
    if telemetry and operation == "create_player_heartbeat":
        exclusive.add(RowRef("Channel", channel.id))
    graph = _PlayerTelemetryGraph(db, exclusive) if telemetry else _WriteGraph(db)
    context = _PlayerWriteScope(channel.client_id, channel.branch_id)
    graph.player_scope = context
    spec = next(o for o in OPERATIONS if o.function == operation)
    prepared = _prepared_row_versions(db, intents)
    if telemetry:
        identity_fields = {
            "Channel": {"id", "client_id", "branch_id", "channel_code"},
            "PlayerDevice": {"id", "client_id", "branch_id", "channel_id", "player_token_hash", "is_active"},
        }
        prepared = {ref: {key: value for key, value in row.items() if key in identity_fields[ref.model]}
                    for ref, row in prepared.items()}
    channel_values = (channel.client_id, channel.branch_id, channel.channel_code)
    device_hash = player_device.player_token_hash if player_device else None
    if db.get_bind().dialect.name == "postgresql":
        conn = db.connection()
        if conn.get_isolation_level() != "READ COMMITTED":
            raise conflict()
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    try:
        gate_type = _PlayerTelemetryGate if telemetry else MutationGate
        with gate_type(graph, context) as gate:
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
            if telemetry:
                intents = _monotonic_player_activity(graph, intents)
                graph._bind_activity_history(gate, context, spec, intents)
            with _graph_validation_phase(graph, gate):
                plan = preflight_relationship_graph(gate, intents, spec, semantic_adapters=_write_adapters())
            with ExitStack() as media_locks:
                paths = frozenset(graph.media_paths)
                if paths:
                    from app.services.media_references import media_reference_operation, lock_media_paths
                    media_locks.enter_context(media_reference_operation(db))
                    if telemetry:
                        lock_media_paths(db, list(paths), shared=True)
                    else:
                        lock_media_paths(db, list(paths))
                graph.snapshots.clear()
                graph.media_paths.clear()
                graph.media_validation_locked = True
                with _graph_validation_phase(graph, gate):
                    confirmed = preflight_relationship_graph(gate, intents, spec, semantic_adapters=_write_adapters())
                if (plan.proposed != confirmed.proposed or plan.checked != confirmed.checked
                        or plan.telemetry != confirmed.telemetry
                        or plan.dependencies != confirmed.dependencies or paths != frozenset(graph.media_paths)):
                    raise conflict()
                confirmed.assert_usable(gate, context)
                db.expire_all()
                yield confirmed
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01", "55P03"}:
            raise
        db.rollback()
        if telemetry and getattr(exc.orig, "sqlstate", None) == "55P03":
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="Player operation temporarily unavailable") from exc
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


class _PlayerTelemetryGraph(_WriteGraph):
    """Shared immutable authority; exclusive append anchors/activity write roots.

    FOR SHARE conflicts with every parent UPDATE/DELETE, including non-key
    ownership/status changes. Each incoming discovery anchor stays FOR UPDATE,
    so FK KEY SHARE cannot insert new history between the two R5 passes.
    """
    def __init__(self, session, exclusive):
        super().__init__(session)
        self._exclusive = frozenset(exclusive)
        self._activity_history_binding = None

    def _row_lock_mode(self, ref):
        return "update" if ref in self._exclusive or ref.model == "PlayerDevice" else "share"

    def _bind_activity_history(self, gate, context, operation, intents):
        # Called only after the exact activity whitelist and fresh locked scope
        # checks, with the immutable normalized intents that both passes use.
        self._activity_history_binding = (gate, gate.token, gate.transaction, context, operation, intents)

    def _skip_prior_telemetry(self, gate, context, operation, intents):
        binding = self._activity_history_binding
        return (type(self) is _PlayerTelemetryGraph and type(binding) is tuple and len(binding) == 6
                and binding[0] is gate and binding[1] is gate.token
                and binding[2] is self.session.get_transaction()
                and binding[2] is gate.transaction
                and gate.active and gate.graph is self
                and binding[3] is context and binding[4] is operation
                and binding[5] is intents and operation.function in {
                    "create_player_heartbeat", "create_player_playback_event", "get_runtime_config"})


def _write_semantics(graph, ref, context, row):
    """Explicit opaque-reference consumers, independent of actor privileges.

    Labels, telemetry and tabular data never establish ownership. Only typed
    persisted media references can prove local media ownership. No HTTP fetch.
    """
    # These models' scalar/telemetry values never carry media ownership. Their
    # owner/context checks remain in the complete preflight outside this helper;
    # resolving an owner here used to compute an unused result before True.
    if ref.model in {"Channel", "DataSource", "DatasetColumn", "DatasetRow",
                     "PlayerDevice", "PlayerHeartbeat", "PlayerPlaybackEvent", "User"}:
        return True
    from collections.abc import Mapping
    from sqlalchemy import select
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

    if ref.model == "VideowallPublication":
        from app.services.videowall_integrity import publication_semantics
        return publication_semantics(graph, ref, context, row)
    if ref.model == "VideowallPublicationAsset":
        from app.services.videowall_integrity import asset_semantics
        return asset_semantics(graph, ref, context, row)
    if ref.model in {"VideowallPlaybackGroup", "VideowallPlaybackGroupMember"}:
        from app.services.videowall_playback_groups import playback_group_semantics
        return playback_group_semantics(graph, ref, context, row)
    if ref.model == "ContentItem":
        if isinstance(graph, _ProposedGraph):
            previous = graph._persisted.snapshot(ref)
            if previous is not None and any(previous.get(k) != row.get(k) for k in ("file_path", "type")):
                from app.models.entities import VideowallPublicationAsset
                referenced = graph.session.scalar(select(VideowallPublicationAsset.id).where(
                    (VideowallPublicationAsset.master_content_id == ref.identity)
                    | (VideowallPublicationAsset.derived_content_id == ref.identity)).limit(1))
                if referenced:
                    return False
        if ref in base.new_content_refs:
            from app.services.media_references import generated_media_reference_authorized
            try:
                generated_path = resolve_stored_media_path(root, row.get("file_path"), must_exist=False)
                if (generated_path is not None and generated_media_reference_authorized(
                        graph.session, ref.identity, owner.client_id, generated_path)):
                    base.media_paths.add(generated_path)
                    return True
            except MediaPathError:
                return False
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


_TELEMETRY_WRITE_SEMANTICS = _write_semantics


def _write_adapters():
    from app.services.tenant_relations import MODEL_SPECS
    return tuple(SemanticAdapter(spec.name, _write_semantics) for spec in MODEL_SPECS
                 if spec.opaque_fields or spec.name in {"VideowallPlaybackGroup", "VideowallPlaybackGroupMember"})


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


_TELEMETRY_DELETE_SEMANTICS = _delete_semantics


def _delete_adapters():
    from app.services.tenant_relations import MODEL_SPECS
    return tuple(SemanticAdapter(s.name, _delete_semantics) for s in MODEL_SPECS
                 if s.opaque_fields or s.name in {"VideowallPlaybackGroup", "VideowallPlaybackGroupMember"})


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
        with _graph_validation_phase(graph, gate):
            plan = preflight_relationship_graph(gate, intents, discovery_spec, semantic_adapters=_delete_adapters())
        graph.snapshots.clear()
        rediscovered = _effect_roots(graph, initial, operation)
        if rediscovered != expanded:
            continue
        with _graph_validation_phase(graph, gate):
            confirmed = preflight_relationship_graph(gate, tuple(MutationIntent("DELETE", ref) for ref in rediscovered),
                                                      discovery_spec, semantic_adapters=_delete_adapters())
        if (plan.checked != confirmed.checked or plan.dependencies != confirmed.dependencies
                or plan.telemetry != confirmed.telemetry
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

    @property
    def telemetry(self):
        return tuple(dependency for plan in self.plans for dependency in plan.telemetry)


def _approved_telemetry_predicate(approval, model):
    """Membership in locked, universally validated historical leaf scopes.

    These certificates are backend-generated by both fresh preflight passes.
    FK anchors prevent new members; every historical member is row-locked until
    this transaction ends. They never approve a different model or parent.
    """
    from app.services.tenant_telemetry import telemetry_scope_predicate
    for plan in approval.plans:
        gate = plan._gate
        if (not gate.active or gate.transaction is not approval.transaction
                or gate.token is not plan._gate_token):
            raise conflict()
    return telemetry_scope_predicate(model, approval.telemetry)


def require_delete_coverage(db, rows):
    """Nested explicit cleanup may consume, never expand, the approved batch."""
    approval = db.info.get(_DELETE_STATE)
    graph = TenantGraph(db)
    if (not isinstance(approval, DeleteApproval)
            or approval.transaction is not db.get_transaction()):
        raise conflict()
    from sqlalchemy import select
    for row in rows:
        ref = graph.reference(row)
        if ref in approval.covered:
            continue
        if ref.model not in {"PlayerHeartbeat", "PlayerPlaybackEvent"}:
            raise conflict()
        cls = model_class(ref.model)
        membership = _approved_telemetry_predicate(approval, ref.model)
        with db.no_autoflush:
            present = db.scalar(select(select(cls.id).where(
                cls.id == ref.identity, membership).exists()))
        if not present:
            raise conflict()
    return approval


@contextmanager
def _approved_delete_execution(db, approval):
    """Explicit handlers execute; this boundary cannot expand their target set.

    Bulk DELETE/UPDATE is checked and constrained to immutable approved IDs or
    locked, exhaustively checked telemetry scopes. No history IDs are returned
    to Python for scoped bulk statements. ORM cascades are checked before flush. FK anchors remain locked through
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
        permitted = cls.id.in_(allowed)
        if name in {"PlayerHeartbeat", "PlayerPlaybackEvent"} and approval.telemetry:
            from sqlalchemy import or_
            permitted = or_(permitted, _approved_telemetry_predicate(approval, name))
            # Check the original complete statement, before adding its approval
            # boundary. Silently narrowing an unauthorized statement is unsafe.
            outside = select(cls.id).where(*statement._where_criteria, permitted.is_not(True)).exists()
            with db.no_autoflush:
                if db.scalar(select(outside), state.parameters or {}):
                    raise conflict()
        else:
            query = select(cls.id).where(*statement._where_criteria)
            with db.no_autoflush:
                actual = set(db.scalars(query, state.parameters or {}))
            if not actual.issubset(allowed):
                raise conflict()
        state.statement = statement.where(permitted)

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
            # V1 has no publication release/GC endpoint. Keep its masters,
            # slices and structural parents protected until an explicit release
            # policy exists, rather than relying on a late RESTRICT failure or
            # deleting media still needed by a prepared/offline wall.
            if any(ref.model in {"VideowallPublication", "VideowallPublicationAsset"}
                   for ref in covered):
                raise conflict()
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
