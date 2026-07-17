from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.auth.schemas import UserCreate, UserRead
from app.modules.auth.service import create_user, get_user_by_username_or_email
from app.modules.common import ModuleStatus

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status", response_model=ModuleStatus)
async def auth_status() -> ModuleStatus:
    return ModuleStatus(
        module="auth",
        responsibility="Handle registration, login, user profiles, and basic member/admin roles.",
        next_steps=[
            "Create database tables",
            "Add password hashing and token authentication",
            "Expose registration, login, and current-user APIs",
        ],
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register_user(user_in: UserCreate, db: Session = Depends(get_db)) -> UserRead:
    existing_user = get_user_by_username_or_email(db, user_in.username, str(user_in.email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already exists.",
        )

    return create_user(db, user_in)
