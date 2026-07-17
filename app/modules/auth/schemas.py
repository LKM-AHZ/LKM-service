from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.modules.auth.models import UserRole


class UserBase(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    nickname: str | None = Field(default=None, max_length=50)
    research_direction: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=500)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRead(UserBase):
    id: int
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
