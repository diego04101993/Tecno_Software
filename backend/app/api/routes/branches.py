from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.routes.channels import cleanup_channel_delete_dependencies
from app.models.entities import AudioAssignment, Branch, Channel, Schedule, TouchExperienceAssignment, User, Videowall, VideowallNode
from app.schemas.domain import BranchCreate, BranchRead, BranchUpdate
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    can_write_client_scope,
    get_branch_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


from app.services.tenant_mutations import delete_guard, require_delete_coverage, write_guard, MutationIntent
from app.services.tenant_ownership import RowRef


router = APIRouter()


def raise_branch_conflict(exc: IntegrityError) -> None:
    detail = str(exc.orig).lower()
    if "uq_branch_client_code" in detail or ("branches.client_id" in detail and "branches.code" in detail):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe otra sucursal con ese codigo para este cliente") from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se pudo guardar la sucursal por un conflicto de datos") from exc


def cleanup_branch_delete_dependencies(db: Session, branch: Branch) -> None:
    require_delete_coverage(db, (branch,))
    channels = list(
        db.scalars(
            select(Channel)
            .where(Channel.branch_id == branch.id)
            .order_by(Channel.created_at.asc())
        )
    )
    channel_ids = [channel.id for channel in channels]
    videowall_ids = list(
        {
            videowall_id
            for videowall_id in db.scalars(
                select(VideowallNode.videowall_id)
                .join(Channel, VideowallNode.channel_id == Channel.id)
                .where(Channel.branch_id == branch.id)
            )
            if videowall_id
        }
    )

    # Requeries may consume only the immutable batch approved under FK locks.
    # Check the whole manual execution set before the first bulk DELETE.
    require_delete_coverage(db, (*channels, *(RowRef("Videowall", value) for value in videowall_ids)))

    db.execute(delete(Schedule).where(Schedule.branch_id == branch.id))
    db.execute(delete(AudioAssignment).where(AudioAssignment.branch_id == branch.id))
    db.execute(delete(TouchExperienceAssignment).where(TouchExperienceAssignment.branch_id == branch.id))

    for channel in channels:
        cleanup_channel_delete_dependencies(db, channel)
        db.delete(channel)

    if channel_ids:
        db.execute(delete(VideowallNode).where(VideowallNode.channel_id.in_(channel_ids)))
    if videowall_ids:
        db.execute(delete(Videowall).where(Videowall.id.in_(videowall_ids)))


@router.get("/", response_model=list[BranchRead])
def list_branches(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Branch]:
    query = apply_client_filter(select(Branch).order_by(Branch.created_at.desc()), Branch, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Branch.client_id == target_client_id)
    if is_branch_scoped(current_user):
        query = query.where(Branch.id == require_branch_assignment(current_user))
    return list(db.scalars(query))


@router.post("/", response_model=BranchRead, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Branch:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create branches")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    values = dict(
        client_id=client_id,
        name=payload.name,
        code=payload.code,
        address=payload.address,
        timezone=payload.timezone,
    )
    ref = RowRef("Branch", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_branch", intents=(intent,)):
        branch = Branch(id=ref.identity, **values)
        db.add(branch)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise_branch_conflict(exc)
        db.refresh(branch)
        return branch


@router.patch("/{branch_id}", response_model=BranchRead)
def update_branch(
    branch_id: str,
    payload: BranchUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Branch:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update branches")

    branch = get_branch_in_scope(db, branch_id, current_user)
    changes = payload.model_dump(exclude_unset=True)

    proposed = dict(changes)
    for field in ("name", "code"):
        if field in proposed:
            proposed[field] = proposed[field].strip()
    if "address" in proposed:
        proposed["address"] = proposed["address"].strip() if proposed["address"] else None
    if "timezone" in proposed:
        proposed["timezone"] = proposed["timezone"].strip() or branch.timezone
    intent = MutationIntent("UPDATE", RowRef("Branch", branch.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=branch.client_id, operation="update_branch", intents=(intent,)):
        if "name" in proposed:
            branch.name = proposed["name"]
        if "code" in proposed:
            branch.code = proposed["code"]
        if "address" in proposed:
            branch.address = proposed["address"]
        if "timezone" in proposed:
            branch.timezone = proposed["timezone"]
        db.add(branch)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise_branch_conflict(exc)
        db.refresh(branch)
        return branch


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_branch(
    branch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete branches")

    branch = get_branch_in_scope(db, branch_id, current_user)
    with delete_guard(db, (branch,), actor=current_user, branch_id=branch.id, operation="delete_branch"):
        cleanup_branch_delete_dependencies(db, branch)
        db.delete(branch)
        db.commit()
