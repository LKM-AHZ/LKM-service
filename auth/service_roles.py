"""用户与角色的多对多分配（auth 域权威写面）。"""

import uuid

from sqlalchemy import or_, select, union, update

from auth import events
from auth.models import Profile, User, UserRole
from auth.repository import UserRoleRepository
from core.db.repository import DbSession
from core.err import BizError, CommonErr
from core.rbac_roles import KNOWN_ROLES, activated_roles, composite_role


async def list_user_roles(db: DbSession, user_id: uuid.UUID) -> tuple[str, ...]:
    row = (
        await db.execute(
            select(User.account_level, Profile.role)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == user_id)
        )
    ).first()
    if row is None:
        raise BizError(CommonErr.NOT_FOUND)
    return activated_roles(
        row[0], row[1] or "member", await UserRoleRepository(db).list_roles(user_id)
    )


async def list_users_for_role(
    db: DbSession, role_name: str, *, limit: int = 100, offset: int = 0
) -> list[uuid.UUID]:
    """角色成员审查：合并基础角色与显式 UA 分配。"""
    if role_name not in KNOWN_ROLES:
        raise BizError(CommonErr.INVALID_INPUT, "Unknown role")
    level, profile_role = role_name.split(":", 1)
    primary_match = Profile.role == profile_role
    if profile_role == "member":
        primary_match = or_(primary_match, Profile.user_id.is_(None))
    primary = (
        select(User.id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(User.account_level == level, primary_match)
    )
    assigned = (
        select(UserRole.user_id)
        .join(User, User.id == UserRole.user_id)
        .where(UserRole.role_name == role_name, User.account_level == level)
    )
    members = union(primary, assigned).subquery()
    stmt = select(members.c.id).order_by(members.c.id).limit(limit).offset(offset)
    return list((await db.scalars(stmt)).all())


async def set_user_role(
    db: DbSession, user_id: uuid.UUID, role_name: str, *, assigned: bool
) -> bool:
    """原子增删附加角色；变更后撤销现有会话，防角色集继续沿用。"""
    if role_name not in KNOWN_ROLES:
        raise BizError(CommonErr.INVALID_INPUT, "Unknown role")
    row = (
        await db.execute(
            select(User.account_level, Profile.role)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.id == user_id)
            .with_for_update(of=User)
        )
    ).first()
    if row is None:
        raise BizError(CommonErr.NOT_FOUND)
    account_level, primary = row[0], row[1] or "member"
    if not role_name.startswith(f"{account_level}:"):
        raise BizError(CommonErr.INVALID_INPUT, "Role account level mismatch")
    if role_name == composite_role(account_level, primary):
        if assigned:
            return False
        raise BizError(CommonErr.INVALID_INPUT, "Primary role cannot be revoked")

    repo = UserRoleRepository(db)
    changed = (
        await repo.add(user_id, role_name)
        if assigned
        else await repo.remove(user_id, role_name)
    )
    if changed:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(token_version=User.token_version + 1)
        )
        await events.notify_user_updated(user_id)
        await events.notify_audit_permission_change(
            user_id, f"{'assign' if assigned else 'revoke'}:{role_name}"
        )
    return changed
