from app.services.tenant_ownership import RowRef
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import Branch, Channel, Client, KioskButton, KioskScreen, TouchExperience, TouchExperienceAssignment, TouchLocation, TouchMap, User
from app.schemas.domain import (
    KioskButtonCreate,
    KioskButtonRead,
    KioskScreenCreate,
    KioskScreenRead,
    TouchExperienceAssignmentCreate,
    TouchExperienceAssignmentRead,
    TouchExperienceAssignmentUpdate,
    TouchExperienceCreate,
    TouchExperienceRead,
    TouchExperienceUpdate,
    TouchLocationCreate,
    TouchLocationRead,
    TouchLocationUpdate,
    TouchMapCreate,
    TouchMapRead,
    TouchMapUpdate,
)
from app.services.tenancy import require_screen_target, safe_tenant_resource, apply_client_filter, assert_client_exists, can_write_client_scope, get_branch_in_scope, get_channel_in_scope, get_touch_experience_in_scope, require_client_scope
from app.services.tenancy import assert_related_clients, related_client_matches, optional_relation_id


from app.services.tenant_mutations import delete_guard, MutationIntent, write_guard


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()


def ensure_experience_screen(experience: TouchExperience, screen_id: str, db: Session) -> KioskScreen:
    screen = db.get(KioskScreen, screen_id)
    if not screen or screen.experience_id != experience.id or screen.client_id != experience.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch screen not found")
    return screen


def ensure_experience_button(screen: KioskScreen, button_id: str, db: Session) -> KioskButton:
    button = db.get(KioskButton, button_id)
    if not button or button.screen_id != screen.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch hotspot not found")
    return button


def ensure_experience_location(experience: TouchExperience, location_id: str, db: Session) -> TouchLocation:
    location = db.get(TouchLocation, location_id)
    if not location or location.experience_id != experience.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch location not found")
    return location


def ensure_experience_map(experience: TouchExperience, map_id: str, db: Session) -> TouchMap:
    touch_map = db.get(TouchMap, map_id)
    if not touch_map or touch_map.experience_id != experience.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch map not found")
    return touch_map


def ensure_experience_assignment(experience: TouchExperience, assignment_id: str, db: Session) -> TouchExperienceAssignment:
    assignment = db.get(TouchExperienceAssignment, assignment_id)
    if not assignment or assignment.experience_id != experience.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Touch assignment not found")
    assert_related_clients(experience.client_id, [assignment.client_id])
    return assignment


def preflight_screen_references(db: Session, client_id: str, screen_ids) -> None:
    # Buttons are owned by their source screen and (when present) its experience.
    # Check both parents: historical inconsistencies must not transfer ownership.
    with db.no_autoflush:
        parents = db.execute(select(KioskScreen.client_id, KioskScreen.experience_id, TouchExperience.client_id)
            .select_from(KioskButton).outerjoin(KioskScreen, KioskScreen.id == KioskButton.screen_id)
            .outerjoin(TouchExperience, TouchExperience.id == KioskScreen.experience_id)
            .where(KioskButton.target_screen_id.in_(screen_ids)))
        for screen_owner, experience_id, experience_owner in parents:
            assert_related_clients(client_id, [screen_owner])
            if experience_id is not None:
                assert_related_clients(client_id, [experience_owner])
        assert_related_clients(client_id, db.scalars(
            select(TouchExperience.client_id).where(or_(
                TouchExperience.home_screen_id.in_(screen_ids),
                TouchExperience.attract_screen_id.in_(screen_ids)))))


def preflight_experience_delete(experience: TouchExperience, db: Session) -> None:
    # Include indirect SET NULL effects of deleting this experience's screens.
    # Locations/maps have no separate owner: their sole parent is this experience.
    with db.no_autoflush:
        for model in (KioskScreen, TouchExperienceAssignment):
            assert_related_clients(experience.client_id, db.scalars(
                select(model.client_id).where(model.experience_id == experience.id)))
        screen_ids = select(KioskScreen.id).where(KioskScreen.experience_id == experience.id)
        preflight_screen_references(db, experience.client_id, screen_ids)


