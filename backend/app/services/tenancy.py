from fastapi import HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Branch, Campaign, CampaignChannelAssignment, CampaignPlaylistItem, Channel, Client, ContentItem, ContentFolder, DataSource, Dataset, Layout, LayoutDataBinding, Schedule, TouchExperience, User, UserRole, VideowallNode


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
    if not can_access_all_clients(current_user) and hasattr(model, "client_id"):
        return query.where(model.client_id == current_user.client_id)
    return query


def get_branch_in_scope(db: Session, branch_id: str, current_user: User) -> Branch:
    branch = db.get(Branch, branch_id)
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    require_client_scope(current_user, branch.client_id)
    if is_branch_scoped(current_user) and branch.id != require_branch_assignment(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch access denied")

    return branch


def get_channel_in_scope(db: Session, channel_id: str, current_user: User) -> Channel:
    channel = db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    require_client_scope(current_user, channel.client_id)
    if is_branch_scoped(current_user) and channel.branch_id != require_branch_assignment(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Channel access denied")

    return channel


def get_data_source_in_scope(db: Session, data_source_id: str, current_user: User) -> DataSource:
    data_source = db.get(DataSource, data_source_id)
    if not data_source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")

    require_client_scope(current_user, data_source.client_id)
    return data_source


def get_content_folder_in_scope(db: Session, folder_id: str, current_user: User) -> ContentFolder:
    folder = db.get(ContentFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content folder not found")

    require_client_scope(current_user, folder.client_id)
    return folder


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

    return content


def get_dataset_in_scope(db: Session, dataset_id: str, current_user: User) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")

    require_client_scope(current_user, dataset.client_id)
    return dataset


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

    return layout


def get_layout_binding_in_scope(db: Session, binding_id: str, current_user: User) -> LayoutDataBinding:
    binding = db.get(LayoutDataBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    get_layout_in_scope(db, binding.layout_id, current_user)
    return binding


def get_touch_experience_in_scope(db: Session, experience_id: str, current_user: User) -> TouchExperience:
    experience = db.get(TouchExperience, experience_id)
    if not experience:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch experience not found")

    require_client_scope(current_user, experience.client_id)
    return experience


def branch_channel_ids_query(branch_id: str):
    return select(Channel.id).where(Channel.branch_id == branch_id)


def branch_schedule_scope_filter(branch_id: str):
    return or_(
        Schedule.branch_id == branch_id,
        Schedule.channel_id.in_(branch_channel_ids_query(branch_id)),
    )


def branch_campaign_ids_query(branch_id: str):
    scheduled_campaigns = select(Schedule.campaign_id).where(branch_schedule_scope_filter(branch_id))
    assigned_campaigns = (
        select(CampaignChannelAssignment.campaign_id)
        .join(Channel, CampaignChannelAssignment.channel_id == Channel.id)
        .where(Channel.branch_id == branch_id)
    )
    return scheduled_campaigns.union(assigned_campaigns)


def branch_layout_ids_query(branch_id: str):
    campaign_layouts = (
        select(Campaign.layout_id)
        .where(
            Campaign.id.in_(branch_campaign_ids_query(branch_id)),
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
    return select(CampaignPlaylistItem.content_id).where(
        CampaignPlaylistItem.campaign_id.in_(branch_campaign_ids_query(branch_id))
    )


def branch_videowall_ids_query(branch_id: str):
    return (
        select(VideowallNode.videowall_id)
        .join(Channel, VideowallNode.channel_id == Channel.id)
        .where(Channel.branch_id == branch_id)
    )
