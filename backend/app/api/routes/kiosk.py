from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import KioskButton, KioskScreen, User
from app.schemas.domain import KioskButtonCreate, KioskButtonRead, KioskScreenCreate, KioskScreenRead
from app.services.tenancy import apply_client_filter, assert_client_exists, can_write_client_scope, is_branch_scoped, require_client_scope


router = APIRouter()


@router.get("/screens", response_model=list[KioskScreenRead])
def list_screens(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KioskScreen]:
    if is_branch_scoped(current_user):
        return []

    query = apply_client_filter(
        select(KioskScreen).where(KioskScreen.experience_id.is_(None)).order_by(KioskScreen.created_at.desc()),
        KioskScreen,
        current_user,
    )
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(KioskScreen.client_id == target_client_id)
    return list(db.scalars(query))


@router.get("/screens/{screen_id}/buttons", response_model=list[KioskButtonRead])
def list_buttons(
    screen_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KioskButton]:
    if is_branch_scoped(current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screen not found")

    screen = db.get(KioskScreen, screen_id)
    if not screen:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screen not found")
    require_client_scope(current_user, screen.client_id)
    return list(
        db.scalars(select(KioskButton).where(KioskButton.screen_id == screen_id).order_by(KioskButton.sort_order.asc()))
    )


@router.post("/screens", response_model=KioskScreenRead, status_code=status.HTTP_201_CREATED)
def create_screen(
    payload: KioskScreenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskScreen:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create kiosk screens")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    screen = KioskScreen(client_id=client_id, **payload.model_dump(exclude={"client_id"}))
    db.add(screen)
    db.commit()
    db.refresh(screen)
    return screen


@router.post("/screens/{screen_id}/buttons", response_model=KioskButtonRead, status_code=status.HTTP_201_CREATED)
def create_button(
    screen_id: str,
    payload: KioskButtonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskButton:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create kiosk buttons")

    screen = db.get(KioskScreen, screen_id)
    if not screen:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screen not found")
    require_client_scope(current_user, screen.client_id)

    button = KioskButton(screen_id=screen_id, **payload.model_dump())
    db.add(button)
    db.commit()
    db.refresh(button)
    return button
