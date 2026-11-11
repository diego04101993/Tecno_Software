"""SQL eligibility built before sorting, count, search and pagination."""
from app.services.tenant_relations import (
    GLOBAL_ROLES, ROLES, conflict, model_class, model_spec, relation_spec, relations_for, validate_registry,
    COLLECTION_DEPENDENCIES,
)


def _owner(entity, name, seen=()):
    from sqlalchemy import select, null
    from sqlalchemy.orm import aliased
    spec = model_spec(name)
    if name in seen:
        raise conflict()
    if spec.owner_kind == "root":
        return entity.id
    parent = aliased(model_class(spec.owner_target))
    value = _owner(parent, spec.owner_target, seen + (name,))
    return select(value).where(parent.id == getattr(entity, spec.owner_field)).correlate(entity).scalar_subquery()


def _branch(entity, name, seen=()):
    from sqlalchemy import select, null, func
    from sqlalchemy.orm import aliased
    spec = model_spec(name)
    if name in seen:
        raise conflict()
    if spec.branch_policy == "SELF":
        return entity.id
    if spec.branch_policy == "SHARED":
        return null()
    if spec.branch_policy == "FIELD":
        if "channel_id" in spec.columns:
            channel = aliased(model_class("Channel"))
            channel_branch = select(channel.branch_id).where(channel.id == entity.channel_id).correlate(entity).scalar_subquery()
            return func.coalesce(entity.branch_id, channel_branch)
        return entity.branch_id
    parent = aliased(model_class(spec.owner_target))
    return select(_branch(parent, spec.owner_target, seen + (name,))).where(
        parent.id == getattr(entity, spec.owner_field)
    ).correlate(entity).scalar_subquery()


def _active(entity, name):
    from sqlalchemy import and_, true
    spec = model_spec(name)
    if name == "Client":
        return entity.status == "active"
    return and_(true(), *(getattr(entity, key).is_(True)
                         for key in ("is_active", "is_enabled", "enabled") if key in spec.columns))


def _parent_scope(entity, name, context, seen=()):
    from sqlalchemy import select, true, false
    from sqlalchemy.orm import aliased
    parent = context.parent
    if parent is None or parent.model == "Client" or name == "Client":
        return true()
    if name in seen or context.parent_relation is None:
        return false()
    edge = relation_spec(context.parent_relation)
    if name == edge.model:
        return getattr(entity, edge.field) == parent.identity
    if name == parent.model:
        return entity.id == parent.identity
    spec = model_spec(name)
    if spec.owner_kind != "structural-owner":
        return false()
    ancestor = aliased(model_class(spec.owner_target))
    return select(ancestor.id).where(
        ancestor.id == getattr(entity, spec.owner_field),
        _parent_scope(ancestor, spec.owner_target, context, seen + (name,)),
    ).correlate(entity).exists()


