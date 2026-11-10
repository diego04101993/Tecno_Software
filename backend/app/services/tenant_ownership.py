"""Authoritative persisted ownership, independent of actor permission and FK integrity."""
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal

from app.services.tenant_relations import (
    BRANCH_ROLES, GLOBAL_ROLES, ROLES, MODELS, conflict, model_class, model_spec,
    relation_spec, relations_for, validate_registry, COLLECTION_DEPENDENCIES,
)


class OwnerState(str, Enum):
    OWNED = "OWNED"
    FOREIGN = "FOREIGN"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"
    INCONSISTENT = "INCONSISTENT"
    GLOBAL_PRINCIPAL = "GLOBAL_PRINCIPAL"


class ContextIntegrityState(str, Enum):
    CONSISTENT = "CONSISTENT"
    INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True)
class ContextIntegrity:
    """Backend proof only; HTTP conflicts never serialize these row identities."""
    state: ContextIntegrityState
    checked: tuple


@dataclass(frozen=True)
class RowRef:
    model: str
    identity: str

    def __post_init__(self):
        model_spec(self.model)
        if type(self.identity) is not str or not self.identity:
            raise conflict()


@dataclass(frozen=True)
class OwnerCandidates:
    """Backend query results only. Request IDs are not ownership evidence."""
    references: tuple[RowRef, ...]


@dataclass(frozen=True)
class OwnerResolution:
    state: OwnerState
    client_id: str | None = None


@dataclass(frozen=True)
class ActorAuthorization:
    """Created from authenticated backend state after the route's role permission."""
    actor: RowRef
    role: str
    client_id: str | None
    branch_id: str | None

    def __post_init__(self):
        if self.role not in ROLES or self.actor.model != "User":
            raise conflict()
        if self.role in GLOBAL_ROLES:
            if self.client_id is not None or self.branch_id is not None:
                raise conflict()
        elif not self.client_id or (self.role in BRANCH_ROLES and not self.branch_id):
            raise conflict()


@dataclass(frozen=True)
class OperationContext:
    actor: ActorAuthorization
    client_id: str | None
    branch_id: str | None = None
    parent: RowRef | None = None
    parent_relation: str | None = None

    def __post_init__(self):
        if not isinstance(self.actor, ActorAuthorization):
            raise conflict()
        if any(value is not None and (type(value) is not str or not value)
               for value in (self.client_id, self.branch_id)):
            raise conflict()
        if self.parent is not None and not isinstance(self.parent, RowRef):
            raise conflict()
        if self.parent_relation is not None:
            edge = relation_spec(self.parent_relation)
            if self.parent is None or edge.target != self.parent.model:
                raise conflict()
        if self.parent is not None and self.parent.model != "Client" and self.parent_relation is None:
            raise conflict()
        if self.actor.role not in GLOBAL_ROLES and self.client_id != self.actor.client_id:
            raise conflict()
        if self.actor.role in BRANCH_ROLES and self.branch_id != self.actor.branch_id:
            raise conflict()
        if self.parent is not None and self.client_id is None:
            raise conflict()


def freeze(value):
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise conflict()
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(v) for v in value)
    if value is None or isinstance(value, (str, int, float, bool, bytes, date, datetime, time, Decimal, Enum)):
        return value
    raise conflict()


def enum_value(value):
    return value.value if isinstance(value, Enum) else value


