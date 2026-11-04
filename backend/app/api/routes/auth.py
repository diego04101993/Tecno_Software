from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import create_access_token, verify_password
from app.models.entities import User
from app.schemas.auth import LoginRequest, TokenResponse, UserSession
from app.services.auth_rate_limit import failure_budget
from app.services.user_admin import assert_user_session_allowed, build_user_session
from app.services.tenant_mutations import MutationIntent, write_guard
from app.services.tenant_ownership import RowRef


from app.services.tenant_responses import read_snapshot

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    with failure_budget(request, "login", payload.email.lower()):
        return _login(payload, db)


def _login(payload: LoginRequest, db: Session) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    assert_user_session_allowed(user, db)
    login_time = datetime.now(UTC)
    intent = MutationIntent("UPDATE", RowRef("User", user.id), (("last_login_at", login_time),))
    with write_guard(db, actor=user, client_id=user.client_id, operation="login", intents=(intent,)):
        assert_user_session_allowed(user, db)
        user.last_login_at = login_time
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_access_token(
        subject=user.id,
        extra_claims={
            "role": user.role.value,
            "client_id": user.client_id,
            "branch_id": user.branch_id,
        },
    )
    return TokenResponse(access_token=token, user=build_user_session(user))


@router.get("/me", response_model=UserSession)
@read_snapshot(response_model=UserSession)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserSession:
    from app.services.tenant_responses import safe_model_read
    result = build_user_session(current_user)
    safe = safe_model_read(current_user, UserSession, db)
    return result.model_copy(deep=True, update={"branch_id": safe.branch_id})
