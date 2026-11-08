"""Exhaustive, locked SQL proofs for the two reviewed historical leaf models.

Only relationship columns enter PostgreSQL's locked SQL subquery. Every selected
row is still locked through transaction end, including its non-FK owner fields;
COUNT consumes the complete locked relation before approval can return. Python
receives a count and DISTINCT *individual* parent identities, never history IDs
or payloads. This deliberately does not claim constant database work.
"""
from dataclasses import dataclass

from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import RELATION_SPECS, conflict, model_class, model_spec


_FIELDS = {
    "PlayerHeartbeat": (
        ("client_id", "Client", False), ("branch_id", "Branch", False),
        ("channel_id", "Channel", False), ("player_device_id", "PlayerDevice", False),
        ("current_content_id", "ContentItem", True),
        ("current_campaign_id", "Campaign", True), ("current_layout_id", "Layout", True),
    ),
    "PlayerPlaybackEvent": (
        ("client_id", "Client", False), ("branch_id", "Branch", False),
        ("channel_id", "Channel", False), ("player_device_id", "PlayerDevice", False),
        ("content_id", "ContentItem", True), ("campaign_id", "Campaign", True),
        ("layout_id", "Layout", True),
    ),
}


@dataclass(frozen=True)
class TelemetryDependency:
    """Transaction-bound plan evidence; the enclosing MutationPlan owns its gate.

    The scope is the incoming FK equality, not a caller-supplied tenant filter.
    Count is diagnostic/equality evidence, not a replacement for the row locks
    and universal predicates that generated this certificate.
    """
    model: str
    relation_key: str
    parent: RowRef
    count: int
    references: tuple[RowRef, ...]


def _reviewed(model):
    fields = _FIELDS.get(model)
    if fields is None:
        return False
    spec = model_spec(model)
    if (spec.owner_kind != "direct-owner" or spec.owner_field != "client_id"
            or spec.owner_target != "Client" or spec.branch_policy != "FIELD"
            or any(edge.target == model for edge in RELATION_SPECS)):
        return False
    actual = tuple(sorted((edge.field, edge.target, edge.nullable, edge.operations)
                          for edge in RELATION_SPECS if edge.model == model))
    wanted = tuple(sorted((field, target, nullable, frozenset(("WRITE", "DELETE")))
                          for field, target, nullable in fields))
    if actual != wanted:
        return False
    # These are the only ownership/branch shapes encoded below. A future graph
    # change must use the original row-wise path until explicitly reviewed.
    for name in {target for _, target, _ in fields}:
        parent = model_spec(name)
        if name == "Client":
            if parent.owner_kind != "root":
                return False
        elif (parent.owner_kind != "direct-owner" or parent.owner_field != "client_id"
              or parent.owner_target != "Client"):
            return False
        expected = "SELF" if name == "Branch" else (
            "FIELD" if name in {"Channel", "PlayerDevice"} else "SHARED")
        if parent.branch_policy != expected:
            return False
    return True


def telemetry_scope_predicate(model, dependencies):
    """SQL membership in already approved incoming scopes, for explicit cleanup."""
    from sqlalchemy import false, or_
    cls = model_class(model)
    terms = []
    for dependency in dependencies:
        if not isinstance(dependency, TelemetryDependency) or dependency.model != model:
            continue
        if not _reviewed(model):
            raise conflict()
        field = next((field for field, target, _ in _FIELDS[model]
                      if model + "." + field == dependency.relation_key
                      and target == dependency.parent.model), None)
        if field is None or type(dependency.count) is not int or dependency.count < 0:
            raise conflict()
        terms.append(getattr(cls, field) == dependency.parent.identity)
    return or_(*terms) if terms else false()


