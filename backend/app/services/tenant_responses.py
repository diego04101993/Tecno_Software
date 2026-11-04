"""Explicit response projections; persistent ORM state is never rewritten."""
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Mapping
from enum import Enum

from app.services.tenant_relations import GLOBAL_ROLES, conflict, model_spec, relations_for
from app.services.tenant_ownership import OwnerState, TenantGraph, ContextIntegrityState

OMIT = object()


@dataclass(frozen=True)
class FieldAdapter:
    """Backend code, never request data. Validates opaque field provenance.

    A7-A9 supply semantic adapters for media/editor/data payloads. Without one,
    projection of an opaque field fails closed instead of returning raw JSON.
    """
    field: str
    validate_and_copy: object

    def __post_init__(self):
        if not callable(self.validate_and_copy):
            raise conflict()


@dataclass(frozen=True)
class ProjectionSpec:
    model: str
    fields: tuple[str, ...]
    adapters: tuple[FieldAdapter, ...] = ()
    allow_global_principal: bool = False

    def __post_init__(self):
        spec = model_spec(self.model)
        if (type(self.fields) is not tuple or len(set(self.fields)) != len(self.fields)
                or any(f not in spec.columns for f in self.fields)
                or type(self.adapters) is not tuple
                or len({a.field for a in self.adapters}) != len(self.adapters)
                or any(a.field not in self.fields for a in self.adapters)):
            raise conflict()
        if any(f in self.fields for f in ("password_hash", "player_token_hash")):
            raise conflict()
        if type(self.allow_global_principal) is not bool or (self.allow_global_principal and self.model != "User"):
            raise conflict()


