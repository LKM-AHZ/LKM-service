"""好友关系服务：请求/接受/拒绝/取消、好友与亲密好友、别名、block/mute 联动。

``block``/``mute`` 仍以 ``user_relationships`` 为唯一事实源（feed/通知过滤依赖它），
好友请求与好友状态落在 ``friend_relationships``；列表读时把两者合并成 desktop 使用的
``status`` 阶梯：``-100`` 屏蔽、``-50`` 静音、``0`` 待接受、``100`` 好友、``200`` 亲密好友。

desktop 直接按 Stargate 契约解析响应（``SnRelationship``），故本模块负责把内部行 +
身份快照组装成兼容出参；账户字段只填解析必需的子集。
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass

from app.modules.interaction import service as interaction_service
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import (
    FriendRelationship,
    UserRelationship,
)
from app.modules.interaction.repository import (
    FriendRelationshipRepository,
    UserRelationshipRepository,
)
from app.modules.interaction.schemas import (
    FRIEND_STATUS_BLOCKED,
    FRIEND_STATUS_CLOSE_FRIEND,
    FRIEND_STATUS_FRIEND,
    FRIEND_STATUS_MUTED,
    FRIEND_STATUS_PENDING,
    StargateAccountOut,
    StargateAccountProfileOut,
    StargateAccountStatusOut,
    StargateFriendOverviewOut,
    StargateRelationshipOut,
)
from app.ws.manager import manager
from core.contracts import UserSnapshot
from core.db.base import now_iso
from core.db.repository import DbSession
from core.err import BizError, CommonErr
from core.ports.snapshot import get_user_snapshot, get_user_snapshot_batch

#: 待接受请求的有效期（与 desktop 的 requestExpiredIn 展示一致）。
_FRIEND_REQUEST_TTL = datetime.timedelta(days=7)

_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_DURATION_UNIT = {
    "m": datetime.timedelta(minutes=1),
    "h": datetime.timedelta(hours=1),
    "d": datetime.timedelta(days=1),
    "w": datetime.timedelta(weeks=1),
}


@dataclass(frozen=True)
class RelationshipView:
    """列表/请求行的中间视图（wire 层再补账户快照）。"""

    account_id: uuid.UUID
    related_id: uuid.UUID
    other_id: uuid.UUID
    status: int
    alias: str | None
    expires_at: datetime.datetime | None
    degrade_to_status: int | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


async def _ensure_target(db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """不能对自己操作，且目标用户必须存在。"""
    if user_id == target_id:
        raise BizError(InteractionErr.CANNOT_RELATE_SELF)
    if await get_user_snapshot(db, user_id=target_id) is None:
        raise BizError(InteractionErr.RELATION_TARGET_NOT_FOUND)


def _parse_expires_in(value: str | None) -> datetime.datetime | None:
    """``30m``/``1h``/``24h``/``7d`` → 绝对 UTC 时间；空值 = 永久。"""
    if value is None or not value.strip():
        return None
    match = _DURATION_RE.match(value.strip().lower())
    if match is None:
        raise BizError(CommonErr.BAD_REQUEST, "时长格式应为 30m/1h/24h/7d")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise BizError(CommonErr.BAD_REQUEST, "时长必须为正数")
    return now_iso() + _DURATION_UNIT[unit] * amount


def _effective_status(
    row: UserRelationship, base_status: int, now: datetime.datetime
) -> int | None:
    """block/mute 行的生效状态：未过期取原状态，过期取降级值（空 = 已失效）。"""
    if row.expires_at is not None and row.expires_at <= now:
        return row.degrade_to_status
    return base_status


def _other_id(row: FriendRelationship, me: uuid.UUID) -> uuid.UUID:
    return row.related_id if row.account_id == me else row.account_id


# ---------------------------------------------------------------------------
# 好友请求 / 好友状态
# ---------------------------------------------------------------------------


async def _is_blocked(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID
) -> bool:
    """任一方向存在**未过期**的 block。"""
    repo = UserRelationshipRepository(db)
    now = now_iso()
    for row in (
        await repo.get_relation(me, target_id, "block"),
        await repo.get_relation(target_id, me, "block"),
    ):
        if row is not None and _effective_status(
            row, FRIEND_STATUS_BLOCKED, now
        ) == FRIEND_STATUS_BLOCKED:
            return True
    return False


async def send_request(db: DbSession, me: uuid.UUID, target_id: uuid.UUID) -> None:
    """发送好友请求；若对方已发来请求则直接成为好友。"""
    await _ensure_target(db, me, target_id)
    if await _is_blocked(db, me, target_id):
        raise BizError(InteractionErr.BLOCKED)

    repo = FriendRelationshipRepository(db)
    pair = await repo.get_between(me, target_id)
    if pair is not None:
        if pair.status == FRIEND_STATUS_PENDING:
            if pair.account_id == target_id:
                # 反向请求：互相有意，直接成为好友。
                await repo.update(
                    pair,
                    status=FRIEND_STATUS_FRIEND,
                    expires_at=None,
                )
            else:
                # 重复发送：刷新请求有效期。
                await repo.update(pair, expires_at=now_iso() + _FRIEND_REQUEST_TTL)
        return

    await repo.create(
        account_id=me,
        related_id=target_id,
        status=FRIEND_STATUS_PENDING,
        expires_at=now_iso() + _FRIEND_REQUEST_TTL,
    )


async def accept_request(
    db: DbSession, me: uuid.UUID, requester_id: uuid.UUID
) -> None:
    """接受来自 ``requester_id`` 的请求；已是好友时幂等。"""
    await _ensure_target(db, me, requester_id)
    repo = FriendRelationshipRepository(db)
    row = await repo.get_pair(requester_id, me)
    if row is None:
        pair = await repo.get_between(me, requester_id)
        if pair is None or pair.status == FRIEND_STATUS_PENDING:
            raise BizError(InteractionErr.FRIEND_REQUEST_NOT_FOUND)
        return
    if row.status == FRIEND_STATUS_PENDING:
        await repo.update(row, status=FRIEND_STATUS_FRIEND, expires_at=None)


async def decline_request(
    db: DbSession, me: uuid.UUID, requester_id: uuid.UUID
) -> None:
    """拒绝来自 ``requester_id`` 的请求（幂等）。"""
    await FriendRelationshipRepository(db).delete_pending(requester_id, me)


async def cancel_request(db: DbSession, me: uuid.UUID, target_id: uuid.UUID) -> None:
    """撤回我发出的请求（幂等）。"""
    await FriendRelationshipRepository(db).delete_pending(me, target_id)


async def delete_relationship(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID
) -> None:
    """删除好友关系：双向好友行 + 我单向的 block/mute。"""
    await FriendRelationshipRepository(db).delete_between(me, target_id)
    rel = UserRelationshipRepository(db)
    await rel.remove(me, target_id, "block")
    await rel.remove(me, target_id, "mute")


async def update_status(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID, status: int
) -> None:
    """desktop 的通用状态入口（主要用于解除屏蔽 → 100）。"""
    if status == FRIEND_STATUS_FRIEND:
        rel = UserRelationshipRepository(db)
        await rel.remove(me, target_id, "block")
        await rel.remove(me, target_id, "mute")
        await _upsert_friend(db, me, target_id, FRIEND_STATUS_FRIEND)
    elif status == FRIEND_STATUS_CLOSE_FRIEND:
        await _upsert_friend(db, me, target_id, FRIEND_STATUS_CLOSE_FRIEND)
    elif status == FRIEND_STATUS_BLOCKED:
        await block(db, me, target_id, None, None)
    elif status == FRIEND_STATUS_MUTED:
        await mute(db, me, target_id, None)
    elif status == FRIEND_STATUS_PENDING:
        raise BizError(InteractionErr.INVALID_RELATIONSHIP_STATUS)
    else:
        raise BizError(InteractionErr.INVALID_RELATIONSHIP_STATUS)


async def _upsert_friend(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID, status: int
) -> None:
    await _ensure_target(db, me, target_id)
    repo = FriendRelationshipRepository(db)
    pair = await repo.get_between(me, target_id)
    if pair is None:
        await repo.create(
            account_id=me,
            related_id=target_id,
            status=status,
            expires_at=None,
        )
    else:
        await repo.update(pair, status=status, expires_at=None)


async def block(
    db: DbSession,
    me: uuid.UUID,
    target_id: uuid.UUID,
    expires_in: str | None,
    degrade_to: int | None,
) -> None:
    await interaction_service.block_user(
        db,
        me,
        target_id,
        expires_at=_parse_expires_in(expires_in),
        degrade_to_status=degrade_to,
    )


async def mute(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID, expires_in: str | None
) -> None:
    await interaction_service.mute_user(
        db, me, target_id, expires_at=_parse_expires_in(expires_in)
    )


async def unmute(db: DbSession, me: uuid.UUID, target_id: uuid.UUID) -> None:
    """解除静音；顺带清掉已过期且降级为静音的 block 行。"""
    await interaction_service.unmute_user(db, me, target_id)
    rel = UserRelationshipRepository(db)
    row = await rel.get_relation(me, target_id, "block")
    if (
        row is not None
        and row.expires_at is not None
        and row.expires_at <= now_iso()
        and row.degrade_to_status == FRIEND_STATUS_MUTED
    ):
        await rel.remove(me, target_id, "block")


async def add_close_friend(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID
) -> None:
    pair = await FriendRelationshipRepository(db).get_between(me, target_id)
    if pair is None or pair.status < FRIEND_STATUS_FRIEND:
        raise BizError(InteractionErr.NOT_FRIENDS)
    await FriendRelationshipRepository(db).update(
        pair, status=FRIEND_STATUS_CLOSE_FRIEND
    )


async def remove_close_friend(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID
) -> None:
    pair = await FriendRelationshipRepository(db).get_between(me, target_id)
    if pair is None or pair.status < FRIEND_STATUS_FRIEND:
        raise BizError(InteractionErr.NOT_FRIENDS)
    await FriendRelationshipRepository(db).update(
        pair, status=FRIEND_STATUS_FRIEND
    )


async def update_alias(
    db: DbSession, me: uuid.UUID, target_id: uuid.UUID, alias: str | None
) -> None:
    pair = await FriendRelationshipRepository(db).get_between(me, target_id)
    if pair is None or pair.status < FRIEND_STATUS_FRIEND:
        raise BizError(InteractionErr.NOT_FRIENDS)
    normalized = alias.strip() if isinstance(alias, str) else None
    await FriendRelationshipRepository(db).update(
        pair, alias=normalized or None
    )


# ---------------------------------------------------------------------------
# 读：列表 / 请求 / 好友概览
# ---------------------------------------------------------------------------


async def list_relationships(
    db: DbSession, me: uuid.UUID, *, offset: int, take: int
) -> tuple[list[RelationshipView], int]:
    """好友 + 我单向的 block/mute 合并列表（按最近变更倒序）。

    ponytail: 全量取到内存再合并切片；个人社交图规模有限，等单用户关系数上万再改为
    三表 UNION 的 SQL 分页。
    """
    now = now_iso()
    friend_repo = FriendRelationshipRepository(db)
    rel_repo = UserRelationshipRepository(db)

    entries: dict[uuid.UUID, RelationshipView] = {}
    for row in await friend_repo.list_for_user(
        me, statuses=(FRIEND_STATUS_FRIEND, FRIEND_STATUS_CLOSE_FRIEND)
    ):
        other = _other_id(row, me)
        entries[other] = RelationshipView(
            account_id=other,
            related_id=other,
            other_id=other,
            status=row.status,
            alias=row.alias,
            expires_at=None,
            degrade_to_status=None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    for kind, base in (("block", FRIEND_STATUS_BLOCKED), ("mute", FRIEND_STATUS_MUTED)):
        for row in await rel_repo.list_relations(me, kind):
            effective = _effective_status(row, base, now)
            if effective is None:
                continue
            prev = entries.get(row.target_id)
            changed_at = (
                max(row.created_at, prev.updated_at)
                if prev is not None
                else row.created_at
            )
            entries[row.target_id] = RelationshipView(
                account_id=row.target_id,
                related_id=row.target_id,
                other_id=row.target_id,
                status=effective,
                alias=prev.alias if prev is not None else None,
                expires_at=row.expires_at,
                degrade_to_status=row.degrade_to_status,
                created_at=prev.created_at if prev is not None else row.created_at,
                updated_at=changed_at,
            )

    ordered = sorted(entries.values(), key=lambda item: item.updated_at, reverse=True)
    return ordered[offset : offset + take], len(ordered)


async def list_requests(db: DbSession, me: uuid.UUID) -> list[RelationshipView]:
    """收件 + 已发出的未过期待接受请求，保留真实发起方向。"""
    rows = await FriendRelationshipRepository(db).list_pending(me)
    views = [
        RelationshipView(
            account_id=row.account_id,
            related_id=row.related_id,
            other_id=_other_id(row, me),
            status=FRIEND_STATUS_PENDING,
            alias=row.alias,
            expires_at=row.expires_at,
            degrade_to_status=None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return sorted(views, key=lambda item: item.updated_at, reverse=True)


async def list_friends(db: DbSession, me: uuid.UUID) -> list[uuid.UUID]:
    """好友 id 列表（排除任一方向未过期的 block）。"""
    now = now_iso()
    rel_repo = UserRelationshipRepository(db)
    blocked = set()
    for row in await rel_repo.list_block_rows_touching(me):
        if _effective_status(row, FRIEND_STATUS_BLOCKED, now) == FRIEND_STATUS_BLOCKED:
            blocked.add(row.target_id if row.user_id == me else row.user_id)

    friends: list[uuid.UUID] = []
    for row in await FriendRelationshipRepository(db).list_for_user(
        me, statuses=(FRIEND_STATUS_FRIEND, FRIEND_STATUS_CLOSE_FRIEND)
    ):
        other = _other_id(row, me)
        if other not in blocked:
            friends.append(other)
    return friends


# ---------------------------------------------------------------------------
# wire：内部行 + 身份快照 → desktop 兼容出参
# ---------------------------------------------------------------------------


def _build_account(
    snapshot: UserSnapshot | None,
    user_id: uuid.UUID,
    *,
    timestamp: datetime.datetime,
    base_url: str,
) -> StargateAccountOut:
    name = snapshot.username if snapshot is not None else str(user_id)
    nick = name
    if snapshot is not None:
        nick = snapshot.nickname or snapshot.display_name or name

    picture = None
    if snapshot is not None and snapshot.avatar:
        avatar = snapshot.avatar
        picture = {
            "id": avatar,
            "name": "avatar",
            "mime_type": "image/*",
            "url": f"{base_url}api/v1/auth/avatar/{user_id}?v={avatar}",
        }

    profile = StargateAccountProfileOut(
        id=str(user_id),
        picture=picture,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return StargateAccountOut(
        id=str(user_id),
        name=name,
        nick=nick,
        is_superuser=snapshot is not None and snapshot.role == "admin",
        profile=profile,
        created_at=timestamp,
        updated_at=timestamp,
    )


async def build_relationships(
    db: DbSession,
    views: list[RelationshipView],
    *,
    base_url: str,
) -> list[StargateRelationshipOut]:
    other_ids = [view.other_id for view in views]
    snapshots = (
        await get_user_snapshot_batch(db, user_ids=other_ids) if other_ids else {}
    )
    out: list[StargateRelationshipOut] = []
    for view in views:
        account = _build_account(
            snapshots.get(view.other_id),
            view.other_id,
            timestamp=view.updated_at,
            base_url=base_url,
        )
        out.append(
            StargateRelationshipOut(
                created_at=view.created_at,
                updated_at=view.updated_at,
                account_id=str(view.account_id),
                account=account,
                related_id=str(view.related_id),
                related=account,
                expired_at=view.expires_at,
                status=view.status,
                alias=view.alias,
                degrade_to_status=view.degrade_to_status,
            )
        )
    return out


async def build_friend_overview(
    db: DbSession,
    me: uuid.UUID,
    *,
    base_url: str,
    include_offline: bool = False,
) -> list[StargateFriendOverviewOut]:
    """好友概览：在线状态取 ``/ws/events`` 长连接（多 worker 经 Redis 计数共享）。

    ``include_offline=False``（SDK 默认调用）只返回在线好友；desktop 的完整列表传
    ``includeOffline=true`` 拿全量。活动列表后端暂无 presence activity，保持为空。
    """
    friend_ids = await list_friends(db, me)
    if not friend_ids:
        return []
    online = await manager.are_online(friend_ids)
    if not include_offline:
        friend_ids = [user_id for user_id in friend_ids if user_id in online]
        if not friend_ids:
            return []

    snapshots = await get_user_snapshot_batch(db, user_ids=friend_ids)
    now = now_iso()
    out: list[StargateFriendOverviewOut] = []
    for user_id in friend_ids:
        account = _build_account(
            snapshots.get(user_id),
            user_id,
            timestamp=now,
            base_url=base_url,
        )
        status = StargateAccountStatusOut(
            id=str(user_id),
            account_id=str(user_id),
            account=account,
            is_online=user_id in online,
            created_at=now,
            updated_at=now,
        )
        out.append(
            StargateFriendOverviewOut(
                account=account, status=status, activities=[]
            )
        )
    return out