class TenantGraph:
    """Read-only snapshots. Never lazy-load ORM attributes or trigger autoflush."""

    def __init__(self, session, *, snapshot_transaction=None):
        validate_registry()
        self.session = session
        self._snapshot_rows = None
        self._snapshot_scope = None
        if snapshot_transaction is not None:
            from sqlalchemy import Connection, event
            bind = session.get_bind()
            transaction = session.get_transaction()
            # The projection caller supplies the transaction verified by
            # readonly_snapshot_session. Standalone and mutation graphs never
            # cache row facts. A nested or restarted transaction cannot reuse them.
            if (isinstance(bind, Connection) and bind.dialect.name == "postgresql"
                    and bind.get_transaction() is snapshot_transaction
                    and transaction is not None and not session.in_nested_transaction()
                    and not bind.in_nested_transaction()):
                self._snapshot_rows = {}
                self._snapshot_scope = (bind, snapshot_transaction, transaction)
                event.listen(session, "after_transaction_end", self._end_snapshot_rows, once=True)

    def _end_snapshot_rows(self, _session, _transaction):
        self.discard_snapshot_rows()

    def discard_snapshot_rows(self):
        if self._snapshot_rows is not None:
            self._snapshot_rows.clear()
            self._snapshot_rows = None
            self._snapshot_scope = None

    def reference(self, row):
        from sqlalchemy import inspect
        if isinstance(row, RowRef):
            return row
        state = inspect(row, raiseerr=False)
        if (state is None or not state.persistent or state.session is not self.session
                or not state.identity or len(state.identity) != 1):
            raise conflict()
        name = state.mapper.class_.__name__
        if type(row) is not model_class(name):
            raise conflict()
        return RowRef(name, state.identity[0])

    def snapshot(self, row):
        from sqlalchemy import select
        ref = self.reference(row)
        rows = self._snapshot_rows
        if rows is not None:
            bind, connection_transaction, session_transaction = self._snapshot_scope
            if (self.session.get_bind() is not bind or bind.closed or bind.invalidated
                    or bind.get_transaction() is not connection_transaction
                    or self.session.get_transaction() is not session_transaction
                    or not connection_transaction.is_active or not session_transaction.is_active
                    or bind.in_nested_transaction() or self.session.in_nested_transaction()):
                self.discard_snapshot_rows()
                rows = None
            elif ref in rows:
                return rows[ref]
        cls = model_class(ref.model)
        spec = model_spec(ref.model)
        with self.session.no_autoflush:
            result = self.session.execute(
                select(*(getattr(cls, key) for key in spec.columns))
                .where(cls.id == ref.identity)
            ).mappings().one_or_none()
        snapshot = freeze(dict(result)) if result is not None else None
        if rows is not None:
            rows[ref] = snapshot
        return snapshot

    def resolve_owner(self, row, expected_client=None, *, _visited=frozenset()):
        if isinstance(row, OwnerCandidates):
            if type(row.references) is not tuple or not row.references:
                return OwnerResolution(OwnerState.UNKNOWN)
            results = tuple(self.resolve_owner(r, _visited=_visited) for r in row.references)
            if any(r.state not in {OwnerState.OWNED, OwnerState.GLOBAL_PRINCIPAL} for r in results):
                return OwnerResolution(OwnerState.UNKNOWN)
            if len({(r.state, r.client_id) for r in results}) != 1:
                return OwnerResolution(OwnerState.AMBIGUOUS)
            result = results[0]
        else:
            try:
                ref = self.reference(row)
            except Exception as exc:
                from fastapi import HTTPException
                if not isinstance(exc, HTTPException):
                    raise
                return OwnerResolution(OwnerState.UNKNOWN)
            if ref in _visited:
                return OwnerResolution(OwnerState.INCONSISTENT)
            record = self.snapshot(ref)
            if record is None:
                return OwnerResolution(OwnerState.UNKNOWN)
            spec = model_spec(ref.model)
            if ref.model == "User" and enum_value(record.get("role")) not in ROLES:
                return OwnerResolution(OwnerState.INCONSISTENT)
            if ref.model == "User" and enum_value(record["role"]) in GLOBAL_ROLES:
                if record["client_id"] is not None or record["branch_id"] is not None:
                    return OwnerResolution(OwnerState.INCONSISTENT)
                result = OwnerResolution(OwnerState.GLOBAL_PRINCIPAL)
            elif spec.owner_kind == "root":
                result = OwnerResolution(OwnerState.OWNED, ref.identity)
            else:
                parent_id = record.get(spec.owner_field)
                if type(parent_id) is not str or not parent_id:
                    return OwnerResolution(OwnerState.UNKNOWN)
                result = self.resolve_owner(
                    RowRef(spec.owner_target, parent_id), _visited=_visited | {ref}
                )
        if result.state == OwnerState.OWNED and expected_client is not None and result.client_id != expected_client:
            return replace(result, state=OwnerState.FOREIGN)
        return result

    def branch(self, ref, *, _visited=frozenset()):
        ref = self.reference(ref)
        if ref in _visited:
            raise conflict()
        row = self.snapshot(ref)
        if row is None:
            raise conflict()
        policy = model_spec(ref.model).branch_policy
        if policy == "SELF":
            return ref.identity
        if policy == "FIELD":
            value = row.get("branch_id")
            if value is None and row.get("channel_id"):
                return self.branch(RowRef("Channel", row["channel_id"]), _visited=_visited | {ref})
            return value
        if policy == "OWNER":
            spec = model_spec(ref.model)
            value = row[spec.owner_field]
            if not value:
                raise conflict()
            return self.branch(RowRef(spec.owner_target, value), _visited=_visited | {ref})
        if policy == "SHARED":
            return None
        raise conflict()

    def authorize_row(self, ref, context):
        if not isinstance(context, OperationContext):
            raise conflict()
        owner = self.resolve_owner(ref, context.client_id)
        if owner.state != OwnerState.OWNED:
            raise conflict()
        if context.parent is not None:
            if self.resolve_owner(context.parent).client_id != context.client_id:
                raise conflict()
            if not self.in_parent_scope(ref, context):
                raise conflict()
        if context.branch_id is not None:
            branch = self.branch(ref)
            if branch is not None and branch != context.branch_id:
                raise conflict()
        return owner

    def in_parent_scope(self, ref, context, *, _visited=frozenset()):
        ref = self.reference(ref)
        parent = context.parent
        if parent is None or parent.model == "Client" or ref.model == "Client" or ref == parent:
            return True
        if ref in _visited or context.parent_relation is None:
            return False
        edge = relation_spec(context.parent_relation)
        row = self.snapshot(ref)
        if row is None:
            return False
        if ref.model == edge.model:
            return row.get(edge.field) == parent.identity
        spec = model_spec(ref.model)
        if spec.owner_kind != "structural-owner" or not row.get(spec.owner_field):
            return False
        return self.in_parent_scope(RowRef(spec.owner_target, row[spec.owner_field]),
                                    context, _visited=_visited | {ref})

    def _relation_local_valid(self, ref, edge, *, snapshot=None, profile="WRITE", context=None):
        edge = relation_spec(edge.key if hasattr(edge, "key") else edge)
        ref = self.reference(ref)
        if edge.model != ref.model or profile not in edge.operations:
            raise conflict()
        row = self.snapshot(ref) if snapshot is None else snapshot
        if row is None:
            return False
        value = row.get(edge.field)
        if value is None:
            return edge.nullable
        if type(value) is not str or not value:
            return False
        target = RowRef(edge.target, value)
        target_row = self.snapshot(target)
        if target_row is None:
            return False
        source_owner = self.resolve_owner(ref)
        target_owner = self.resolve_owner(target)
        if (edge.key in {"LayoutRevision.created_by_user_id", "VideowallPublication.requested_by_user_id"}
                and target_owner.state == OwnerState.GLOBAL_PRINCIPAL):
            return True
        if (source_owner.state != OwnerState.OWNED or target_owner.state != OwnerState.OWNED
                or source_owner.client_id != target_owner.client_id):
            return False
        if context is not None and context.branch_id is not None:
            branch = self.branch(target)
            if branch is not None and branch != context.branch_id:
                return False
        if edge.key == "Videowall.active_publication_id":
            if target_row["wall_id"] != ref.identity:
                return False
            if target_row.get("playback_group_id") is not None:
                return False
            if row["media_delivery"] == "node_slice" and (
                    target_row["status"] != "ACTIVE" or target_row["loop_start_epoch_ms"] is None):
                return False
        if edge.key == "VideowallPlaybackGroup.active_publication_id":
            if (target_row.get("playback_group_id") != ref.identity or target_row["wall_id"] != row["wall_id"]
                    or target_row["campaign_id"] != row["campaign_id"] or row["mode"] != "SYNCHRONIZED"
                    or target_row["status"] != "ACTIVE" or target_row["loop_start_epoch_ms"] is None):
                return False
        if edge.key == "VideowallPublication.playback_group_id":
            if target_row["wall_id"] != row["wall_id"] or target_row["branch_id"] != row["branch_id"]:
                return False
        if edge.key == "VideowallPlaybackGroupMember.node_id":
            group = self.snapshot(RowRef("VideowallPlaybackGroup", row["group_id"]))
            channel = self.snapshot(RowRef("Channel", target_row["channel_id"]))
            if (group is None or channel is None or target_row["videowall_id"] != group["wall_id"]
                    or not target_row["layout_enabled"] or channel["branch_id"] != group["branch_id"]):
                return False
        if edge.model == "VideowallPublicationAsset":
            publication = self.snapshot(RowRef("VideowallPublication", row["publication_id"]))
            if publication is None:
                return False
            if edge.field == "node_id":
                channel = self.snapshot(RowRef("Channel", target_row["channel_id"]))
                if (target_row["videowall_id"] != publication["wall_id"] or channel is None
                        or channel["branch_id"] != publication["branch_id"]):
                    return False
            if edge.field == "master_content_id":
                program = publication.get("program_snapshot_json")
                if not isinstance(program, Mapping) or not isinstance(program.get("items"), (list, tuple)):
                    return False
                item = next((i for i in program["items"] if isinstance(i, Mapping)
                             and i.get("id") == row["logical_item_id"]), None)
                if item is None or item.get("master_content_id") != value:
                    return False
            if edge.field == "derived_content_id" and value == row["master_content_id"]:
                return False
        if edge.key in {"DatasetColumn.import_id", "DatasetRow.import_id", "Dataset.current_import_id"}:
            dataset_id = ref.identity if ref.model == "Dataset" else row["dataset_id"]
            if target_row["dataset_id"] != dataset_id:
                return False
        if edge.key in {"TouchExperience.home_screen_id", "TouchExperience.attract_screen_id"}:
            if target_row["experience_id"] != ref.identity:
                return False
        if edge.key == "KioskButton.target_screen_id":
            screen = self.snapshot(RowRef("KioskScreen", row["screen_id"]))
            if screen is None or screen["experience_id"] != target_row["experience_id"]:
                return False
        if edge.target == "Channel" and "branch_id" in row and row["branch_id"] is not None:
            if target_row["branch_id"] != row["branch_id"]:
                return False
        if edge.target == "PlayerDevice":
            if any(row.get(key) != target_row[key] for key in ("branch_id", "channel_id")):
                return False
        if edge.key in {"AudioAssignment.music_playlist_id", "AudioAssignment.spot_playlist_id"}:
            required = "music" if edge.field == "music_playlist_id" else "spot"
            if enum_value(target_row["kind"]) != required:
                return False
        if edge.key == "ContentFolder.parent_id":
            seen = {ref}
            cursor = target
            while cursor is not None:
                if cursor in seen:
                    return False
                seen.add(cursor)
                data = self.snapshot(cursor)
                if data is None or self.resolve_owner(cursor).client_id != source_owner.client_id:
                    return False
                cursor = RowRef("ContentFolder", data["parent_id"]) if data["parent_id"] else None
        if profile == "RUNTIME" and not self.active(target):
            return False
        return True

    def collection_children(self, ref, profile):
        from sqlalchemy import select
        refs = set()
        for key in COLLECTION_DEPENDENCIES:
            edge = relation_spec(key)
            if edge.target == ref.model and profile in edge.operations:
                cls = model_class(edge.model)
                with self.session.no_autoflush:
                    refs.update(RowRef(edge.model, value) for value in self.session.scalars(
                        select(cls.id).where(getattr(cls, edge.field) == ref.identity)))
        return tuple(sorted(refs, key=lambda x: (x.model, x.identity)))

    def context_edges(self, ref, profile):
        return tuple(edge for edge in relations_for(ref.model) if profile in edge.operations)

    def context_integrity(self, ref, context, *, profile="WRITE", snapshot=None):
        """Validate the registered dependency closure, separately from ownership.

        Each persisted identity is visited once. Proven dataset/import and audio
        owner backlinks can close a cycle; acyclic policies such as folder.parent
        are checked locally. Unknown nodes or invalid edges never close a proof.
        READ dependencies are strict: only the top-level response may neutralize
        its own optional field; it cannot promise a corrupt dependency is safe.
        """
        if profile not in {"READ", "WRITE", "DELETE", "RUNTIME"}:
            raise conflict()
        ref = self.reference(ref)
        queue, checked, valid = [ref], set(), True
        while queue:
            current = queue.pop(0)
            if current in checked:
                continue
            checked.add(current)
            row = snapshot if current == ref and snapshot is not None else self.snapshot(current)
            owner = self.resolve_owner(current)
            if (row is None or owner.state not in {OwnerState.OWNED, OwnerState.GLOBAL_PRINCIPAL}
                    or not self.row_shape_valid(current, row)):
                valid = False
                continue
            if profile == "RUNTIME" and not self.active(current):
                valid = False
            for edge in self.context_edges(current, profile):
                value = row.get(edge.field)
                target = RowRef(edge.target, value) if type(value) is str and value else None
                if not self._relation_local_valid(current, edge, snapshot=row, profile=profile, context=context):
                    valid = False
                    if target is not None:
                        checked.add(target)
                elif target is not None:
                    queue.append(target)
            queue.extend(self.collection_children(current, profile))
        state = ContextIntegrityState.CONSISTENT if valid else ContextIntegrityState.INCONSISTENT
        return ContextIntegrity(state, tuple(sorted(checked, key=lambda x: (x.model, x.identity))))

    def relation_valid(self, ref, edge, *, snapshot=None, profile="WRITE", context=None):
        edge = relation_spec(edge.key if hasattr(edge, "key") else edge)
        ref = self.reference(ref)
        row = self.snapshot(ref) if snapshot is None else snapshot
        if not self._relation_local_valid(ref, edge, snapshot=row, profile=profile, context=context):
            return False
        value = row.get(edge.field)
        return value is None or self.context_integrity(
            RowRef(edge.target, value), context, profile=profile
        ).state == ContextIntegrityState.CONSISTENT

    def active(self, ref):
        row = self.snapshot(ref)
        if row is None:
            return False
        if ref.model == "Client":
            return row["status"] == "active"
        return not any(row.get(key) is False for key in ("is_active", "is_enabled", "enabled"))

    def invalid_relations(self, ref, *, snapshot=None, profile="WRITE", context=None):
        if profile not in {"READ", "WRITE", "DELETE", "RUNTIME"}:
            raise conflict()
        return tuple(
            r for r in relations_for(ref.model) if profile in r.operations
            and not self.relation_valid(ref, r, snapshot=snapshot, profile=profile, context=context)
        )

    def owner_chain_valid(self, ref, context, *, profile="WRITE", _visited=frozenset()):
        ref = self.reference(ref)
        if ref in _visited:
            return False
        spec = model_spec(ref.model)
        if spec.owner_kind == "root":
            return profile != "RUNTIME" or self.active(ref)
        row = self.snapshot(ref)
        parent_id = row.get(spec.owner_field) if row is not None else None
        if not parent_id:
            return False
        parent = RowRef(spec.owner_target, parent_id)
        bad = self.invalid_relations(parent, profile=profile, context=context)
        if any(profile != "READ" or r.read_policy != "FOREIGN_FIELD_NEUTRALIZED" for r in bad):
            return False
        if profile == "RUNTIME" and not self.active(parent):
            return False
        return self.owner_chain_valid(parent, context, profile=profile, _visited=_visited | {ref})

    def assert_integrity(self, ref, context, *, snapshot=None, profile="WRITE"):
        ref = self.reference(ref)
        self.authorize_row(ref, context)
        if self.context_integrity(ref, context, profile=profile, snapshot=snapshot).state != ContextIntegrityState.CONSISTENT:
            raise conflict()
        return True

    def row_shape_valid(self, ref, row):
        if ref.model == "CampaignSequenceItem":
            kind = enum_value(row["item_type"])
            if kind not in {"content", "layout"} or not row.get(kind + "_id"):
                return False
        return True


