from collections.abc import Generator
import hashlib

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_access_token
from app.models.entities import Channel, PlayerDevice, User, UserRole
from app.services.tenancy import require_active_channel_tenant, validate_authenticated_tenant_state
from app.services.user_admin import assert_user_session_allowed


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    payload = decode_access_token(token)
    user = db.get(User, payload.get("sub"))

    return validate_authenticated_tenant_state(assert_user_session_allowed(user, db), db)


def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin access required")
    return current_user


def get_tenant_operation_context(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Opt-in identity context, not a replacement for endpoint role checks."""
    from app.services.tenancy import tenant_operation_context
    return tenant_operation_context(db, current_user)


def get_current_player_device(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> PlayerDevice:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing player bearer token")

    token = authorization.split(" ", 1)[1].strip()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    player_device = db.query(PlayerDevice).filter(PlayerDevice.player_token_hash == token_hash).one_or_none()

    if not player_device or not player_device.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid player token")

    channel = require_active_channel_tenant(db.get(Channel, player_device.channel_id), db)
    if channel.client_id != player_device.client_id or channel.branch_id != player_device.branch_id:
        raise HTTPException(status_code=403, detail="Invalid player scope")
    return player_device
