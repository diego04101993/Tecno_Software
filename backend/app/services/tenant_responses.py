"""Explicit response projections; persistent ORM state is never rewritten."""
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Mapping
from contextlib import contextmanager
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


def safe_model_read(row, schema, db, *, client_id=None, profile="READ", channel_playback=None):
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
    if db.info.get("a9_snapshot"):
        graph = db.info.get("a9_read_graph")
        if graph is None:
            graph = TenantGraph(db, snapshot_transaction=db.info.get("a9_graph_snapshot_transaction"))
            db.info["a9_read_graph"] = graph
    else:
        graph = TenantGraph(db)
    invalid = graph.invalid_relations(graph.reference(row), profile=profile,
                                      context=SimpleNamespace(branch_id=None))
    changes = {edge.field: None for edge in invalid}
    if any(edge.key == "Dataset.current_import_id" for edge in invalid):
        changes.update(row_count=0, column_count=0)
    if any(edge.key == "KioskButton.target_screen_id" for edge in invalid):
        changes.update(action_value=None, action_payload_json={})
    if model.__name__ == "Channel":
        changes["current_playback"] = (playback_label_for_read(row, db) if channel_playback is None
                                       else channel_playback.get(row.id))
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


def latest_heartbeats_query(channel_ids, *, dialect_name="postgresql"):
    """One bounded latest-row lookup per visible channel, in one SQL statement.

    PostgreSQL can stop at the first entry in the channel/time/id index instead
    of sorting a channel's complete telemetry history. The window fallback is
    for SQLite tests; neither path materializes historical JSON or ORM rows.
    """
    from sqlalchemy import String, column, false, func, select, true, values
    from app.models.entities import PlayerHeartbeat
    fields = ("channel_id", "client_id", "branch_id", "player_device_id",
              "current_content_id", "current_campaign_id", "current_layout_id")
    columns = tuple(getattr(PlayerHeartbeat, name) for name in fields)
    identities = tuple(dict.fromkeys(channel_ids))
    if not identities:
        return select(*columns).where(false())
    order = (PlayerHeartbeat.received_at.desc(), PlayerHeartbeat.id.desc())
    if dialect_name == "postgresql":
        visible = values(column("channel_id", String(36)), name="visible_channels").data(
            [(identity,) for identity in identities])
        latest = select(*columns).where(
            PlayerHeartbeat.channel_id == visible.c.channel_id).order_by(*order).limit(1).lateral(
                "latest_heartbeat")
        return select(*(latest.c[name] for name in fields)).select_from(visible.join(latest, true()))
    ranked = select(*columns, func.row_number().over(
        partition_by=PlayerHeartbeat.channel_id, order_by=order).label("heartbeat_rank")).where(
            PlayerHeartbeat.channel_id.in_(identities)).subquery("ranked_heartbeats")
    return select(*(ranked.c[name] for name in fields)).where(ranked.c.heartbeat_rank == 1)


def playback_labels_for_read(channels, db):
    """Rebuild trusted playback labels for an already authorized channel set.

    Only the latest heartbeat for each supplied channel is materialized. Invalid
    latest rows fail closed; they never fall back to an older valid heartbeat.
    References are resolved in batches with the same RUNTIME eligibility used
    by safe_tenant_resource, including media, relation and active-client guards.
    Nothing is cached beyond this projection, including on write/detail paths.
    """
    from sqlalchemy import select, tuple_
    from app.models.entities import ContentItem, Campaign, Layout, PlayerDevice
    from app.services.tenant_queries import readable_query
    channels = {channel.id: channel for channel in channels}
    labels = dict.fromkeys(channels)
    if not channels:
        return labels
    with db.no_autoflush:
        latest = db.execute(latest_heartbeats_query(
            channels, dialect_name=db.get_bind().dialect.name)).mappings()
        heartbeats = {}
        for heartbeat in latest:
            channel = channels[heartbeat["channel_id"]]
            if (heartbeat["client_id"] == channel.client_id
                    and heartbeat["branch_id"] == channel.branch_id):
                heartbeats[channel.id] = heartbeat
        if not heartbeats:
            return labels
        player_keys = {(heartbeat["player_device_id"], channel_id, heartbeat["client_id"],
                        heartbeat["branch_id"]) for channel_id, heartbeat in heartbeats.items()}
        players = set(db.execute(select(PlayerDevice.id, PlayerDevice.channel_id,
                                        PlayerDevice.client_id, PlayerDevice.branch_id).where(
            tuple_(PlayerDevice.id, PlayerDevice.channel_id, PlayerDevice.client_id,
                   PlayerDevice.branch_id).in_(player_keys))).tuples())
        heartbeats = {channel_id: heartbeat for channel_id, heartbeat in heartbeats.items()
                      if (heartbeat["player_device_id"], channel_id, heartbeat["client_id"],
                          heartbeat["branch_id"]) in players}
        resources = {}
        for model, field in ((ContentItem, "current_content_id"),
                             (Campaign, "current_campaign_id"), (Layout, "current_layout_id")):
            keys = {(heartbeat["client_id"], heartbeat[field]) for heartbeat in heartbeats.values()
                    if heartbeat[field]}
            if not keys:
                resources[field] = {}
                continue
            query = readable_query(select(model.id, model.client_id, model.name).where(
                tuple_(model.client_id, model.id).in_(keys)), model, db=db, profile="RUNTIME")
            resources[field] = {(row.client_id, row.id): row.name for row in db.execute(query)}
        for channel_id, heartbeat in heartbeats.items():
            names = []
            for field in ("current_content_id", "current_campaign_id", "current_layout_id"):
                if heartbeat[field]:
                    key = (heartbeat["client_id"], heartbeat[field])
                    if key not in resources[field]:
                        break
                    names.append(resources[field][key])
            else:
                labels[channel_id] = names[0] if names else None
    return labels


