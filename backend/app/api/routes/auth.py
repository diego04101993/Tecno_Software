from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import create_access_token, verify_password
from app.models.entities import User
from app.schemas.auth import LoginRequest, TokenResponse, UserSession
from app.services.user_admin import assert_user_session_allowed, build_user_session, stamp_last_login


router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    assert_user_session_allowed(user, db)
    stamp_last_login(user)
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
def me(current_user: User = Depends(get_current_user)) -> UserSession:
    return build_user_session(current_user)