def serialize_runtime_config(experience: TouchExperience, db: Session) -> dict:
    from app.services.media_references import read_payload_media_owned, MediaReadContext
    if not experience.is_active or not read_payload_media_owned(db, experience.client_id, experience.metadata_json,
                                                             context=MediaReadContext.STRUCTURED):
        return {"experience": None, "screens": [], "locations": [], "maps": [], "assignments": []}
    screens = list(
        db.scalars(
            readable_query(select(KioskScreen), KioskScreen, db=db)
            .where(KioskScreen.experience_id == experience.id, KioskScreen.client_id == experience.client_id)
            .order_by(KioskScreen.sort_order.asc(), KioskScreen.created_at.asc())
        )
    )
    screen_ids = [screen.id for screen in screens]
    buttons = []
    if screen_ids:
        buttons = list(
            db.scalars(
                readable_query(select(KioskButton), KioskButton, db=db)
                .where(KioskButton.screen_id.in_(screen_ids))
                .order_by(KioskButton.sort_order.asc(), KioskButton.created_at.asc())
            )
        )
    locations = list(
        db.scalars(
            readable_query(select(TouchLocation), TouchLocation, db=db)
            .where(TouchLocation.experience_id == experience.id, TouchLocation.is_active.is_(True))
            .order_by(TouchLocation.name.asc())
        )
    )
    maps = list(
        db.scalars(
            readable_query(select(TouchMap), TouchMap, db=db)
            .where(TouchMap.experience_id == experience.id, TouchMap.is_active.is_(True))
            .order_by(TouchMap.name.asc())
        )
    )
    assignments = list(
        db.scalars(
            readable_query(select(TouchExperienceAssignment), TouchExperienceAssignment, db=db)
            .where(TouchExperienceAssignment.experience_id == experience.id, TouchExperienceAssignment.client_id == experience.client_id, TouchExperienceAssignment.is_active.is_(True))
            .order_by(TouchExperienceAssignment.sort_order.asc(), TouchExperienceAssignment.created_at.asc())
        )
    )

    screens_payload = []
    for screen in screens:
        screen_buttons = [button for button in buttons if button.screen_id == screen.id and (not button.target_screen_id or button.target_screen_id in screen_ids)]
        screens_payload.append(
            {
                "id": screen.id,
                "name": screen.name,
                "slug": screen.slug,
                "background_url": screen.background_url,
                "attract_media_url": screen.attract_media_url,
                "screen_kind": screen.screen_kind,
                "sort_order": screen.sort_order,
                "inactivity_timeout_seconds": screen.inactivity_timeout_seconds,
                "idle_timeout_override": screen.idle_timeout_override,
                "is_attract_screen": screen.is_attract_screen,
                "metadata_json": screen.metadata_json,
                "buttons": [
                    {
                        "id": button.id,
                        "label": button.label,
                        "x": button.x,
                        "y": button.y,
                        "width": button.width,
                        "height": button.height,
                        "action_type": button.action_type.value,
                        "action_value": button.action_value,
                        "target_screen_id": button.target_screen_id,
                        "sort_order": button.sort_order,
                        "style_json": button.style_json,
                        "action_payload_json": button.action_payload_json,
                        "is_hotspot": button.is_hotspot,
                    }
                    for button in screen_buttons
                ],
            }
        )

    return {
        "experience": {
            "id": experience.id,
            "name": experience.name,
            "slug": experience.slug,
            "description": experience.description,
            "default_idle_timeout_seconds": experience.default_idle_timeout_seconds,
            "attract_screen_id": experience.attract_screen_id if experience.attract_screen_id in screen_ids else None,
            "home_screen_id": experience.home_screen_id if experience.home_screen_id in screen_ids else None,
            "is_active": experience.is_active,
            "metadata_json": experience.metadata_json,
        },
        "screens": screens_payload,
        "locations": [
            {
                "id": location.id,
                "name": location.name,
                "category": location.category,
                "description": location.description,
                "floor_zone": location.floor_zone,
                "suite": location.suite,
                "image_url": location.image_url,
                "metadata_json": location.metadata_json,
                "is_active": location.is_active,
            }
            for location in locations
        ],
        "maps": [
            {
                "id": touch_map.id,
                "name": touch_map.name,
                "floor_zone": touch_map.floor_zone,
                "background_url": touch_map.background_url,
                "overlay_url": touch_map.overlay_url,
                "metadata_json": touch_map.metadata_json,
                "is_active": touch_map.is_active,
            }
            for touch_map in maps
        ],
        "assignments": [
            {
                "id": assignment.id,
                "branch_id": assignment.branch_id,
                "channel_id": assignment.channel_id,
                "sort_order": assignment.sort_order,
                "is_active": assignment.is_active,
                "metadata_json": assignment.metadata_json,
            }
            for assignment in assignments
            if (not assignment.channel_id or safe_tenant_resource(db, Channel, assignment.channel_id, experience.client_id))
            and (not assignment.branch_id or safe_tenant_resource(db, Branch, assignment.branch_id, experience.client_id))
        ],
        "player_ready": False,
        "touch_runtime_version": 1,
    }