def playback_label_for_read(channel, db):
    """Detail projection using the same trusted reconstruction as collections."""
    return playback_labels_for_read((channel,), db)[channel.id]


@contextmanager
def _projection_graph_scope(db):
    from sqlalchemy import Connection
    bind = db.get_bind()
    if isinstance(bind, Connection) and bind.dialect.name == "postgresql":
        # readonly_snapshot_session established or verified READ ONLY and
        # REPEATABLE READ before this scope receives the connection.
        db.info["a9_graph_snapshot_transaction"] = bind.get_transaction()
    try:
        yield
    finally:
        db.info.pop("a9_read_predicates", None)
        graph = db.info.pop("a9_read_graph", None)
        if graph is not None:
            graph.discard_snapshot_rows()
        db.info.pop("a9_graph_snapshot_transaction", None)


def read_snapshot(*, response_model=None):
    """One read-only MVCC snapshot per administrative read, through serialization.

    Authentication and route permissions remain explicit. Clean request reads
    release their checkout before opening the independent read-only snapshot.
    The copied response is fully materialized before the snapshot is closed.
    """
    from functools import wraps
    from inspect import signature
    from sqlalchemy.orm import Session
    from sqlalchemy import event
    from fastapi import HTTPException
    from pydantic import TypeAdapter
    from app.services.db_snapshots import readonly_snapshot_session

    def decorate(function):
        sig = signature(function, eval_str=True)
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            source = bound.arguments.get("db")
            if not isinstance(source, Session):
                raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
            actor = bound.arguments.get("current_user")
            actor_id = actor.id if actor is not None else None
            with readonly_snapshot_session(source) as db, _projection_graph_scope(db):
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
                if actor is not None:
                    from app.models.entities import User
                    from app.services.user_admin import assert_user_session_allowed
                    from app.services.tenancy import validate_authenticated_tenant_state
                    actor = db.get(User, actor_id)
                    bound.arguments["current_user"] = validate_authenticated_tenant_state(
                        assert_user_session_allowed(actor, db), db)
                result = function(*bound.args, **bound.kwargs)
                if db.new or db.dirty or db.deleted:
                    raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")
                if response_model is not None:
                    from typing import get_args, get_origin
                    from sqlalchemy import inspect as orm_inspect
                    def project(value, schema, playback=None):
                        if get_origin(schema) is list:
                            item_schema = get_args(schema)[0]
                            items = list(value)
                            from app.models.entities import Channel
                            channels = [item for item in items if type(item) is Channel
                                        and (state := orm_inspect(item, raiseerr=False)) is not None
                                        and state.persistent]
                            playback = playback_labels_for_read(channels, db) if channels else None
                            return [project(item, item_schema, playback) for item in items]
                        state = orm_inspect(value, raiseerr=False)
                        if state is not None and state.persistent:
                            return safe_model_read(value, schema, db, channel_playback=playback)
                        return value
                    db.info["a9_read_predicates"] = {}
                    result = project(result, response_model)
                    adapter = TypeAdapter(response_model)
                    result = adapter.dump_python(adapter.validate_python(result, from_attributes=True))
                return _copy(result)
        wrapped.__signature__ = sig
        return wrapped
    return decorate
