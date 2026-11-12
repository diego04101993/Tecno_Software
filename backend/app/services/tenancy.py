import logging
from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Branch, Campaign, CampaignChannelAssignment, CampaignPlaylistItem, Channel, Client, ContentItem, ContentFolder, DataSource, Dataset, DeviceOutput, Layout, LayoutDataBinding, KioskScreen, Schedule, TouchExperience, User, UserRole, Videowall, VideowallNode


logger = logging.getLogger(__name__)


def tenant_operation_context(db: Session, current_user: User, *, parent=None,
                             client_id=None, branch_id=None, parent_relation=None):
    """Opt-in A6 adapter; existing route permissions remain mandatory."""
    from app.services.tenant_ownership import context_for_actor
    return context_for_actor(db, current_user, parent=parent,
                             client_id=client_id, branch_id=branch_id, parent_relation=parent_relation)


def assert_related_clients(client_id: str, related_client_ids: Iterable[str | None]) -> None:
    """A parent grant never authorizes a foreign or unresolved dependent owner."""
    if not client_id or any(owner != client_id for owner in related_client_ids):
        logger.warning("TENANT_RELATION_CONFLICT")
        raise HTTPException(status_code=409, detail="TENANT_RELATION_CONFLICT")


def assert_related_clients_query(client_id: str, db: Session, statement: Select) -> None:
    """Prove every selected owner matches, returning one boolean rather than history.

    Callers supply the complete one-column owner query, without a tenant filter.
    IS DISTINCT FROM keeps NULL/unresolved ownership fail-closed.
    """
    assert_related_clients(client_id, ())
    owners = statement.subquery()
    if len(owners.c) != 1:
        assert_related_clients(client_id, (None,))
    owner = next(iter(owners.c))
    incompatible = select(1).select_from(owners).where(owner.is_distinct_from(client_id)).exists()
    if db.scalar(select(incompatible)):
        assert_related_clients(client_id, (None,))


def assert_same_client(resource, client_id: str):
    if resource is None or not client_id or resource.client_id != client_id:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource


def related_client_matches(model, foreign_key, client_id, *, optional: bool = False):
    """SQL read integrity, before pagination; a missing non-null parent fails closed.

    Administrative reads may include inactive resources, but never another owner.
    """
    match = select(model.id).where(model.id == foreign_key, model.client_id == client_id).correlate_except(model).exists()
    return or_(foreign_key.is_(None), match) if optional else match


def require_same_client_resource(db: Session, model, resource_id: str | None, client_id: str):
    if resource_id is None:
        return None
    return assert_same_client(db.get(model, resource_id), client_id)


def optional_relation_id(db: Session, model, resource_id: str | None, client_id: str, **context) -> str | None:
    """Read projection only: inactive administrative resources remain visible."""
    from app.services.tenant_queries import readable_query
    resource = db.scalar(readable_query(select(model).where(model.id == resource_id), model,
                                        db=db, client_id=client_id)) if resource_id else None
    if (resource is None or resource.client_id != client_id
            or any(getattr(resource, key) != value for key, value in context.items())):
        return None
    return resource.id


def channel_read_integrity():
    return related_client_matches(Branch, Channel.branch_id, Channel.client_id, optional=True)


def schedule_read_integrity():
    return and_(
        related_client_matches(Campaign, Schedule.campaign_id, Schedule.client_id),
        related_client_matches(Layout, Schedule.layout_id, Schedule.client_id, optional=True),
        related_client_matches(Branch, Schedule.branch_id, Schedule.client_id, optional=True),
        related_client_matches(Channel, Schedule.channel_id, Schedule.client_id, optional=True),
    )


