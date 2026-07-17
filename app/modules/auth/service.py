from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.auth.models import User
from app.modules.auth.schemas import UserCreate
from app.modules.auth.security import hash_password


def get_user_by_username(db: Session, username: str) -> User | None:
    statement = select(User).where(User.username == username)
    return db.scalar(statement)


def get_user_by_email(db: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    return db.scalar(statement)


def get_user_by_username_or_email(db: Session, username: str, email: str) -> User | None:
    statement = select(User).where(or_(User.username == username, User.email == email))
    return db.scalar(statement)


def create_user(db: Session, user_in: UserCreate) -> User:
    user = User(
        username=user_in.username,
        email=str(user_in.email),
        hashed_password=hash_password(user_in.password),
        nickname=user_in.nickname,
        research_direction=user_in.research_direction,
        bio=user_in.bio,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