def _edge_predicate(entity, edge, owner, profile, context, *, bad=None, cycles=None):
    from sqlalchemy import select, and_, or_
    from sqlalchemy.orm import aliased
    target = aliased(model_class(edge.target))
    condition = _owner(target, edge.target) == owner
    if edge.key in {"LayoutRevision.created_by_user_id", "VideowallPublication.requested_by_user_id"}:
        condition = or_(condition, and_(target.role.in_(tuple(GLOBAL_ROLES)),
                                        target.client_id.is_(None), target.branch_id.is_(None)))
    conditions = [target.id == getattr(entity, edge.field), condition]
    if context.branch_id is not None:
        target_branch = _branch(target, edge.target)
        conditions.append(or_(target_branch.is_(None), target_branch == context.branch_id))
    if bad is not None:
        conditions.append(~select(bad.c.identity).where(
            bad.c.model == edge.target, bad.c.identity == target.id).exists())
    if edge.key == "ContentFolder.parent_id" and cycles is not None:
        conditions.append(~select(cycles.c.origin).where(
            cycles.c.origin == entity.id, cycles.c.current == entity.id).exists())
    if edge.key in {"DatasetColumn.import_id", "DatasetRow.import_id", "Dataset.current_import_id"}:
        conditions.append(target.dataset_id == (entity.id if edge.model == "Dataset" else entity.dataset_id))
    if edge.key in {"TouchExperience.home_screen_id", "TouchExperience.attract_screen_id"}:
        conditions.append(target.experience_id == entity.id)
    if edge.key == "KioskButton.target_screen_id":
        screen = aliased(model_class("KioskScreen"))
        exp = select(screen.experience_id).where(screen.id == entity.screen_id).correlate(entity).scalar_subquery()
        conditions.append(or_(target.experience_id == exp, and_(target.experience_id.is_(None), exp.is_(None))))
    if edge.target == "Channel" and "branch_id" in model_spec(edge.model).columns:
        conditions.append(or_(entity.branch_id.is_(None), target.branch_id == entity.branch_id))
    if edge.target == "PlayerDevice":
        conditions.extend((target.branch_id == entity.branch_id, target.channel_id == entity.channel_id))
    if edge.key == "Videowall.active_publication_id":
        conditions.extend((target.wall_id == entity.id, target.playback_group_id.is_(None),
                           or_(entity.media_delivery == "legacy", and_(
                               target.status == "ACTIVE", target.loop_start_epoch_ms.is_not(None)))))
    if edge.key == "VideowallPlaybackGroup.active_publication_id":
        conditions.extend((target.playback_group_id == entity.id, target.wall_id == entity.wall_id,
                           target.campaign_id == entity.campaign_id, entity.mode == "SYNCHRONIZED",
                           target.status == "ACTIVE", target.loop_start_epoch_ms.is_not(None)))
    if edge.key == "VideowallPublication.playback_group_id":
        conditions.extend((target.wall_id == entity.wall_id, target.branch_id == entity.branch_id))
    if edge.key == "VideowallPlaybackGroupMember.node_id":
        group = aliased(model_class("VideowallPlaybackGroup"))
        channel = aliased(model_class("Channel"))
        conditions.append(select(group.id).join(channel, channel.id == target.channel_id).where(
            group.id == entity.group_id, target.videowall_id == group.wall_id,
            target.layout_enabled.is_(True), channel.branch_id == group.branch_id).correlate(entity, target).exists())
    if edge.key == "VideowallPublicationAsset.node_id":
        publication = aliased(model_class("VideowallPublication"))
        channel = aliased(model_class("Channel"))
        conditions.append(select(publication.id).join(channel, channel.id == target.channel_id).where(
            publication.id == entity.publication_id, target.videowall_id == publication.wall_id,
            channel.branch_id == publication.branch_id).correlate(entity, target).exists())
    if edge.key in {"AudioAssignment.music_playlist_id", "AudioAssignment.spot_playlist_id"}:
        conditions.append(target.kind == ("music" if edge.field == "music_playlist_id" else "spot"))
    if profile == "RUNTIME":
        conditions.append(_active(target, edge.target))
    exists = select(target.id).where(*conditions).correlate(entity).exists()
    return or_(getattr(entity, edge.field).is_(None), exists) if edge.nullable else exists


def _integrity(entity, name, profile, context, *, bad=None, strict=False, cycles=None, collections=True):
    from sqlalchemy import and_, or_, true
    owner = _owner(entity, name)
    predicates = [true()]
    for edge in relations_for(name):
        if profile not in edge.operations:
            continue
        if not strict and profile == "READ" and edge.read_policy == "FOREIGN_FIELD_NEUTRALIZED":
            continue
        predicates.append(_edge_predicate(entity, edge, owner, profile, context, bad=bad, cycles=cycles))
    if bad is not None and collections:
        from sqlalchemy import select
        from sqlalchemy.orm import aliased
        for key in COLLECTION_DEPENDENCIES:
            edge = relation_spec(key)
            if edge.target == name and profile in edge.operations:
                child = aliased(model_class(edge.model))
                predicates.append(~select(child.id).where(
                    getattr(child, edge.field) == entity.id,
                    select(bad.c.identity).where(bad.c.model == edge.model,
                                                 bad.c.identity == child.id).exists(),
                ).correlate(entity).exists())
    if name == "CampaignSequenceItem":
        predicates.append(or_(
            and_(entity.item_type == "content", entity.content_id.is_not(None)),
            and_(entity.item_type == "layout", entity.layout_id.is_not(None)),
        ))
    if profile == "RUNTIME":
        predicates.append(_active(entity, name))
    return and_(*predicates)


