from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import hash_password
from app.models.entities import Branch, User, UserRole
from app.schemas.domain import UserCreate, UserRead
from app.services.tenancy import (
    apply_client_filter,
    can_access_all_clients,
    can_manage_client_users,
    can_manage_clients_directory,
    can_manage_internal_team,
    is_branch_scoped,
    is_super_admin,
    require_client_scope,
)
from app.services.user_admin import (
    ACTIVE_USER_STATUS,
    TEAM_USER_ROLES,
    build_user_read,
    ensure_client_user_role,
    ensure_team_user_role,
)


router = APIRouter()


def _assert_unique_user_email(db: Session, email: str) -> None:
    if db.scalar(select(User.id).where(User.email == email.lower()).limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")


def _validate_branch(role: UserRole, branch_id: str | None, client_id: str, db: Session) -> str | None:
    if role in {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}:
        return None
    if role not in {UserRole.BRANCH_MANAGER, UserRole.OPERATOR}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported user role")
    if not branch_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_id is required for branch-scoped users")
    branch = db.get(Branch, branch_id)
    if not branch or branch.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")
    return branch.id


@router.get("/", response_model=list[UserRead])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserRead]:
    if is_branch_scoped(current_user) or current_user.role in {UserRole.CLIENT_OPERATOR, UserRole.STAFF_OPERATOR}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view users")

    query = select(User).order_by(User.created_at.desc())
    if can_access_all_clients(current_user):
        if not is_super_admin(current_user):
            query = query.where(User.client_id.is_not(None))
    else:
        query = apply_client_filter(query, User, current_user)
    users = list(db.scalars(query))
    return [build_user_read(user) for user in users]


@router.post("/", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    email = payload.email.lower()
    _assert_unique_user_email(db, email)

    if payload.role in TEAM_USER_ROLES:
        if not can_manage_internal_team(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only super admin can create internal team users")
        ensure_team_user_role(payload.role)
        user = User(
            email=email,
            full_name=payload.full_name.strip(),
            password_hash=hash_password(payload.password),
            role=payload.role,
            client_id=None,
            branch_id=None,
            status=payload.status,
            is_active=payload.status == ACTIVE_USER_STATUS,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return build_user_read(user)

    if not can_manage_client_users(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create client users")

    client_id = require_client_scope(current_user, payload.client_id)
    ensure_client_user_role(payload.role)
    if not can_manage_clients_directory(current_user) and payload.role not in {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only global admins can create branch-scoped users")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        client_id=client_id,
        branch_id=_validate_branch(payload.role, payload.branch_id, client_id, db),
        status=payload.status,
        is_active=payload.status == ACTIVE_USER_STATUS,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_user_read(user)