def safe_tenant_resource(db: Session, model, resource_id: str | None, client_id: str):
    if not resource_id:
        return None
    from app.services.tenant_queries import readable_query
    from app.services.runtime_read import runtime_reads
    # Touch aggregates sanitize optional navigation targets and children
    # individually, preserving the valid remainder of a historical experience.
    profile = "READ" if model in {TouchExperience, KioskScreen} else "RUNTIME"
    if runtime_reads(db) is None:
        statement = readable_query(select(model).where(model.id == resource_id), model,
                                   db=db, profile=profile, client_id=client_id)
    else:
        content_keys = {(client_id, resource_id)} if model is ContentItem else None
        statement = readable_query(select(model), model, db=db, profile=profile, client_id=client_id,
                                   content_keys=content_keys).where(model.id == resource_id)
    resource = db.scalar(statement)
    if resource is None or resource.client_id != client_id or getattr(resource, "is_active", True) is False:
        # No IDs, names, input or stored metadata in security diagnostics.
        logger.warning("TENANT_RELATION_OMITTED")
        return None
    return resource


def validate_authenticated_tenant_state(user: User, db: Session) -> User:
    if is_global_workspace_user(user):
        return user
    if not user.client_id or db.get(Client, user.client_id) is None:
        raise HTTPException(status_code=403, detail="Invalid tenant identity")
    if is_branch_scoped(user):
        branch = db.get(Branch, user.branch_id) if user.branch_id else None
        if branch is None or branch.client_id != user.client_id:
            raise HTTPException(status_code=403, detail="Invalid branch identity")
    return user


def require_active_channel_tenant(channel: Channel | None, db: Session) -> Channel:
    client = db.get(Client, channel.client_id) if channel else None
    branch = db.get(Branch, channel.branch_id) if channel else None
    if (client is None or client.status != "active" or branch is None
            or branch.client_id != client.id or not branch.is_active):
        raise HTTPException(status_code=403, detail="Player operation unavailable")
    return channel


def require_screen_target(db: Session, screen: KioskScreen, target_id: str | None):
    target = require_same_client_resource(db, KioskScreen, target_id, screen.client_id)
    if target is not None and target.experience_id != screen.experience_id:
        raise HTTPException(status_code=404, detail="Target screen not found")
    return target


GLOBAL_WORKSPACE_ROLES = {UserRole.SUPER_ADMIN, UserRole.STAFF_ADMIN, UserRole.STAFF_OPERATOR}
GLOBAL_CLIENT_DIRECTORY_ROLES = {UserRole.SUPER_ADMIN, UserRole.STAFF_ADMIN}
INTERNAL_TEAM_ROLES = GLOBAL_WORKSPACE_ROLES
CLIENT_ADMIN_COMPAT_ROLES = {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}
BRANCH_SCOPED_ROLES = {UserRole.BRANCH_MANAGER, UserRole.OPERATOR}


def is_super_admin(current_user: User) -> bool:
    return current_user.role == UserRole.SUPER_ADMIN


def is_staff_admin(current_user: User) -> bool:
    return current_user.role == UserRole.STAFF_ADMIN


def is_staff_operator(current_user: User) -> bool:
    return current_user.role == UserRole.STAFF_OPERATOR


def is_global_workspace_user(current_user: User) -> bool:
    return current_user.role in GLOBAL_WORKSPACE_ROLES


def can_access_all_clients(current_user: User) -> bool:
    return is_global_workspace_user(current_user)


def can_manage_clients_directory(current_user: User) -> bool:
    return current_user.role in GLOBAL_CLIENT_DIRECTORY_ROLES


def can_manage_internal_team(current_user: User) -> bool:
    return is_super_admin(current_user)


def can_manage_client_users(current_user: User) -> bool:
    return can_manage_clients_directory(current_user) or current_user.role in {UserRole.CLIENT, UserRole.CLIENT_ADMIN}


def is_client_admin_like(current_user: User) -> bool:
    return current_user.role in CLIENT_ADMIN_COMPAT_ROLES


def is_client_operator(current_user: User) -> bool:
    return current_user.role == UserRole.CLIENT_OPERATOR


def is_branch_manager(current_user: User) -> bool:
    return current_user.role == UserRole.BRANCH_MANAGER


def is_operator(current_user: User) -> bool:
    return current_user.role == UserRole.OPERATOR


def is_branch_scoped(current_user: User) -> bool:
    return current_user.role in BRANCH_SCOPED_ROLES


def can_write_client_scope(current_user: User) -> bool:
    return is_global_workspace_user(current_user) or is_client_admin_like(current_user)


def can_write_branch_scope(current_user: User) -> bool:
    return can_write_client_scope(current_user) or is_branch_manager(current_user)