def _invalid_closure(name, profile, context, *, projection=False):
    """SQL fixed point over registered dependencies, before any pagination.

    UNION (not UNION ALL recursion) visits each invalid (model, id) once.
    Valid backlinks need no depth cutoff; an invalid node propagates through
    every required edge, including cycles. No Python post-filter or DB writes.
    """
    from sqlalchemy import select, union_all, literal, and_, or_, not_, true
    from sqlalchemy.orm import aliased
    names, pending = set(), [name]
    while pending:
        current = pending.pop()
        if current in names:
            continue
        names.add(current)
        pending.extend(e.target for e in relations_for(current) if profile in e.operations)
        if not projection:
            pending.extend(relation_spec(k).model for k in COLLECTION_DEPENDENCIES
                           if relation_spec(k).target == current and profile in relation_spec(k).operations)
    cycles = None
    if "ContentFolder" in names:
        folder = aliased(model_class("ContentFolder"))
        cycles = select(folder.id.label("origin"), folder.parent_id.label("current")).where(
            folder.parent_id.is_not(None)).cte(recursive=True)
        parent = aliased(model_class("ContentFolder"))
        cycles = cycles.union(select(cycles.c.origin, parent.parent_id).join(
            parent, parent.id == cycles.c.current).where(parent.parent_id.is_not(None)))
    seeds, links = [], []
    for current in sorted(names):
        entity = aliased(model_class(current))
        owner_ok = _owner(entity, current).is_not(None)
        if current == "User":
            owner_ok = or_(and_(owner_ok, entity.role.in_(tuple(ROLES - GLOBAL_ROLES))),
                           and_(entity.role.in_(tuple(GLOBAL_ROLES)), entity.client_id.is_(None),
                                entity.branch_id.is_(None)))
        valid = and_(owner_ok, _integrity(entity, current, profile, context,
                                        strict=not projection, cycles=cycles))
        seeds.append(select(literal(current).label("model"), entity.id.label("identity")).where(not_(valid)))
        for edge in relations_for(current):
            if profile not in edge.operations:
                continue
            if projection and edge.read_policy == "FOREIGN_FIELD_NEUTRALIZED":
                continue
            target_id = getattr(entity, edge.field)
            links.append(select(literal(current).label("source_model"), entity.id.label("source_id"),
                                literal(edge.target).label("target_model"), target_id.label("target_id"))
                         .where(target_id.is_not(None)))
            if not projection and edge.key in COLLECTION_DEPENDENCIES:
                links.append(select(literal(edge.target).label("source_model"), target_id.label("source_id"),
                                    literal(current).label("target_model"), entity.id.label("target_id"))
                             .where(target_id.is_not(None)))
    seed = union_all(*seeds).subquery()
    bad = select(seed.c.model, seed.c.identity).cte(recursive=True)
    if links:
        edges = union_all(*links).cte()
        bad = bad.union(select(edges.c.source_model, edges.c.source_id).join(
            bad, and_(edges.c.target_model == bad.c.model, edges.c.target_id == bad.c.identity)))
    return bad, cycles


