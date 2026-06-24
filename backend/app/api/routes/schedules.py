from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import Campaign, Channel, Layout, Schedule, User
from app.schemas.domain import ScheduleCreate, ScheduleRead
from app.services.tenancy import apply_client_filter, branch_channel_ids_query, branch_schedule_scope_filter, can_write_branch_scope, get_channel_in_scope, is_branch_scoped, require_branch_assignment, require_client_scope


router = APIRouter()


def normalize_schedule_days(days_of_week: list[int]) -> list[int]:
    normalized = sorted({int(day) for day in days_of_week if 1 <= int(day) <= 7})
    return normalized


def schedules_match(candidate: Schedule, payload: ScheduleCreate, *, client_id: str, branch_id: str | None) -> bool:
    return (
        candidate.client_id == client_id
        and candidate.campaign_id == payload.campaign_id
        and candidate.channel_id == payload.channel_id
        and candidate.branch_id == branch_id
        and candidate.layout_id == payload.layout_id
        and candidate.title.strip() == payload.title.strip()
        and candidate.recurrence == payload.recurrence
        and list(candidate.days_of_week or []) == list(payload.days_of_week or [])
        and candidate.starts_on == payload.starts_on
        and candidate.ends_on == payload.ends_on
        and candidate.start_time == payload.start_time
        and candidate.end_time == payload.end_time
        and candidate.is_active == payload.is_active
        and candidate.is_looping == payload.is_looping
        and (candidate.timezone or "America/Mexico_City") == (payload.timezone or "America/Mexico_City")
        and candidate.priority == payload.priority
    )


@router.get("/", response_model=list[ScheduleRead])
def list_schedules(
    client_id: str | None = Query(default=None),
    channel_id: str | None = Query(default=None),
    campaign_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Schedule]:
    query = apply_client_filter(select(Schedule).order_by(Schedule.created_at.desc()), Schedule, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Schedule.client_id == target_client_id)
    if channel_id:
        channel = get_channel_in_scope(db, channel_id, current_user)
        query = query.where(Schedule.channel_id == channel.id)
    if campaign_id:
        query = query.where(Schedule.campaign_id == campaign_id)
    if is_branch_scoped(current_user):
        branch_id = require_branch_assignment(current_user)
        query = query.where(
            or_(
                Schedule.branch_id == branch_id,
                Schedule.channel_id.in_(branch_channel_ids_query(branch_id)),
            )
        )
    return list(db.scalars(query))


@router.post("/", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduleCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Schedule:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create schedules")

    campaign = db.get(Campaign, payload.campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    client_id = require_client_scope(current_user, payload.client_id or campaign.client_id)
    if campaign.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Campaign does not belong to this client")

    if is_branch_scoped(current_user):
        assigned_branch_id = require_branch_assignment(current_user)
        if payload.branch_id and payload.branch_id != assigned_branch_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch access denied")

    if payload.channel_id:
        channel = get_channel_in_scope(db, payload.channel_id, current_user)
        if channel.client_id != client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
        if is_branch_scoped(current_user) and channel.branch_id != assigned_branch_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Channel access denied")

    if payload.layout_id:
        layout = db.get(Layout, payload.layout_id)
        if not layout or layout.client_id != client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout not found")

    if is_branch_scoped(current_user):
        if not payload.channel_id and not payload.branch_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Branch-scoped schedules require a channel_id or branch_id")
        if payload.branch_id and payload.branch_id != assigned_branch_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch access denied")

    payload.days_of_week = normalize_schedule_days(payload.days_of_week)
    resolved_branch_id = payload.branch_id or (assigned_branch_id if is_branch_scoped(current_user) and not payload.channel_id else None)

    existing_candidates = list(
        db.scalars(
            select(Schedule).where(
                Schedule.client_id == client_id,
                Schedule.campaign_id == payload.campaign_id,
                Schedule.channel_id == payload.channel_id,
                Schedule.branch_id == resolved_branch_id,
            )
        )
    )
    existing_schedule = next(
        (candidate for candidate in existing_candidates if schedules_match(candidate, payload, client_id=client_id, branch_id=resolved_branch_id)),
        None,
    )
    if existing_schedule:
        response.status_code = status.HTTP_200_OK
        return existing_schedule

    schedule = Schedule(
        client_id=client_id,
        campaign_id=payload.campaign_id,
        channel_id=payload.channel_id,
        branch_id=resolved_branch_id,
        layout_id=payload.layout_id,
        title=payload.title.strip(),
        recurrence=payload.recurrence,
        days_of_week=payload.days_of_week,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        start_time=payload.start_time,
        end_time=payload.end_time,
        is_active=payload.is_active,
        is_looping=payload.is_looping,
        timezone=payload.timezone,
        priority=payload.priority,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule
