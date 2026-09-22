"""feed 域的仓储子类：关注关系（软删墓碑幂等）+ 物化时间线读。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放 feed 域的
领域查询。业务域 import 内容表（``Board``）只读缝已在 pyproject 的 import-linter
精确豁免（原 ``feed.service -> content.models`` 随查询下沉为
``feed.repository -> content.models``）。

关注关系的「软删墓碑」语义：follow 时已有行 ``deleted_at`` 置 NULL（复活），否则新插；
unfollow 只置 ``deleted_at``。故取配对行必须 ``include_deleted=True``，否则复活路径
永远看不到墓碑行。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select

from app.db.base import now_iso
from app.db.repository import AsyncRepository, DbSession
from app.modules.content.models import Board
from app.modules.feed.models import BoardFollow, FeedItemMaterialized, UserFollow


class UserFollowRepository(AsyncRepository[UserFollow]):
    model = UserFollow

    async def get_pair(
        self, follower_id: uuid.UUID, following_id: uuid.UUID
    ) -> UserFollow | None:
        """取 (follower, following) 配对行——**含已软删**（复活/promotion 判定需要）。"""
        return await self.get_one(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
            include_deleted=True,
        )

    async def follow(self, follower_id: uuid.UUID, following_id: uuid.UUID) -> bool:
        """关注（幂等）：返回是否属于「新关注/复活」（供 fanout 回填判定）。

        已存在且未软删时是 no-op（仍 flush 保持原调用节奏）。
        """
        row = await self.get_pair(follower_id, following_id)
        created = row is None or row.deleted_at is not None
        if row is None:
            await self.create(follower_id=follower_id, following_id=following_id)
        elif row.deleted_at is not None:
            await self.update(row, deleted_at=None)
        else:
            await self.flush()
        return created

    async def unfollow(self, follower_id: uuid.UUID, following_id: uuid.UUID) -> bool:
        """取关（幂等）：仅命中活动行才置墓碑，返回是否真的发生变更。"""
        row = await self.get_pair(follower_id, following_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_following_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """我关注的所有用户 id（活动行，无排序——与原实现一致）。"""
        rows = await self.db.execute(
            select(UserFollow.following_id).where(
                UserFollow.follower_id == user_id,
                UserFollow.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    async def is_following(
        self, follower_id: uuid.UUID, following_id: uuid.UUID
    ) -> bool:
        """当前是否存在活动关注（基类 exists 自动施加 deleted_at 过滤）。"""
        return await self.exists(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
        )


class BoardFollowRepository(AsyncRepository[BoardFollow]):
    model = BoardFollow

    async def get_pair(
        self, follower_id: uuid.UUID, board_id: uuid.UUID
    ) -> BoardFollow | None:
        """取 (follower, board) 配对行——**含已软删**。"""
        return await self.get_one(
            BoardFollow.follower_id == follower_id,
            BoardFollow.board_id == board_id,
            include_deleted=True,
        )

    async def follow(self, follower_id: uuid.UUID, board_id: uuid.UUID) -> bool:
        """关注版块（幂等）：返回是否属于「新关注/复活」。"""
        row = await self.get_pair(follower_id, board_id)
        created = row is None or row.deleted_at is not None
        if row is None:
            await self.create(follower_id=follower_id, board_id=board_id)
        elif row.deleted_at is not None:
            await self.update(row, deleted_at=None)
        else:
            await self.flush()
        return created

    async def unfollow(self, follower_id: uuid.UUID, board_id: uuid.UUID) -> bool:
        """取关版块（幂等）：仅命中活动行才置墓碑。"""
        row = await self.get_pair(follower_id, board_id)
        if row is None or row.deleted_at is not None:
            return False
        await self.update(row, deleted_at=now_iso())
        return True

    async def list_board_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """我关注的所有版块 id（活动行）。"""
        rows = await self.db.execute(
            select(BoardFollow.board_id).where(
                BoardFollow.follower_id == user_id,
                BoardFollow.deleted_at.is_(None),
            )
        )
        return list(rows.scalars().all())


class FeedBoardRepository:
    """feed 侧**只读**板块表缝（标题回填 / 关注目标存在性）。

    刻意不继承 ``AsyncRepository[Board]``：继承会把 create/update/delete/pg_upsert 这整套写面
    暴露给 feed 域，既与该类 docstring、也与 pyproject 里「feed->content.models 只读缝」的
    import-linter 豁免口径相矛盾；而且 ``Board`` 没有 ``deleted_at``，继承来的
    soft_delete/soft_delete_where 会静默退化成真 DELETE（内容表实删）。
    这里只暴露 feed 真正用到的那两个读方法，写路径请回 content 域自己的仓储。
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db

    async def get(self, board_id: uuid.UUID) -> Board | None:
        """按主键取板块（不存在 → ``None``）。"""
        return await self.db.get(Board, board_id)

    async def title_map(self, board_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not board_ids:
            return {}
        rows = (
            (await self.db.execute(select(Board).where(Board.id.in_(board_ids))))
            .scalars()
            .all()
        )
        return {b.id: b.title for b in rows}


class FeedItemMaterializedRepository(AsyncRepository[FeedItemMaterialized]):
    model = FeedItemMaterialized

    async def list_page(
        self,
        user_id: uuid.UUID,
        *,
        before_time: datetime.datetime | None,
        before_id: uuid.UUID | None,
        limit: int,
    ) -> list[FeedItemMaterialized]:
        """物化表按 (created_at, id) 游标取一页（时间倒序）。

        下滤条件与 ``feed.feed._before_conds`` 严格同式（本类自持一份，避免仓库层
        反向依赖实时合流模块）；同理，``before_id`` 缺失时退化为纯时间下滤——
        不能保留 ``id < NULL``（SQL 中恒为 NULL，会把同一时刻的行整批漏掉）。
        """
        conds: list[Any] = [FeedItemMaterialized.user_id == user_id]
        if before_time is not None:
            if before_id is None:
                conds.append(FeedItemMaterialized.created_at < before_time)
            else:
                conds.append(
                    (FeedItemMaterialized.created_at < before_time)
                    | (
                        (FeedItemMaterialized.created_at == before_time)
                        & (FeedItemMaterialized.id < before_id)
                    )
                )
        return await self.get_many(
            *conds,
            order_by=(
                FeedItemMaterialized.created_at.desc(),
                FeedItemMaterialized.id.desc(),
            ),
            limit=limit,
        )