def tenant_safe_query(statement, model, context, *, profile="READ", include_global_principals=False):
    """A single-model Select only; no joined foreign projections or pre-paged input.

    Branch-shared resources remain tenant scoped; endpoint-specific assignment or
    opaque media predicates must be added by A7-A9 before any pagination.
    """
    from sqlalchemy import Select, and_, or_, select
    from app.services.tenant_ownership import OperationContext
    validate_registry()
    name = model if isinstance(model, str) else getattr(model, "__name__", None)
    cls = model_class(name)
    if (profile not in {"READ", "RUNTIME"} or not isinstance(context, OperationContext)
            or not isinstance(statement, Select) or type(include_global_principals) is not bool):
        raise conflict()
    # Reject aliases, joins, aggregates, distinct and page boundaries: callers
    # aggregate/count a subquery of the returned eligible set, never the reverse.
    if (statement._limit_clause is not None or statement._offset_clause is not None
            or statement._fetch_clause is not None or statement._group_by_clauses
            or statement._distinct or statement._order_by_clauses
            or tuple(statement.get_final_froms()) != (cls.__table__,)
            or len(statement.column_descriptions) != 1
            or statement.column_descriptions[0].get("expr") is not cls):
        raise conflict()
    edges = relations_for(name)
    if edges and not any(profile in r.operations for r in edges):
        raise conflict()
    owner = _owner(cls, name)
    source_valid = owner.is_not(None)
    if name == "User":
        source_valid = and_(source_valid, cls.role.in_(tuple(ROLES - GLOBAL_ROLES)))
    if include_global_principals:
        if (name != "User" or profile != "READ" or context.actor.role not in GLOBAL_ROLES
                or context.client_id is not None or context.parent is not None or context.branch_id is not None):
            raise conflict()
        source_valid = or_(source_valid, and_(
            cls.role.in_(tuple(GLOBAL_ROLES)), cls.client_id.is_(None), cls.branch_id.is_(None)))
    predicates = [source_valid]
    if context.client_id is not None:
        predicates.append(owner == context.client_id)
    if context.parent is not None:
        parent_cls = model_class(context.parent.model)
        predicates.append(select(parent_cls.id).where(
            parent_cls.id == context.parent.identity,
            _owner(parent_cls, context.parent.model) == context.client_id,
        ).exists())
        predicates.append(_parent_scope(cls, name, context))
    if context.branch_id is not None:
        branch = _branch(cls, name)
        predicates.append(or_(branch.is_(None), branch == context.branch_id))
    bad, cycles = _invalid_closure(name, profile, context)
    predicates.append(_integrity(cls, name, profile, context, bad=bad, cycles=cycles))
    if profile == "RUNTIME":
        predicates.append(_active(cls, name))
        client = model_class("Client")
        predicates.append(select(client.id).where(client.id == owner, client.status == "active").exists())
    return statement.where(*predicates)


def read_integrity_predicate(model, *, profile="READ", client_id=None, branch_id=None):
    """Context integrity only; callers retain their existing actor/RBAC checks.

    The predicate is part of SQL WHERE, including when a caller later counts,
    searches, orders or pages the eligible set. It never repairs a stored row.
    """
    from types import SimpleNamespace
    from sqlalchemy import and_, or_, select
    name = model if isinstance(model, str) else model.__name__
    cls = model_class(name)
    if profile not in {"READ", "RUNTIME"}:
        raise conflict()
    context = SimpleNamespace(branch_id=branch_id)
    owner = _owner(cls, name)
    valid_owner = owner.is_not(None)
    if name == "User":
        valid_owner = or_(
            and_(valid_owner, cls.role.in_(tuple(ROLES - GLOBAL_ROLES))),
            and_(cls.role.in_(tuple(GLOBAL_ROLES)), cls.client_id.is_(None), cls.branch_id.is_(None)),
        )
    # Readable aggregates keep valid children while each child is independently
    # filtered. A bad sibling must not poison a whole administrative collection.
    # Runtime selectors retain the stricter collection/priority dependency rules.
    projection = profile == "READ"
    bad, cycles = _invalid_closure(name, profile, context, projection=projection)
    conditions = [valid_owner, _integrity(cls, name, profile, context, bad=bad,
                                         cycles=cycles, collections=not projection)]
    if client_id is not None:
        conditions.append(owner == client_id)
    if branch_id is not None:
        branch = _branch(cls, name)
        conditions.append(or_(branch.is_(None), branch == branch_id))
    if profile == "RUNTIME":
        client = model_class("Client")
        conditions.extend((_active(cls, name), select(client.id).where(
            client.id == owner, client.status == "active").exists()))
    return and_(*conditions)


