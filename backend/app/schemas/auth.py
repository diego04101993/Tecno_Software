from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.entities import UserRole
from app.schemas.common import TimestampedModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserSession(TimestampedModel):
    email: EmailStr
    full_name: str
    role: UserRole
    client_id: str | None = None
    branch_id: str | None = None
    is_active: bool
    status: str
    last_login_at: datetime | None = None
    permissions: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserSession
