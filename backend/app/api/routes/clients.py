from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import hash_password
from app.models.entities import Branch, Client, User, UserRole
from app.schemas.domain import (
    ClientCreate,
    ClientDeleteRequest,
    ClientRead,
    ClientStatusUpdate,
    ClientUpdate,
    UserCreate,
    UserRead,
    UserStatusUpdate,
    UserUpdate,
)
from app.services.client_cleanup import delete_client_cascade
from app.services.tenancy import (
    can_access_all_clients,
    can_manage_client_users,
    can_manage_clients_directory,
    can_write_client_scope,
    is_super_admin,
    require_client_scope,
    resolve_client_id,
)
from app.services.user_admin import (
    ACTIVE_USER_STATUS,
    apply_user_status,
    build_user_read,
    ensure_client_scoped_user,
    ensure_client_user_role,
)


router = APIRouter()


def _get_client_or_404(client_id: str, db: Session, current_user: User) -> Client:
    scoped_client_id = resolve_client_id(current_user, client_id)
    target_id = scoped_client_id or client_id
    client = db.get(Client, target_id)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def _require_client_directory_access(current_user: User) -> None:
    if not can_manage_clients_directory(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to manage clients")


def _require_client_users_access(current_user: User, client_id: str) -> str:
    if not can_manage_client_users(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to manage client users")
    return require_client_scope(current_user, client_id)


def _validate_branch_for_user(role: UserRole, branch_id: str | None, client_id: str, db: Session) -> str | None:
    if role in {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}:
        return None

    if role not in {UserRole.BRANCH_MANAGER, UserRole.OPERATOR}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported client user role")

    if not branch_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_id is required for branch-scoped users")

    branch = db.get(Branch, branch_id)
    if not branch or branch.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found for this client")
    return branch.id


def _assert_unique_client_slug(db: Session, slug: str, *, exclude_client_id: str | None = None) -> None:
    query = select(Client.id).where(Client.slug == slug)
    if exclude_client_id:
        query = query.where(Client.id != exclude_client_id)
    if db.scalar(query.limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client slug already exists")


def _assert_unique_user_email(db: Session, email: str, *, exclude_user_id: str | None = None) -> None:
    query = select(User.id).where(User.email == email.lower())
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    if db.scalar(query.limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")


@router.get("/", response_model=list[ClientRead])
def list_clients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Client]:
    if not can_access_all_clients(current_user):
        client = db.get(Client, current_user.client_id)
        return [client] if client else []
    return list(db.scalars(select(Client).order_by(Client.created_at.desc())))


@router.post("/", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
def create_client(
    payload: ClientCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Client:
    _require_client_directory_access(current_user)
    _assert_unique_client_slug(db, payload.slug)

    client = Client(
        name=payload.name.strip(),
        slug=payload.slug.strip(),
        contact_email=payload.contact_email,
        brand_name=payload.brand_name.strip() if payload.brand_name else None,
        status="active",
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@router.get("/{client_id}/users", response_model=list[UserRead])
def list_client_users(
    client_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserRead]:
    scoped_client_id = _require_client_users_access(current_user, client_id)
    _get_client_or_404(scoped_client_id, db, current_user)
    users = list(
        db.scalars(
            select(User)
            .where(User.client_id == scoped_client_id)
            .order_by(User.created_at.desc())
        )
    )
    return [build_user_read(user) for user in users]


@router.post("/{client_id}/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_client_user(
    client_id: str,
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    scoped_client_id = _require_client_users_access(current_user, client_id)
    _get_client_or_404(scoped_client_id, db, current_user)

    role = payload.role
    ensure_client_user_role(role)
    if not can_manage_clients_directory(current_user) and role not in {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only global admins can create branch-scoped users")

    email = payload.email.lower()
    _assert_unique_user_email(db, email)
    branch_id = _validate_branch_for_user(role, payload.branch_id, scoped_client_id, db)

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=role,
        client_id=scoped_client_id,
        branch_id=branch_id,
        status=payload.status,
        is_active=payload.status == ACTIVE_USER_STATUS,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_user_read(user)


@router.patch("/{client_id}/users/{user_id}", response_model=UserRead)
def update_client_user(
    client_id: str,
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    scoped_client_id = _require_client_users_access(current_user, client_id)
    _get_client_or_404(scoped_client_id, db, current_user)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    ensure_client_scoped_user(user, client_id=scoped_client_id)

    next_role = payload.role or user.role
    ensure_client_user_role(next_role)
    if not can_manage_clients_directory(current_user) and next_role not in {UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.CLIENT_OPERATOR}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only global admins can assign branch-scoped roles")

    if payload.email is not None:
        email = payload.email.lower()
        _assert_unique_user_email(db, email, exclude_user_id=user.id)
        user.email = email
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.password:
        user.password_hash = hash_password(payload.password)

    user.role = next_role
    user.branch_id = _validate_branch_for_user(next_role, payload.branch_id if payload.branch_id is not None else user.branch_id, scoped_client_id, db)
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_user_read(user)


@router.patch("/{client_id}/users/{user_id}/status", response_model=UserRead)
def update_client_user_status(
    client_id: str,
    user_id: str,
    payload: UserStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    scoped_client_id = _require_client_users_access(current_user, client_id)
    _get_client_or_404(scoped_client_id, db, current_user)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    ensure_client_scoped_user(user, client_id=scoped_client_id)

    apply_user_status(user, payload.status)
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_user_read(user)


@router.delete("/{client_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client_user(
    client_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    scoped_client_id = _require_client_users_access(current_user, client_id)
    _get_client_or_404(scoped_client_id, db, current_user)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    ensure_client_scoped_user(user, client_id=scoped_client_id)
    db.delete(user)
    db.commit()


@router.get("/{client_id}", response_model=ClientRead)
def get_client(
    client_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Client:
    return _get_client_or_404(client_id, db, current_user)


@router.patch("/{client_id}", response_model=ClientRead)
def update_client(
    client_id: str,
    payload: ClientUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Client:
    _require_client_directory_access(current_user)
    client = _get_client_or_404(client_id, db, current_user)

    if payload.slug is not None:
        _assert_unique_client_slug(db, payload.slug.strip(), exclude_client_id=client.id)
        client.slug = payload.slug.strip()
    if payload.name is not None:
        client.name = payload.name.strip()
    if payload.contact_email is not None:
        client.contact_email = payload.contact_email
    if payload.brand_name is not None:
        client.brand_name = payload.brand_name.strip() or None

    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@router.patch("/{client_id}/status", response_model=ClientRead)
def update_client_status(
    client_id: str,
    payload: ClientStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Client:
    _require_client_directory_access(current_user)
    client = _get_client_or_404(client_id, db, current_user)
    client.status = payload.status
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(
    client_id: str,
    payload: ClientDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not is_super_admin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only super admin can delete clients")
    if payload.confirm_text.strip().upper() != "ELIMINAR":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Type ELIMINAR to confirm client deletion")

    client = _get_client_or_404(client_id, db, current_user)
    delete_client_cascade(client, db)