def safe_experience_read(experience: TouchExperience, db: Session) -> TouchExperienceRead:
    updates = {field: optional_relation_id(db, KioskScreen, getattr(experience, field),
                    experience.client_id, experience_id=experience.id)
               for field in ("home_screen_id", "attract_screen_id")}
    return TouchExperienceRead.model_validate(experience).model_copy(deep=True, update=updates)


def preflight_experience_update(experience: TouchExperience, changes: dict, db: Session) -> None:
    with db.no_autoflush:
        for field in ("home_screen_id", "attract_screen_id"):
            current_id = getattr(experience, field)
            proposed_id = changes.get(field, current_id)
            for target_id in (current_id, proposed_id):
                if target_id and optional_relation_id(db, KioskScreen, target_id,
                        experience.client_id, experience_id=experience.id) is None:
                    assert_related_clients(experience.client_id, [None])


@router.get("/experiences", response_model=list[TouchExperienceRead])
@read_snapshot(response_model=list[TouchExperienceRead])
def list_touch_experiences(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TouchExperience]:
    query = apply_client_filter(readable_query(select(TouchExperience), TouchExperience, db=db).order_by(TouchExperience.created_at.desc()), TouchExperience, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(TouchExperience.client_id == target_client_id)
    return [safe_experience_read(experience, db) for experience in db.scalars(query)]


@router.post("/experiences", response_model=TouchExperienceRead, status_code=status.HTTP_201_CREATED)
def create_touch_experience(
    payload: TouchExperienceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchExperience:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch experiences")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    existing = db.scalar(select(TouchExperience).where(TouchExperience.client_id == client_id, TouchExperience.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A touch experience with this slug already exists for the client")
    if payload.attract_screen_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attract screen must be defined after creating the experience")
    if payload.home_screen_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Home screen must be defined after creating the experience")

    values = dict(
        client_id=client_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        attract_screen_id=payload.attract_screen_id,
        home_screen_id=payload.home_screen_id,
        default_idle_timeout_seconds=payload.default_idle_timeout_seconds,
        is_active=payload.is_active,
        metadata_json=payload.metadata_json,
    )
    ref = RowRef("TouchExperience", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_touch_experience", intents=(intent,)):
        experience = TouchExperience(id=ref.identity, **values)
        db.add(experience)
        db.commit()
        db.refresh(experience)
        return experience


@router.get("/experiences/{experience_id}", response_model=TouchExperienceRead)
@read_snapshot(response_model=TouchExperienceRead)
def get_touch_experience(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchExperience:
    return safe_experience_read(get_touch_experience_in_scope(db, experience_id, current_user), db)


@router.patch("/experiences/{experience_id}", response_model=TouchExperienceRead)
def update_touch_experience(
    experience_id: str,
    payload: TouchExperienceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchExperience:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch experiences")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    changes = payload.model_dump(exclude_unset=True)
    preflight_experience_update(experience, changes, db)
    if "slug" in changes and changes["slug"] != experience.slug:
        existing = db.scalar(
            select(TouchExperience).where(
                TouchExperience.client_id == experience.client_id,
                TouchExperience.slug == changes["slug"],
                TouchExperience.id != experience.id,
            )
        )
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A touch experience with this slug already exists for the client")

    proposed = dict(changes)
    intent = MutationIntent("UPDATE", RowRef("TouchExperience", experience.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_experience", intents=(intent,)):
        for field, value in proposed.items():
            setattr(experience, field, value)
        db.add(experience)
        db.commit()
        db.refresh(experience)
        return experience


@router.delete("/experiences/{experience_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_experience(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch experiences")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    with delete_guard(db, (experience,), actor=current_user, operation="delete_touch_experience"):
        preflight_experience_delete(experience, db)
        experience.attract_screen_id = None
        experience.home_screen_id = None
        db.add(experience)
        db.flush()

        screens = list(
            db.scalars(
                select(KioskScreen)
                .where(KioskScreen.experience_id == experience.id, KioskScreen.client_id == experience.client_id)
                .order_by(KioskScreen.created_at.asc())
            )
        )
        for screen in screens:
            screen_buttons = list(db.scalars(select(KioskButton).where(KioskButton.screen_id == screen.id)))
            for button in screen_buttons:
                db.delete(button)
            db.delete(screen)

        for location in list(db.scalars(select(TouchLocation).where(TouchLocation.experience_id == experience.id))):
            db.delete(location)
        for touch_map in list(db.scalars(select(TouchMap).where(TouchMap.experience_id == experience.id))):
            db.delete(touch_map)
        for assignment in list(db.scalars(select(TouchExperienceAssignment).where(TouchExperienceAssignment.experience_id == experience.id))):
            db.delete(assignment)
        db.delete(experience)
        db.commit()


@router.get("/experiences/{experience_id}/screens", response_model=list[KioskScreenRead])
@read_snapshot(response_model=list[KioskScreenRead])
def list_touch_screens(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KioskScreen]:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    return list(
        db.scalars(
            readable_query(select(KioskScreen), KioskScreen, db=db)
            .where(KioskScreen.experience_id == experience.id, KioskScreen.client_id == experience.client_id)
            .order_by(KioskScreen.sort_order.asc(), KioskScreen.created_at.asc())
        )
    )


@router.post("/experiences/{experience_id}/screens", response_model=KioskScreenRead, status_code=status.HTTP_201_CREATED)
def create_touch_screen(
    experience_id: str,
    payload: KioskScreenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskScreen:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch screens")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    values = dict(
        client_id=experience.client_id,
        experience_id=experience.id,
        name=payload.name,
        slug=payload.slug,
        background_url=payload.background_url,
        attract_media_url=payload.attract_media_url,
        inactivity_timeout_seconds=payload.inactivity_timeout_seconds,
        is_attract_screen=payload.is_attract_screen,
        screen_kind=payload.screen_kind,
        sort_order=payload.sort_order,
        metadata_json=payload.metadata_json,
        idle_timeout_override=payload.idle_timeout_override,
    )
    ref = RowRef("KioskScreen", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="create_touch_screen", intents=(intent,)):
        screen = KioskScreen(id=ref.identity, **values)
        db.add(screen)
        db.commit()
        db.refresh(screen)
        return screen


@router.patch("/experiences/{experience_id}/screens/{screen_id}", response_model=KioskScreenRead)
def update_touch_screen(
    experience_id: str,
    screen_id: str,
    payload: KioskScreenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskScreen:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch screens")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    proposed = {'name': payload.name, 'slug': payload.slug, 'background_url': payload.background_url, 'attract_media_url': payload.attract_media_url, 'inactivity_timeout_seconds': payload.inactivity_timeout_seconds, 'is_attract_screen': payload.is_attract_screen, 'screen_kind': payload.screen_kind, 'sort_order': payload.sort_order, 'metadata_json': payload.metadata_json, 'idle_timeout_override': payload.idle_timeout_override}
    intent = MutationIntent("UPDATE", RowRef("KioskScreen", screen.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_screen", intents=(intent,)):
        screen.name = payload.name
        screen.slug = payload.slug
        screen.background_url = payload.background_url
        screen.attract_media_url = payload.attract_media_url
        screen.inactivity_timeout_seconds = payload.inactivity_timeout_seconds
        screen.is_attract_screen = payload.is_attract_screen
        screen.screen_kind = payload.screen_kind
        screen.sort_order = payload.sort_order
        screen.metadata_json = payload.metadata_json
        screen.idle_timeout_override = payload.idle_timeout_override
        db.add(screen)
        db.commit()
        db.refresh(screen)
        return screen


@router.delete("/experiences/{experience_id}/screens/{screen_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_screen(
    experience_id: str,
    screen_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch screens")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    with delete_guard(db, (screen,), actor=current_user, operation="delete_touch_screen"):
        preflight_screen_references(db, experience.client_id, [screen.id])
        if experience.attract_screen_id == screen.id:
            experience.attract_screen_id = None
        if experience.home_screen_id == screen.id:
            experience.home_screen_id = None
        db.add(experience)
        db.flush()
        for button in list(db.scalars(select(KioskButton).where(KioskButton.screen_id == screen.id))):
            db.delete(button)
        db.delete(screen)
        db.commit()


@router.get("/experiences/{experience_id}/screens/{screen_id}/buttons", response_model=list[KioskButtonRead])
@read_snapshot(response_model=list[KioskButtonRead])
def list_touch_buttons(
    experience_id: str,
    screen_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KioskButton]:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    return list(
        db.scalars(
            readable_query(select(KioskButton), KioskButton, db=db)
            .where(KioskButton.screen_id == screen.id, or_(
                KioskButton.target_screen_id.is_(None),
                select(KioskScreen.id).where(KioskScreen.id == KioskButton.target_screen_id,
                    KioskScreen.client_id == experience.client_id,
                    KioskScreen.experience_id == experience.id).exists()))
            .order_by(KioskButton.sort_order.asc(), KioskButton.created_at.asc())
        )
    )


@router.post("/experiences/{experience_id}/screens/{screen_id}/buttons", response_model=KioskButtonRead, status_code=status.HTTP_201_CREATED)
def create_touch_button(
    experience_id: str,
    screen_id: str,
    payload: KioskButtonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskButton:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch hotspots")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    require_screen_target(db, screen, payload.target_screen_id)
    values = dict(screen_id=screen.id, **payload.model_dump())
    ref = RowRef("KioskButton", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="create_touch_button", intents=(intent,)):
        button = KioskButton(id=ref.identity, **values)
        db.add(button)
        db.commit()
        db.refresh(button)
        return button


@router.patch("/experiences/{experience_id}/screens/{screen_id}/buttons/{button_id}", response_model=KioskButtonRead)
def update_touch_button(
    experience_id: str,
    screen_id: str,
    button_id: str,
    payload: KioskButtonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KioskButton:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch hotspots")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    button = ensure_experience_button(screen, button_id, db)
    require_screen_target(db, screen, payload.target_screen_id)
    proposed = dict(payload.model_dump())
    intent = MutationIntent("UPDATE", RowRef("KioskButton", button.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_button", intents=(intent,)):
        for field, value in proposed.items():
            setattr(button, field, value)
        db.add(button)
        db.commit()
        db.refresh(button)
        return button


@router.delete("/experiences/{experience_id}/screens/{screen_id}/buttons/{button_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_button(
    experience_id: str,
    screen_id: str,
    button_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch hotspots")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    screen = ensure_experience_screen(experience, screen_id, db)
    button = ensure_experience_button(screen, button_id, db)
    with delete_guard(db, (button,), actor=current_user, operation="delete_touch_button"):
        db.delete(button)
        db.commit()


@router.get("/experiences/{experience_id}/locations", response_model=list[TouchLocationRead])
@read_snapshot(response_model=list[TouchLocationRead])
def list_touch_locations(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TouchLocation]:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    return list(
        db.scalars(
            readable_query(select(TouchLocation), TouchLocation, db=db)
            .where(TouchLocation.experience_id == experience.id)
            .order_by(TouchLocation.name.asc())
        )
    )


@router.post("/experiences/{experience_id}/locations", response_model=TouchLocationRead, status_code=status.HTTP_201_CREATED)
def create_touch_location(
    experience_id: str,
    payload: TouchLocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchLocation:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch locations")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    values = dict(experience_id=experience.id, **payload.model_dump())
    ref = RowRef("TouchLocation", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="create_touch_location", intents=(intent,)):
        location = TouchLocation(id=ref.identity, **values)
        db.add(location)
        db.commit()
        db.refresh(location)
        return location


@router.patch("/experiences/{experience_id}/locations/{location_id}", response_model=TouchLocationRead)
def update_touch_location(
    experience_id: str,
    location_id: str,
    payload: TouchLocationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchLocation:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch locations")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    location = ensure_experience_location(experience, location_id, db)
    proposed = dict(payload.model_dump(exclude_unset=True))
    intent = MutationIntent("UPDATE", RowRef("TouchLocation", location.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_location", intents=(intent,)):
        for field, value in proposed.items():
            setattr(location, field, value)
        db.add(location)
        db.commit()
        db.refresh(location)
        return location


@router.delete("/experiences/{experience_id}/locations/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_location(
    experience_id: str,
    location_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch locations")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    location = ensure_experience_location(experience, location_id, db)
    with delete_guard(db, (location,), actor=current_user, operation="delete_touch_location"):
        db.delete(location)
        db.commit()


@router.get("/experiences/{experience_id}/maps", response_model=list[TouchMapRead])
@read_snapshot(response_model=list[TouchMapRead])
def list_touch_maps(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TouchMap]:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    return list(
        db.scalars(
            readable_query(select(TouchMap), TouchMap, db=db)
            .where(TouchMap.experience_id == experience.id)
            .order_by(TouchMap.name.asc())
        )
    )


@router.post("/experiences/{experience_id}/maps", response_model=TouchMapRead, status_code=status.HTTP_201_CREATED)
def create_touch_map(
    experience_id: str,
    payload: TouchMapCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchMap:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch maps")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    values = dict(experience_id=experience.id, **payload.model_dump())
    ref = RowRef("TouchMap", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="create_touch_map", intents=(intent,)):
        touch_map = TouchMap(id=ref.identity, **values)
        db.add(touch_map)
        db.commit()
        db.refresh(touch_map)
        return touch_map


@router.patch("/experiences/{experience_id}/maps/{map_id}", response_model=TouchMapRead)
def update_touch_map(
    experience_id: str,
    map_id: str,
    payload: TouchMapUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchMap:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch maps")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    touch_map = ensure_experience_map(experience, map_id, db)
    proposed = dict(payload.model_dump(exclude_unset=True))
    intent = MutationIntent("UPDATE", RowRef("TouchMap", touch_map.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_map", intents=(intent,)):
        for field, value in proposed.items():
            setattr(touch_map, field, value)
        db.add(touch_map)
        db.commit()
        db.refresh(touch_map)
        return touch_map


@router.delete("/experiences/{experience_id}/maps/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_map(
    experience_id: str,
    map_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch maps")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    touch_map = ensure_experience_map(experience, map_id, db)
    with delete_guard(db, (touch_map,), actor=current_user, operation="delete_touch_map"):
        db.delete(touch_map)
        db.commit()


@router.get("/experiences/{experience_id}/assignments", response_model=list[TouchExperienceAssignmentRead])
@read_snapshot(response_model=list[TouchExperienceAssignmentRead])
def list_touch_assignments(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TouchExperienceAssignment]:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    return list(
        db.scalars(
            readable_query(select(TouchExperienceAssignment), TouchExperienceAssignment, db=db)
            .where(TouchExperienceAssignment.experience_id == experience.id,
                   TouchExperienceAssignment.client_id == experience.client_id,
                   related_client_matches(Branch, TouchExperienceAssignment.branch_id, experience.client_id, optional=True),
                   related_client_matches(Channel, TouchExperienceAssignment.channel_id, experience.client_id, optional=True))
            .order_by(TouchExperienceAssignment.sort_order.asc(), TouchExperienceAssignment.created_at.asc())
        )
    )


@router.post("/experiences/{experience_id}/assignments", response_model=TouchExperienceAssignmentRead, status_code=status.HTTP_201_CREATED)
def create_touch_assignment(
    experience_id: str,
    payload: TouchExperienceAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchExperienceAssignment:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create touch assignments")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    if payload.branch_id:
        branch = get_branch_in_scope(db, payload.branch_id, current_user)
        if branch.client_id != experience.client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    if payload.channel_id:
        channel = get_channel_in_scope(db, payload.channel_id, current_user)
        if channel.client_id != experience.client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    values = dict(
        client_id=experience.client_id,
        experience_id=experience.id,
        branch_id=payload.branch_id,
        channel_id=payload.channel_id,
        sort_order=payload.sort_order,
        is_active=payload.is_active,
        metadata_json=payload.metadata_json,
    )
    ref = RowRef("TouchExperienceAssignment", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="create_touch_assignment", intents=(intent,)):
        assignment = TouchExperienceAssignment(id=ref.identity, **values)
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment


@router.patch("/experiences/{experience_id}/assignments/{assignment_id}", response_model=TouchExperienceAssignmentRead)
def update_touch_assignment(
    experience_id: str,
    assignment_id: str,
    payload: TouchExperienceAssignmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TouchExperienceAssignment:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update touch assignments")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    assignment = ensure_experience_assignment(experience, assignment_id, db)

    if payload.branch_id:
        branch = get_branch_in_scope(db, payload.branch_id, current_user)
        if branch.client_id != experience.client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    if payload.channel_id:
        channel = get_channel_in_scope(db, payload.channel_id, current_user)
        if channel.client_id != experience.client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    proposed = dict(payload.model_dump(exclude_unset=True))
    intent = MutationIntent("UPDATE", RowRef("TouchExperienceAssignment", assignment.id), tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=experience.client_id, operation="update_touch_assignment", intents=(intent,)):
        for field, value in proposed.items():
            setattr(assignment, field, value)
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment


@router.delete("/experiences/{experience_id}/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_touch_assignment(
    experience_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete touch assignments")

    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    assignment = ensure_experience_assignment(experience, assignment_id, db)
    with delete_guard(db, (assignment,), actor=current_user, operation="delete_touch_assignment"):
        db.delete(assignment)
        db.commit()


@router.get("/experiences/{experience_id}/runtime-config")
@read_snapshot()
def get_touch_runtime_config(
    experience_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    experience = get_touch_experience_in_scope(db, experience_id, current_user)
    return serialize_runtime_config(experience, db)
