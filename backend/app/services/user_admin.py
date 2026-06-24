from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Client, User, UserRole
from app.schemas.auth import UserSession
from app.schemas.domain import UserRead
from app.services.tenancy import (
    can_access_all_clients,
    can_manage_client_users,
    can_manage_clients_directory,
    can_manage_internal_team,
    can_write_client_scope,
    is_client_admin_like,
    is_client_operator,
    is_global_workspace_user,
    is_staff_admin,
    is_staff_operator,
    is_super_admin,
)


ACTIVE_USER_STATUS = "active"
SUSPENDED_USER_STATUS = "suspended"
CLIENT_USER_ROLES = {
    UserRole.CLIENT,
    UserRole.CLIENT_ADMIN,
    UserRole.CLIENT_OPERATOR,
    UserRole.BRANCH_MANAGER,
    UserRole.OPERATOR,
}
VISIBLE_CLIENT_USER_ROLES = {
    UserRole.CLIENT,
    UserRole.CLIENT_ADMIN,
    UserRole.CLIENT_OPERATOR,
    UserRole.BRANCH_MANAGER,
    UserRole.OPERATOR,
}
TEAM_USER_ROLES = {
    UserRole.SUPER_ADMIN,
    UserRole.STAFF_ADMIN,
    UserRole.STAFF_OPERATOR,
}


def normalize_user_status(raw_status: str | None, *, fallback_active: bool = True) -> str:
    normalized = (raw_status or "").strip().lower()
    if normalized == SUSPENDED_USER_STATUS:
        return SUSPENDED_USER_STATUS
    if normalized == ACTIVE_USER_STATUS:
        return ACTIVE_USER_STATUS
    return ACTIVE_USER_STATUS if fallback_active else SUSPENDED_USER_STATUS


def is_user_active_status(user: User) -> bool:
    return normalize_user_status(user.status, fallback_active=bool(user.is_active)) == ACTIVE_USER_STATUS and bool(user.is_active)


def apply_user_status(user: User, next_status: str) -> None:
    normalized = normalize_user_status(next_status)
    user.status = normalized
    user.is_active = normalized == ACTIVE_USER_STATUS


def build_user_permissions(user: User) -> list[str]:
    permissions: list[str] = []

    if can_access_all_clients(user):
        permissions.extend(["clients.read_all", "workspace.read_all"])

    if can_manage_clients_directory(user):
        permissions.extend(["clients.create", "clients.update", "clients.suspend"])

    if can_write_client_scope(user):
        permissions.extend(["workspace.write", "campaigns.write", "contents.write", "branches.write", "channels.write"])

    if can_manage_client_users(user):
        permissions.append("client_users.manage")

    if can_manage_internal_team(user):
        permissions.append("team.manage")

    if is_super_admin(user):
        permissions.append("clients.delete")

    if is_staff_admin(user):
        permissions.extend(["clients.manage_all", "workspace.manage_all"])

    if is_staff_operator(user):
        permissions.append("workspace.operate_all")

    if is_client_admin_like(user):
        permissions.append("workspace.manage_own_client")

    if is_client_operator(user):
        permissions.append("workspace.operate_own_client")

    return sorted(set(permissions))


def build_user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        created_at=user.created_at,
        updated_at=user.updated_at,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        client_id=user.client_id,
        branch_id=user.branch_id,
        is_active=bool(user.is_active),
        status=normalize_user_status(user.status, fallback_active=bool(user.is_active)),
        last_login_at=user.last_login_at,
    )


def build_user_session(user: User) -> UserSession:
    return UserSession(
        id=user.id,
        created_at=user.created_at,
        updated_at=user.updated_at,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        client_id=user.client_id,
        branch_id=user.branch_id,
        is_active=bool(user.is_active),
        status=normalize_user_status(user.status, fallback_active=bool(user.is_active)),
        last_login_at=user.last_login_at,
        permissions=build_user_permissions(user),
    )


def assert_user_session_allowed(user: User | None, db: Session) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not is_user_active_status(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta suspendida por administración")

    if user.client_id:
        client = db.get(Client, user.client_id)
        if not client or (client.status or "active").lower() == "suspended":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta suspendida por administración")

    return user


def stamp_last_login(user: User) -> None:
    user.last_login_at = datetime.now(UTC)


def list_super_admins(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.role == UserRole.SUPER_ADMIN).order_by(User.created_at.asc())))


def ensure_super_admin_survives(
    *,
    db: Session,
    target_user: User,
    next_role: UserRole | None = None,
    next_status: str | None = None,
    deleting: bool = False,
) -> None:
    is_current_super_admin = target_user.role == UserRole.SUPER_ADMIN
    will_stop_being_super_admin = is_current_super_admin and next_role is not None and next_role != UserRole.SUPER_ADMIN
    will_be_suspended = is_current_super_admin and next_status is not None and normalize_user_status(next_status) != ACTIVE_USER_STATUS
    if not (deleting or will_stop_being_super_admin or will_be_suspended):
        return

    super_admins = list_super_admins(db)
    active_super_admins = [
        user
        for user in super_admins
        if user.id != target_user.id and is_user_active_status(user)
    ]
    if active_super_admins:
        return

    if deleting:
        detail = "No se puede eliminar el ultimo super_admin"
    elif will_be_suspended:
        detail = "No se puede suspender el ultimo super_admin"
    else:
        detail = "No se puede degradar el ultimo super_admin"
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def ensure_client_user_role(role: UserRole) -> None:
    if role not in VISIBLE_CLIENT_USER_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rol de usuario cliente no valido")


def ensure_team_user_role(role: UserRole) -> None:
    if role not in TEAM_USER_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rol interno no valido")


def ensure_client_user_scope(user: User, *, client_id: str) -> None:
    if user.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El usuario no pertenece al cliente solicitado")


def ensure_internal_team_user(user: User) -> None:
    if user.role not in TEAM_USER_ROLES or user.client_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario interno no encontrado")


def ensure_client_scoped_user(user: User, *, client_id: str) -> None:
    if user.role not in CLIENT_USER_ROLES or user.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario del cliente no encontrado")
