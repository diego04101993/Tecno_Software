from app.services.tenant_ownership import RowRef
from app.services.tenant_mutations import MutationIntent, write_guard
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import KioskButton, KioskScreen, TouchExperience, User
from app.schemas.domain import KioskButtonCreate, KioskButtonRead, KioskScreenCreate, KioskScreenRead
from app.services.tenancy import require_same_client_resource, require_screen_target, apply_client_filter, assert_client_exists, can_write_client_scope, is_branch_scoped, require_client_scope
from app.services.tenancy import optional_relation_id


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()


@router.get("/screens", response_model=list[KioskScreenRead])
@read_snapshot(response_model=list[KioskScreenRead])
def list_screens(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KioskScreen]:
    if is_branch_scoped(current_user):
        return []

    query = apply_client_filter(
        readable_query(select(KioskScreen), KioskScreen, db=db).where(KioskScreen.experience_id.is_(None)).order_by(KioskScreen.created_at.desc()),
        KioskScreen,
        current_user,
    )
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(KioskScreen.client_id == target_client_id)
    return list(db.scalars(query))


@router.get("/screens/{screen_id}/buttons", response_model=list[KioskButtonRead])
@read_snapshot(response_model=list[KioskButtonRead])
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
    return [safe_button_read(button, screen, db) for button in db.scalars(
        readable_query(select(KioskButton), KioskButton, db=db).where(KioskButton.screen_id == screen_id).order_by(KioskButton.sort_order.asc()))]


def safe_button_read(button: KioskButton, screen: KioskScreen, db: Session) -> KioskButtonRead:
    target_id = optional_relation_id(db, KioskScreen, button.target_screen_id,
                                     screen.client_id, experience_id=screen.experience_id)
    result = KioskButtonRead.model_validate(button)
    if button.target_screen_id and target_id is None:
        # Navigation aliases may repeat the rejected target; never keep them live.
        return result.model_copy(deep=True, update={"target_screen_id": None, "action_value": None, "action_payload_json": {}})
    return result


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
    require_same_client_resource(db, TouchExperience, payload.experience_id, client_id)
    values = dict(client_id=client_id, **payload.model_dump(exclude={"client_id"}))
    ref = RowRef("KioskScreen", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_screen", intents=(intent,)):
        screen = KioskScreen(id=ref.identity, **values)
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

    require_screen_target(db, screen, payload.target_screen_id)
    values = dict(screen_id=screen_id, **payload.model_dump())
    ref = RowRef("KioskButton", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=screen.client_id, operation="create_button", intents=(intent,)):
        button = KioskButton(id=ref.identity, **values)
        db.add(button)
        db.commit()
        db.refresh(button)
        return button