def validate_telemetry_incoming(graph, view, parent, edge, proposals, context):
    """Return a locked universal proof, or None to retain the original traversal.

    Persisted current rows and proposed parent values are checked independently.
    Explicit new history proposals remain ordinary graph roots; an UPDATE or
    DELETE of an existing history row falls back rather than hiding its overlay.
    Parent context/opaque semantics are intentionally left to the graph caller.
    """
    from sqlalchemy import and_, case, false, func, literal, or_, select, true

    model = edge.model
    if (not _reviewed(model) or graph.session.get_bind().dialect.name != "postgresql"
            or not graph.locking or edge.target != parent.model
            or any(p.reference.model == model and p.previous is not None for p in proposals)):
        return None
    if any(p.reference.model == model and p.reference.identity == parent.identity
           for p in proposals):
        return None
    parent_row = graph.snapshot(parent)
    if parent_row is not None:
        proof = graph._snapshot_lock_proofs.get(parent)
        if (proof is None or proof[0] is not parent_row
                or proof[1] is not graph.session.get_transaction()):
            return None
    if getattr(context, "parent", None) is not None and not hasattr(context, "parent_relation"):
        return None
    fields = _FIELDS[model]
    table = model_class(model).__table__
    locked = (select(table.c.id, *(table.c[field] for field, _, _ in fields))
              .where(table.c[edge.field] == parent.identity).order_by(table.c.id)
              .with_for_update(of=table).subquery("tenant_telemetry_locked"))
    # Each DISTINCT aggregate is one FK, never a combination of seven keys.
    # The outer aggregate consumes every locked row without a CTE tuplestore.
    summary = select(func.count(), *(func.array_agg(locked.c[field].distinct()).filter(
        locked.c[field].is_not(None)) for field, _, _ in fields)).select_from(locked)
    result = graph.session.execute(summary).one()
    count, references = int(result[0]), set()
    for (_, target, _), identities in zip(fields, result[1:], strict=True):
        for identity in identities or ():
            if type(identity) is not str or not identity:
                raise conflict()
            references.add(RowRef(target, identity))
    ordered_refs = tuple(sorted(references, key=lambda ref: (ref.model, ref.identity)))
    # Parent columns in the universal SQL predicate must be locked before that
    # query: comparing them first, then only checking each parent's standalone
    # graph, would miss a concurrent parent ownership/branch change.
    for reference in ordered_refs:
        row = graph.snapshot(reference)
        proof = graph._snapshot_lock_proofs.get(reference)
        if row is not None and (proof is None or proof[0] is not row
                                or proof[1] is not graph.session.get_transaction()):
            raise conflict()
    # The parent anchor and all matching history rows are now locked. No new row
    # can acquire this incoming FK, nor can a selected row leave/change scope.
    # Re-read only relationship columns; do not re-send history IDs to Python.
    columns = [table.c[field] for field, _, _ in fields]
    columns.append(and_(table.c.id.is_not(None), table.c.id != "").label("_valid_identity"))
    if getattr(getattr(context, "parent", None), "model", None) == model:
        columns.append(table.c.id)
    # Local validity depends on these FKs, identity validity and the identity
    # itself for a same-model parent scope. Exact duplicate vectors have identical
    # current/proposed predicates, so SQL may check each distinct vector once.
    # Vectors never leave PostgreSQL; Python still receives individual parents.
    vectors = (select(*columns).where(table.c[edge.field] == parent.identity)
               .distinct().subquery("tenant_telemetry_vectors"))
    source = vectors.c
    joins = vectors
    parents = {}
    for field, target, _ in fields:
        alias = model_class(target).__table__.alias("telemetry_" + field)
        parents[field] = alias
        joins = joins.outerjoin(alias, alias.c.id == source[field])

    def parent_value(field, target_name, key, proposed):
        target = parents[field]
        column = target.c[key]
        replacements = tuple((target.c.id == p.reference.identity,
                              literal(p.values[key], type_=column.type))
                             for p in proposals if proposed and p.reference.model == target_name
                             and key in p.values)
        return case(*replacements, else_=column) if replacements else column

    def valid(proposed):
        conditions = [source._valid_identity.is_(True), source.client_id.is_not(None), source.client_id != ""]
        if context.client_id is not None:
            conditions.append(source.client_id == context.client_id)
        if context.branch_id is not None:
            conditions.append(source.branch_id == context.branch_id)
        if getattr(context, "parent", None) is not None:
            from app.services.tenant_relations import relation_spec
            scope = context.parent
            if scope.model == "Client":
                scope_ok = true()
            elif scope.model == model:
                scope_ok = source.id == scope.identity
            elif context.parent_relation is None:
                scope_ok = false()
            else:
                scope_edge = relation_spec(context.parent_relation)
                scope_ok = (source[scope_edge.field] == scope.identity
                            if scope_edge.model == model else false())
            conditions.append(scope_ok)
        for field, target_name, nullable in fields:
            target = parents[field]
            local = [source[field].is_not(None), source[field] != "", target.c.id.is_not(None)]
            owner = target.c.id if target_name == "Client" else parent_value(
                field, target_name, "client_id", proposed)
            local.extend((owner.is_not(None), owner != "", owner == source.client_id))
            if target_name == "Branch":
                branch = target.c.id
            elif target_name in {"Channel", "PlayerDevice"}:
                branch = parent_value(field, target_name, "branch_id", proposed)
            else:
                branch = None
            if context.branch_id is not None and branch is not None:
                local.append(or_(branch.is_(None), branch == context.branch_id))
            if target_name == "Channel":
                local.append(parent_value(field, target_name, "branch_id", proposed) == source.branch_id)
            if target_name == "PlayerDevice":
                local.extend((parent_value(field, target_name, "branch_id", proposed) == source.branch_id,
                              parent_value(field, target_name, "channel_id", proposed) == source.channel_id))
            predicate = and_(*local)
            conditions.append(or_(source[field].is_(None), predicate) if nullable else predicate)
        return and_(*conditions)

    current_and_proposed = and_(valid(False), valid(True))
    # A total-cost aggregate avoids EXISTS' startup-cost bias toward a full
    # sequential scan when every row is valid. Empty scopes remain valid.
    invalid = select(func.coalesce(func.bool_or(current_and_proposed.is_not(True)), False)).select_from(
        joins).where(source[edge.field] == parent.identity)
    if graph.session.scalar(invalid):
        raise conflict()
    return TelemetryDependency(model, edge.key, parent, count, ordered_refs)