def _copy(value):
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_copy(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return deepcopy(value)


def tenant_safe_response(graph: TenantGraph, ref, context, projection, *, profile="READ"):
    from fastapi import HTTPException
    ref = graph.reference(ref)
    if not isinstance(projection, ProjectionSpec) or ref.model != projection.model:
        raise conflict()
    if profile not in {"READ", "RUNTIME"}:
        raise conflict()
    edges = relations_for(ref.model)
    if edges and not any(profile in edge.operations for edge in edges):
        return OMIT
    global_principal = (
        projection.allow_global_principal and profile == "READ"
        and context.actor.role in GLOBAL_ROLES and context.client_id is None
        and context.parent is None and context.branch_id is None
        and graph.resolve_owner(ref).state == OwnerState.GLOBAL_PRINCIPAL
    )
    try:
        if not global_principal:
            graph.authorize_row(ref, context)
    except HTTPException:
        return OMIT
    row = graph.snapshot(ref)
    if row is None:
        return OMIT
    if not global_principal and not graph.owner_chain_valid(ref, context, profile=profile):
        return OMIT
    if not graph.row_shape_valid(ref, row):
        return OMIT
    for child in graph.collection_children(ref, profile):
        if graph.context_integrity(child, context, profile=profile).state != ContextIntegrityState.CONSISTENT:
            return OMIT
    invalid = graph.invalid_relations(ref, profile=profile, context=context)
    if any(profile != "READ" or r.read_policy != "FOREIGN_FIELD_NEUTRALIZED" for r in invalid):
        return OMIT
    if profile == "RUNTIME" and not graph.active(ref):
        return OMIT
    neutral = {r.field: None for r in invalid}
    for edge in invalid:
        if edge.key == "KioskButton.target_screen_id":
            neutral.update(action_value=None, action_payload_json={})
        if edge.key == "Dataset.current_import_id":
            neutral.update(row_count=0, column_count=0)
    adapters = {a.field: a for a in projection.adapters}
    output = {}
    for field in projection.fields:
        if field in neutral:
            output[field] = _copy(neutral[field])
        elif field in model_spec(ref.model).opaque_fields:
            adapter = adapters.get(field)
            if adapter is None:
                return OMIT
            value = adapter.validate_and_copy(graph, ref, context, row)
            if value is OMIT:
                return OMIT
            output[field] = _copy(value)
        else:
            output[field] = _copy(row[field])
    return output


def safe_model_read(row, schema, db, *, client_id=None, profile="READ"):
    """Copy an already scoped row and neutralize approved optional relations.

    This is response integrity, not authorization. Query eligibility must have
    run before paging; a detail caller receives a generic 404 if ineligible.
    """
    from types import SimpleNamespace
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.services.tenant_queries import readable_query
    model = type(row)
    key = (model, profile, client_id)
    cache = db.info.setdefault("a9_read_eligible", {}) if db.info.get("a9_snapshot") else {}
    if key not in cache:
        cache[key] = frozenset(db.connection().scalars(readable_query(
            select(model.id), model, db=db, profile=profile, client_id=client_id)))
    if row.id not in cache[key]:
        raise HTTPException(status_code=404, detail="Resource not found")
    graph = db.info.setdefault("a9_read_graph", TenantGraph(db)) if db.info.get("a9_snapshot") else TenantGraph(db)
    invalid = graph.invalid_relations(graph.reference(row), profile=profile,
                                      context=SimpleNamespace(branch_id=None))
    changes = {edge.field: None for edge in invalid}
    if any(edge.key == "Dataset.current_import_id" for edge in invalid):
        changes.update(row_count=0, column_count=0)
    if any(edge.key == "KioskButton.target_screen_id" for edge in invalid):
        changes.update(action_value=None, action_payload_json={})
    if model.__name__ == "Channel":
        changes["current_playback"] = playback_label_for_read(row, db)
        if "device_outputs" in schema.model_fields:
            from app.models.entities import DeviceOutput
            from app.schemas.domain import DeviceOutputRead
            outputs = db.scalars(readable_query(select(DeviceOutput), DeviceOutput, db=db,
                                                client_id=row.client_id).where(
                DeviceOutput.root_channel_id == row.id).order_by(DeviceOutput.display_order))
            # Materialize the checked collection before Pydantic can lazy-load
            # the raw relationship. Never replace the relationship on the ORM.
            copied = {name: getattr(row, name) for name in schema.model_fields
                      if name != "device_outputs" and hasattr(row, name)}
            copied["computed_status"] = row.computed_status
            copied["device_outputs"] = [safe_model_read(output, DeviceOutputRead, db,
                                                        client_id=row.client_id) for output in outputs]
            return schema.model_validate(copied).model_copy(deep=True, update=changes)
    # Pydantic's deep copy disconnects nested dictionaries/lists from ORM JSON.
    return schema.model_validate(row).model_copy(deep=True, update=changes)


def playback_label_for_read(channel, db):
    """Rebuild telemetry-derived names from current scoped references.

    Historical free text is not ownership evidence. A missing or corrupt latest
    heartbeat has no trusted label; the stored Channel is never rewritten.
    """
    from sqlalchemy import select
    from app.models.entities import PlayerHeartbeat, ContentItem, Campaign, Layout, PlayerDevice
    from app.services.tenancy import safe_tenant_resource
    heartbeat = db.scalar(select(PlayerHeartbeat).where(
        PlayerHeartbeat.channel_id == channel.id).order_by(PlayerHeartbeat.received_at.desc()).limit(1))
    if heartbeat is None or heartbeat.client_id != channel.client_id or heartbeat.branch_id != channel.branch_id:
        return None
    player = db.get(PlayerDevice, heartbeat.player_device_id)
    if (player is None or player.client_id != channel.client_id or player.channel_id != channel.id
            or player.branch_id != channel.branch_id):
        return None
    labels = []
    for model, identity in ((ContentItem, heartbeat.current_content_id),
                            (Campaign, heartbeat.current_campaign_id), (Layout, heartbeat.current_layout_id)):
        if identity:
            resource = safe_tenant_resource(db, model, identity, channel.client_id)
            if resource is None:
                return None
            labels.append(resource.name)
    return labels[0] if labels else None


def read_snapshot(*, response_model=None):
    """One read-only MVCC snapshot per administrative read, through serialization.

    Authentication and route permissions remain explicit. A separate session
    avoids changing a caller's transaction or accidentally flushing its ORM.
    The copied response is fully materialized before the snapshot is closed.
    """
    from functools import wraps
    from inspect import signature
    from contextlib import ExitStack
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from sqlalchemy import event
    from fastapi import HTTPException
    from pydantic import TypeAdapter

    def decorate(function):
        sig = signature(function, eval_str=True)
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            source = bound.arguments.get("db")
            if not isinstance(source, Session):
                raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
            with ExitStack() as stack:
                bind = source.get_bind()
                if bind.dialect.name == "postgresql":
                    # Engine and Connection callers both get an independent
                    # connection; no isolation change to an active transaction.
                    engine = getattr(bind, "engine", bind)
                    connection = stack.enter_context(engine.connect().execution_options(
                        isolation_level="REPEATABLE READ"))
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    bind = connection
                db = stack.enter_context(Session(bind=bind, autoflush=False))
                db.info["a9_snapshot"] = True
                def deny_write(*_args, **_kwargs):
                    raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
                event.listen(db, "before_flush", deny_write)
                event.listen(db, "before_commit", deny_write)
                def read_statement(state):
                    if not state.is_select:
                        deny_write()
                event.listen(db, "do_orm_execute", read_statement)
                bound.arguments["db"] = db
                actor = bound.arguments.get("current_user")
                if actor is not None:
                    from app.models.entities import User
                    from app.services.user_admin import assert_user_session_allowed
                    from app.services.tenancy import validate_authenticated_tenant_state
                    actor = db.get(User, actor.id)
                    bound.arguments["current_user"] = validate_authenticated_tenant_state(
                        assert_user_session_allowed(actor, db), db)
                result = function(*bound.args, **bound.kwargs)
                if db.new or db.dirty or db.deleted:
                    raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
                if response_model is not None:
                    from typing import get_args, get_origin
                    from sqlalchemy import inspect as orm_inspect
                    def project(value, schema):
                        if get_origin(schema) is list:
                            return [project(item, get_args(schema)[0]) for item in value]
                        state = orm_inspect(value, raiseerr=False)
                        if state is not None and state.persistent:
                            return safe_model_read(value, schema, db)
                        return value
                    result = project(result, response_model)
                    adapter = TypeAdapter(response_model)
                    result = adapter.dump_python(adapter.validate_python(result, from_attributes=True))
                return _copy(result)
        wrapped.__signature__ = sig
        return wrapped
    return decorate