def _is_canonical_projection(statement, cls):
    from sqlalchemy import Select, select

    if type(statement) is not Select or statement.get_execution_options():
        return False
    descriptions = statement.column_descriptions
    if len(descriptions) != 1 or descriptions[0].get("entity") is not cls:
        return False
    expression = descriptions[0]["expr"]
    if expression is not cls and expression is not cls.id:
        return False
    canonical = select(expression)
    # Select.compare omits these options, including explicit correlate(None).
    if (statement._auto_correlate != canonical._auto_correlate
            or statement._compile_options is not canonical._compile_options):
        return False
    # ORM alias context can defeat SQLAlchemy's structural comparison.
    # An unsupported comparison must take the existing construction path.
    try:
        return statement.compare(canonical, compare_annotations=True)
    except AttributeError:
        return False


def _read_projection_predicate(statement, cls, db, profile, client_id, branch_id):
    cache = getattr(db, "info", {}).get("a9_read_predicates")
    # A fresh administrative response projection owns this cache. Only reuse
    # integrity SQL for bare selects of the canonical table or its ID column.
    # Actor authorization and statement-specific filters still run separately.
    if (cache is not None and type(profile) is str and profile in ("READ", "RUNTIME")
            and (client_id is None or type(client_id) is str)
            and (branch_id is None or type(branch_id) is str)
            and _is_canonical_projection(statement, cls)):
        key = (cls, profile, client_id, branch_id)
        if key not in cache:
            cache[key] = read_integrity_predicate(cls, profile=profile, client_id=client_id, branch_id=branch_id)
        return cache[key]
    return read_integrity_predicate(cls, profile=profile, client_id=client_id, branch_id=branch_id)


def readable_query(statement, model, *, db=None, profile="READ", client_id=None, branch_id=None,
                   content_keys: set[tuple[str, str]] | None = None):
    """Add integrity and opaque-media eligibility before SQL pagination.

    Filesystem aliases cannot be compared safely as SQL strings. Their complete
    eligible ID set is computed before executing the result/count/page query.
    Runtime playback can supply the exact content keys already in its query.
    This function grants no actor authorization and does not change write paths.
    """
    name = model if isinstance(model, str) else model.__name__
    if content_keys is not None and (name != "ContentItem" or profile != "RUNTIME"):
        raise conflict()
    cls = model_class(name)
    statement = statement.where(_read_projection_predicate(statement, cls, db, profile, client_id, branch_id))
    if name in {"ContentItem", "DatasetImport"}:
        if db is None:
            raise conflict()
        from app.services.media_references import readable_media_ids
        statement = statement.where(cls.id.in_(readable_media_ids(db, name, content_keys=content_keys)))
    fields = model_spec(name).opaque_fields
    if name in {"VideowallPublication", "VideowallPublicationAsset"}:
        if db is None:
            raise conflict()
        from app.services.videowall_integrity import readable_publication_ids
        statement = statement.where(cls.id.in_(readable_publication_ids(db, name, profile=profile)))
    if fields and name not in {"ContentItem", "DatasetImport", "LayoutRevision", "User", "PlayerDevice",
                               "VideowallPublication", "VideowallPublicationAsset"}:
        if db is None:
            raise conflict()
        from app.services.media_references import readable_payload_ids
        cache = db.info.setdefault("a9_payload_ids", {}) if db.info.get("a9_snapshot") else {}
        if name not in cache:
            cache[name] = readable_payload_ids(db, name, fields)
        statement = statement.where(cls.id.in_(cache[name]))
    # Reports must count exactly the same eligible content that serializers may
    # name. An unsafe media reference cannot contribute an anonymous extra count.
    if name in {"AudioPlaybackEvent", "AudioPlaylistItem", "CampaignPlaylistItem", "CampaignSequenceItem"}:
        from sqlalchemy import or_
        content = model_class("ContentItem")
        from sqlalchemy import select
        content_ids = readable_query(select(content.id), content, db=db, profile="READ")
        statement = statement.where(or_(cls.content_id.is_(None), cls.content_id.in_(content_ids)))
    return statement.execution_options(a9_read_integrity=True)