def require_client_write_access(current_user: User) -> User:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client admin access required")
    return current_user


def require_branch_write_access(current_user: User) -> User:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch manager access required")
    return current_user


def require_branch_assignment(current_user: User) -> str:
    if not current_user.branch_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is not assigned to a branch")
    return current_user.branch_id


def resolve_client_id(current_user: User, requested_client_id: str | None) -> str | None:
    if can_access_all_clients(current_user):
        return requested_client_id

    if current_user.client_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is not assigned to a client")

    if requested_client_id and requested_client_id != current_user.client_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-tenant access denied")

    return current_user.client_id


def require_client_scope(current_user: User, requested_client_id: str | None) -> str:
    client_id = resolve_client_id(current_user, requested_client_id)
    if not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="client_id is required")
    return client_id


def assert_client_exists(db: Session, client_id: str) -> Client:
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def apply_client_filter(query: Select, model: type, current_user: User) -> Select:
    # This helper is used only by read/list/report paths. Keep authorization
    # separate from transitive relationship eligibility, including global reads.
    from app.services.tenant_queries import read_integrity_predicate
    if not query.get_execution_options().get("a9_read_integrity"):
        query = query.where(read_integrity_predicate(model))
    if not can_access_all_clients(current_user) and hasattr(model, "client_id"):
        return query.where(model.client_id == current_user.client_id)
    return query


def _read_scoped_resource(resource, db):
    """Detail/lazy-loader boundary, only inside an administrative read snapshot.

    Write planning keeps the complete historical graph so A6-A8 can reject it.
    """
    if db.info.get("a9_snapshot"):
        from app.services.tenant_queries import readable_query
        model = type(resource)
        if db.connection().scalar(readable_query(select(model.id), model, db=db).where(
                model.id == resource.id)) is None:
            raise HTTPException(status_code=404, detail="Resource not found")
    return resource