def context_for_actor(session, user, *, parent=None, client_id=None, branch_id=None, parent_relation=None):
    """Only authenticated ORM users are accepted; no payload role/client evidence."""
    graph = TenantGraph(session)
    ref = graph.reference(user)
    if ref.model != "User" or isinstance(user, RowRef):
        raise conflict()
    row = graph.snapshot(ref)
    if row is None or not row["is_active"] or row["status"] != "active":
        raise conflict()
    owner = graph.resolve_owner(ref)
    role = enum_value(row["role"])
    if owner.state not in {OwnerState.OWNED, OwnerState.GLOBAL_PRINCIPAL} or role not in ROLES:
        raise conflict()
    if row["branch_id"]:
        branch_owner = graph.resolve_owner(RowRef("Branch", row["branch_id"]))
        if branch_owner.state != OwnerState.OWNED or branch_owner.client_id != owner.client_id:
            raise conflict()
    actor = ActorAuthorization(ref, role, owner.client_id, row["branch_id"])
    parent = graph.reference(parent) if parent is not None else None
    if parent is not None:
        parent_owner = graph.resolve_owner(parent)
        if parent_owner.state != OwnerState.OWNED:
            raise conflict()
        if client_id is not None and client_id != parent_owner.client_id:
            raise conflict()
        client_id = parent_owner.client_id
    if role not in GLOBAL_ROLES:
        if client_id is not None and client_id != owner.client_id:
            raise conflict()
        client_id = owner.client_id
    elif client_id is not None and graph.resolve_owner(RowRef("Client", client_id)).state != OwnerState.OWNED:
        raise conflict()
    if role in BRANCH_ROLES:
        if branch_id is not None and branch_id != row["branch_id"]:
            raise conflict()
        branch_id = row["branch_id"]
    if branch_id is not None:
        branch_owner = graph.resolve_owner(RowRef("Branch", branch_id), client_id)
        if client_id is None or branch_owner.state != OwnerState.OWNED:
            raise conflict()
    context = OperationContext(actor, client_id, branch_id, parent, parent_relation)
    if parent is not None:
        graph.authorize_row(parent, context)
    return context


def assert_same_tenant_relation(graph, source, edge_key, context, *, profile="WRITE"):
    source = graph.reference(source)
    graph.authorize_row(source, context)
    if not graph.relation_valid(source, edge_key, profile=profile, context=context):
        raise conflict()
    return True
