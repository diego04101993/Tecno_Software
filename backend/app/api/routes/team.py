from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import hash_password
from app.models.entities import User
from app.schemas.domain import UserCreate, UserRead, UserStatusUpdate, UserUpdate
from app.services.tenancy import can_manage_internal_team
from app.services.user_admin import (
    ACTIVE_USER_STATUS,
    TEAM_USER_ROLES,
    apply_user_status,
    build_user_read,
    ensure_internal_team_user,
    ensure_super_admin_survives,
    ensure_team_user_role,
)


from app.services.tenant_mutations import delete_guard, write_guard, MutationIntent
from app.services.tenant_ownership import RowRef


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()


def _require_team_access(current_user: User) -> None:
    if not can_manage_internal_team(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only super admin can manage the internal team")


def _assert_unique_user_email(db: Session, email: str, *, exclude_user_id: str | None = None) -> None:
    query = select(User.id).where(User.email == email.lower())
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    if db.scalar(query.limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")


def _get_team_user_or_404(user_id: str, db: Session) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    ensure_internal_team_user(user)
    return user


@router.get("/users", response_model=list[UserRead])
@read_snapshot(response_model=list[UserRead])
def list_team_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserRead]:
    _require_team_access(current_user)
    users = list(db.scalars(readable_query(select(User), User, db=db).where(User.role.in_(TEAM_USER_ROLES)).order_by(User.created_at.desc())))
    return [build_user_read(user) for user in users]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_team_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    _require_team_access(current_user)
    ensure_team_user_role(payload.role)
    _assert_unique_user_email(db, payload.email.lower())

    values = dict(
        email=payload.email.lower(),
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        client_id=None,
        branch_id=None,
        status=payload.status,
        is_active=payload.status == ACTIVE_USER_STATUS,
    )
    ref = RowRef("User", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=None, operation="create_team_user", intents=(intent,)):
        _require_team_access(current_user)
        _assert_unique_user_email(db, payload.email.lower())
        user = User(id=ref.identity, **values)
        db.add(user)
        db.commit()
        db.refresh(user)
        return build_user_read(user)


@router.patch("/users/{user_id}", response_model=UserRead)
def update_team_user(
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    _require_team_access(current_user)
    user = _get_team_user_or_404(user_id, db)

    proposed = {}
    if payload.role is not None:
        ensure_team_user_role(payload.role)
        proposed["role"] = payload.role
    if payload.email is not None:
        email = payload.email.lower()
        _assert_unique_user_email(db, email, exclude_user_id=user.id)
        proposed["email"] = email
    if payload.full_name is not None:
        proposed["full_name"] = payload.full_name.strip()
    if payload.password:
        proposed["password_hash"] = hash_password(payload.password)

    intent = MutationIntent("UPDATE", RowRef("User", user.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=None, operation="update_team_user", intents=(intent,)):
        _require_team_access(current_user)
        ensure_internal_team_user(user)
        next_role = proposed.get("role", user.role)
        ensure_super_admin_survives(db=db, target_user=user, next_role=next_role)
        if "email" in proposed:
            _assert_unique_user_email(db, proposed["email"], exclude_user_id=user.id)
            user.email = proposed["email"]
        if "full_name" in proposed:
            user.full_name = proposed["full_name"]
        if "password_hash" in proposed:
            user.password_hash = proposed["password_hash"]
        if "role" in proposed:
            user.role = next_role
        db.add(user)
        db.commit()
        db.refresh(user)
        return build_user_read(user)


@router.patch("/users/{user_id}/status", response_model=UserRead)
def update_team_user_status(
    user_id: str,
    payload: UserStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    _require_team_access(current_user)
    user = _get_team_user_or_404(user_id, db)
    proposed = {"status": payload.status, "is_active": payload.status == ACTIVE_USER_STATUS}
    intent = MutationIntent("UPDATE", RowRef("User", user.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=None, operation="update_team_user_status", intents=(intent,)):
        _require_team_access(current_user)
        ensure_internal_team_user(user)
        ensure_super_admin_survives(db=db, target_user=user, next_status=payload.status)
        apply_user_status(user, payload.status)
        db.add(user)
        db.commit()
        db.refresh(user)
        return build_user_read(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    _require_team_access(current_user)
    user = _get_team_user_or_404(user_id, db)
    with delete_guard(db, (user,), actor=current_user, operation="delete_team_user"):
        ensure_super_admin_survives(db=db, target_user=user, deleting=True)
        db.delete(user)
        db.commit()