def get_branch_in_scope(db: Session, branch_id: str, current_user: User) -> Branch:
    branch = db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    require_client_scope(current_user, branch.client_id)
    if is_branch_scoped(current_user) and branch.id != require_branch_assignment(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch access denied")

    return _read_scoped_resource(branch, db)


def get_channel_in_scope(db: Session, channel_id: str, current_user: User) -> Channel:
    channel = db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    require_client_scope(current_user, channel.client_id)
    if is_branch_scoped(current_user) and channel.branch_id != require_branch_assignment(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Channel access denied")

    return _read_scoped_resource(channel, db)


def get_device_output_in_scope(db: Session, device_output_id: str, current_user: User) -> DeviceOutput:
    device_output = db.get(DeviceOutput, device_output_id)
    if not device_output:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device output not found")

    root_channel = db.get(Channel, device_output.root_channel_id)
    if not root_channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Root channel not found")

    require_client_scope(current_user, root_channel.client_id)
    if is_branch_scoped(current_user) and root_channel.branch_id != require_branch_assignment(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device output access denied")

    return _read_scoped_resource(device_output, db)


def get_data_source_in_scope(db: Session, data_source_id: str, current_user: User) -> DataSource:
    data_source = db.get(DataSource, data_source_id)
    if not data_source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")

    require_client_scope(current_user, data_source.client_id)
    return _read_scoped_resource(data_source, db)


def get_content_folder_in_scope(db: Session, folder_id: str, current_user: User) -> ContentFolder:
    folder = db.get(ContentFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content folder not found")

    require_client_scope(current_user, folder.client_id)
    return _read_scoped_resource(folder, db)


def get_content_in_scope(db: Session, content_id: str, current_user: User) -> ContentItem:
    content = db.get(ContentItem, content_id)
    if not content:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    require_client_scope(current_user, content.client_id)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        visible_content = db.scalar(
            select(ContentItem.id).where(
                ContentItem.id == content_id,
                ContentItem.id.in_(branch_content_ids_query(branch_id)),
            )
        )
        if not visible_content:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    return _read_scoped_resource(content, db)


def get_dataset_in_scope(db: Session, dataset_id: str, current_user: User) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")

    require_client_scope(current_user, dataset.client_id)
    return _read_scoped_resource(dataset, db)


def get_layout_in_scope(db: Session, layout_id: str, current_user: User) -> Layout:
    layout = db.get(Layout, layout_id)
    if not layout:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout not found")

    require_client_scope(current_user, layout.client_id)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        visible_layout = db.scalar(
            select(Layout.id).where(
                Layout.id == layout_id,
                Layout.id.in_(branch_layout_ids_query(branch_id)),
            )
        )
        if not visible_layout:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout not found")

    return _read_scoped_resource(layout, db)


def get_layout_binding_in_scope(db: Session, binding_id: str, current_user: User) -> LayoutDataBinding:
    binding = db.get(LayoutDataBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    get_layout_in_scope(db, binding.layout_id, current_user)
    return _read_scoped_resource(binding, db)


def get_touch_experience_in_scope(db: Session, experience_id: str, current_user: User) -> TouchExperience:
    experience = db.get(TouchExperience, experience_id)
    if not experience:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch experience not found")

    require_client_scope(current_user, experience.client_id)
    return _read_scoped_resource(experience, db)


def branch_channel_ids_query(branch_id: str):
    return (select(Channel.id).join(Branch, Branch.id == Channel.branch_id)
            .where(Branch.id == branch_id, Channel.client_id == Branch.client_id))


def branch_schedule_scope_filter(branch_id: str):
    return and_(
        schedule_read_integrity(),
        Schedule.client_id == select(Branch.client_id).where(Branch.id == branch_id).scalar_subquery(),
        or_(
            and_(Schedule.channel_id.is_(None), Schedule.branch_id == branch_id),
            and_(Schedule.channel_id.in_(branch_channel_ids_query(branch_id)),
                 or_(Schedule.branch_id.is_(None), Schedule.branch_id == branch_id)),
        ),
    )


def branch_campaign_ids_query(branch_id: str):
    scheduled_campaigns = select(Schedule.campaign_id).where(branch_schedule_scope_filter(branch_id))
    assigned_campaigns = (
        select(CampaignChannelAssignment.campaign_id)
        .join(Channel, CampaignChannelAssignment.channel_id == Channel.id)
        .join(Campaign, Campaign.id == CampaignChannelAssignment.campaign_id)
        .where(Channel.id.in_(branch_channel_ids_query(branch_id)),
               Campaign.client_id == Channel.client_id)
    )
    return scheduled_campaigns.union(assigned_campaigns)


def branch_layout_ids_query(branch_id: str):
    campaign_layouts = (
        select(Campaign.layout_id).join(Layout, Layout.id == Campaign.layout_id)
        .where(
            Campaign.id.in_(branch_campaign_ids_query(branch_id)),
            Layout.client_id == Campaign.client_id,
            Campaign.layout_id.is_not(None),
        )
    )
    scheduled_layouts = (
        select(Schedule.layout_id)
        .where(
            branch_schedule_scope_filter(branch_id),
            Schedule.layout_id.is_not(None),
        )
    )
    return campaign_layouts.union(scheduled_layouts)


def branch_content_ids_query(branch_id: str):
    return (select(CampaignPlaylistItem.content_id)
            .join(Campaign, Campaign.id == CampaignPlaylistItem.campaign_id)
            .join(ContentItem, ContentItem.id == CampaignPlaylistItem.content_id)
            .where(Campaign.id.in_(branch_campaign_ids_query(branch_id)),
                   ContentItem.client_id == Campaign.client_id))


def branch_videowall_ids_query(branch_id: str):
    node_scoped_videowalls = (
        select(VideowallNode.videowall_id)
        .join(Channel, VideowallNode.channel_id == Channel.id)
        .join(Videowall, Videowall.id == VideowallNode.videowall_id)
        .where(Channel.id.in_(branch_channel_ids_query(branch_id)), Videowall.client_id == Channel.client_id)
    )
    hardware_scoped_videowalls = (
        select(Videowall.id)
        .join(Channel, Videowall.primary_channel_id == Channel.id)
        .where(Channel.id.in_(branch_channel_ids_query(branch_id)), Videowall.client_id == Channel.client_id)
    )
    return node_scoped_videowalls.union(hardware_scoped_videowalls)
