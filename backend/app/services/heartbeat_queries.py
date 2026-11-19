"""Request-local heartbeat retrieval; authorization and ownership stay with their validators."""
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import inspect, literal, select, union_all
from sqlalchemy.orm.attributes import set_committed_value

from app.models.entities import Branch, Campaign, Channel, Client, ContentItem, Layout, PlayerDevice
from app.services.tenant_relations import conflict


_BATCH = "heartbeat_query_batch"
_AUTH = "heartbeat_auth_rows"


@dataclass(frozen=True)
class _HeartbeatQueryScope:
    session: object
    transaction: object
    connection: object
    connection_transaction: object


def heartbeat_query_batch_active(db):
    scope = db.info.get(_BATCH)
    return (type(scope) is _HeartbeatQueryScope and scope.session is db
            and scope.transaction is db.get_transaction() and scope.transaction.is_active
            and db.connection() is scope.connection and not scope.connection.closed and scope.connection.in_transaction()
            and scope.connection_transaction is scope.connection.get_transaction())


@contextmanager
def heartbeat_query_scope(db):
    connection = db.connection()
    transaction = db.get_transaction()
    if transaction is None or not transaction.is_active:
        raise conflict()
    previous = db.info.get(_BATCH)
    db.info[_BATCH] = _HeartbeatQueryScope(db, transaction, connection, connection.get_transaction())
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_BATCH, None)
        else:
            db.info[_BATCH] = previous


def load_heartbeat_auth_rows(db, token_hash):
    """Load the same four auth records without filtering away an invalid scope."""
    rows = db.execute(select(PlayerDevice, Channel, Client, Branch)
        .select_from(PlayerDevice)
        .outerjoin(Channel, Channel.id == PlayerDevice.channel_id)
        .outerjoin(Client, Client.id == Channel.client_id)
        .outerjoin(Branch, Branch.id == Channel.branch_id)
        .where(PlayerDevice.player_token_hash == token_hash)
        .execution_options(populate_existing=True)).one_or_none()
    if rows is None:
        return None
    # Strong references last only until the heartbeat handler consumes them.
    # Existing tenancy validators still inspect these persisted ORM records.
    db.info[_AUTH] = (db.get_transaction(), rows[0], rows[1], rows[2], rows[3])
    return rows[0]


def take_heartbeat_channel(db, device):
    rows = db.info.pop(_AUTH, None)
    if (type(rows) is tuple and len(rows) == 5 and rows[0] is db.get_transaction()
            and rows[1] is device):
        return rows[2]
    return db.get(Channel, device.channel_id)


def refresh_heartbeat_roots(db, device, channel):
    """Refresh both original identities after rollback, without rebasing a moved device."""
    device_id, = inspect(device).identity
    channel_id, = inspect(channel).identity
    anchor = select(literal(1).label("anchor")).subquery()
    # Independent original PK predicates are intentional: joining Channel through
    # a newly moved Device would conceal the identity actually authenticated.
    db.execute(select(PlayerDevice, Channel).select_from(anchor)
        .outerjoin(PlayerDevice, PlayerDevice.id == device_id)
        .outerjoin(Channel, Channel.id == channel_id)
        .execution_options(populate_existing=True)).one()


def sanitize_heartbeat_references(payload, db, client_id):
    """Execute the original three eligibility projections in a single statement."""
    from app.services.tenancy import logger
    from app.services.tenant_queries import readable_query

    fields = (("current_content_id", ContentItem), ("current_campaign_id", Campaign),
              ("current_layout_id", Layout))
    updates, statements = {}, []
    for field, model in fields:
        identity = getattr(payload, field)
        updates[field] = None
        if not identity:
            continue
        eligible = readable_query(select(model), model, db=db, profile="RUNTIME",
                                  client_id=client_id, exact_id=identity).subquery()
        statement = select(literal(field).label("field"), eligible.c.id.label("identity"))\
            .where(eligible.c.client_id == client_id)
        if "is_active" in eligible.c:
            statement = statement.where(eligible.c.is_active.is_not(False))
        statements.append(statement)
    if statements:
        query = union_all(*statements) if len(statements) > 1 else statements[0]
        for field, identity in db.execute(query):
            updates[field] = identity
    for field, _model in fields:
        if getattr(payload, field) and updates[field] is None:
            logger.warning("TENANT_RELATION_OMITTED")
    return payload.model_copy(update=updates)


def retain_heartbeat_write_keys(db, plan, channel, device):
    """Restore only physically locked PKs after expire_all; preserve all other ORM history."""
    from app.services.tenant_mutations import _PlayerTelemetryGraph
    from app.services.tenant_ownership import RowRef

    gate = plan._gate
    graph = gate.graph
    if (type(graph) is not _PlayerTelemetryGraph or graph.session is not db
            or not gate.active or gate.transaction is not db.get_transaction()
            or plan.operation.function != "create_player_heartbeat"):
        raise conflict()
    for row in (channel, device):
        state = inspect(row)
        if not state.persistent or state.session is not db:
            raise conflict()
        identity, = state.identity
        ref = RowRef(state.mapper.class_.__name__, identity)
        snapshot = graph.snapshots.get(ref)
        proof = graph._snapshot_lock_proofs.get(ref)
        if (not isinstance(snapshot, MappingProxyType) or proof is None
                or proof[0] is not snapshot or proof[1] is not db.get_transaction()
                or snapshot["id"] != identity):
            raise conflict()
        # Non-PK attributes stay expired. Setting their supplied values below
        # therefore keeps the existing UPDATE/updated_at behavior, even if equal.
        set_committed_value(row, "id", identity)
