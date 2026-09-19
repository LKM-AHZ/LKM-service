"""points 域的仓储子类：余额原子写 + 流水 + 榜单聚合 + 成就/任务/兑换定义。

余额/进度表主键是 ``user_id``（非 ``id``），对应子类显式 ``pk_attr = "user_id"``。
所有聚合/窗口/``func.*`` 查询（榜单归并、成就 join、JSON 统计读）都收在这里，
service 层只做纯 Python 组装与缓存编排。

流水幂等：原 service 用 savepoint 承载 ``points_ledger`` 插入并捕获
``IntegrityError``；本层改用 ``pg_upsert(do_nothing=True)``（约束
``uq_points_ledger_ref``）—— 冲突由 PG 直接忽略，不产生异常、不污染调用方事务，
service 侧以「回读流水行」判定是否已存在。并发语义仍幂等（撞键者回读既有行）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.db.base import now_iso
from app.db.repository import AsyncRepository
from app.modules.points.models import (
    Achievement,
    ExchangeItem,
    PointsLedger,
    Task,
    UserAchievement,
    UserBalance,
    UserBehaviorStat,
    UserTaskProgress,
)


class UserBalanceRepository(AsyncRepository[UserBalance]):
    model = UserBalance
    pk_attr = "user_id"

    async def get_value(self, user_id: uuid.UUID) -> int | None:
        """余额单列读；行不存在返回 ``None``（调用方按 0 处理）。"""
        return await self.db.scalar(
            select(UserBalance.balance).where(UserBalance.user_id == user_id)
        )

    async def list_positive(self) -> list[tuple[uuid.UUID, int]]:
        """余额 > 0 的 (user_id, balance)（total 榜数据源）。"""
        rows = await self.db.execute(
            select(UserBalance.user_id, UserBalance.balance).where(
                UserBalance.balance > 0
            )
        )
        return [(uid, int(balance)) for uid, balance in rows.all()]

    async def apply_delta(
        self, user_id: uuid.UUID, delta: int, allow_negative: bool
    ) -> int | None:
        """原子增减 balance 并返回变动后余额。

        ``WHERE balance + delta >= 0``（允许负时无条件）；rowcount==0 表示余额不足
        或用户无记录 → 返回 ``None``（由 service 转领域错误）。更新后回读与原先
        ``db.get`` 同源，读到的是同事务内的最新值。
        """
        stmt = sa_update(UserBalance).where(UserBalance.user_id == user_id)
        if allow_negative:
            stmt = stmt.values(
                balance=UserBalance.balance + delta, updated_at=now_iso()
            )
        else:
            stmt = stmt.where(UserBalance.balance + delta >= 0).values(
                balance=UserBalance.balance + delta, updated_at=now_iso()
            )
        result = await self.db.execute(stmt)
        if (getattr(result, "rowcount", 0) or 0) == 0:
            return None
        row = await self.db.get(UserBalance, user_id)
        assert row is not None
        return int(row.balance)


class PointsLedgerRepository(AsyncRepository[PointsLedger]):
    model = PointsLedger

    async def get_by_ref(
        self, user_id: uuid.UUID, ref_type: str, ref_id: str
    ) -> PointsLedger | None:
        """按幂等键 (user_id, ref_type, ref_id) 取流水行。"""
        return await self.get_one(
            PointsLedger.user_id == user_id,
            PointsLedger.ref_type == ref_type,
            PointsLedger.ref_id == ref_id,
        )

    async def sum_positive_since(
        self, since: datetime.datetime
    ) -> list[tuple[uuid.UUID, int]]:
        """窗口内 delta>0 按用户归并求和（daily/weekly 榜数据源）。"""
        agg = await self.db.execute(
            select(
                PointsLedger.user_id,
                func.sum(PointsLedger.delta).label("total"),
            )
            .where(PointsLedger.created_at >= since, PointsLedger.delta > 0)
            .group_by(PointsLedger.user_id)
        )
        return [(uid, int(total)) for uid, total in agg.all()]

    async def add_all(self, entries: list[PointsLedger]) -> None:
        """一批流水一次 flush（转账两笔共享同一 flush 边界）。"""
        self.db.add_all(entries)
        await self.flush()


class AchievementRepository(AsyncRepository[Achievement]):
    model = Achievement

    async def list_ordered(self) -> list[Achievement]:
        return await self.get_many(order_by=Achievement.sort_order)

    async def unlocked_keys_for(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, set[str]]:
        """批量取用户已解锁成就 key（一次 IN join，避免榜单 N+1）。"""
        rows = await self.db.execute(
            select(Achievement.key, UserAchievement.user_id)
            .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
            .where(
                UserAchievement.user_id.in_(user_ids),
                UserAchievement.unlocked.is_(True),
            )
        )
        unlocked: dict[uuid.UUID, set[str]] = {}
        for key, uid in rows.all():
            unlocked.setdefault(uid, set()).add(key)
        return unlocked


class UserAchievementRepository(AsyncRepository[UserAchievement]):
    model = UserAchievement

    async def list_for_user(self, user_id: uuid.UUID) -> list[UserAchievement]:
        return await self.get_many(UserAchievement.user_id == user_id)


class UserBehaviorStatRepository(AsyncRepository[UserBehaviorStat]):
    model = UserBehaviorStat
    pk_attr = "user_id"

    async def get_locked(self, user_id: uuid.UUID) -> UserBehaviorStat | None:
        """行锁取行为统计（打卡串行化用 ``SELECT ... FOR UPDATE``）。"""
        stmt = (
            select(UserBehaviorStat)
            .where(UserBehaviorStat.user_id == user_id)
            .with_for_update()
        )
        return (await self.db.execute(stmt)).scalars().first()


class TaskRepository(AsyncRepository[Task]):
    model = Task

    async def list_ordered(self) -> list[Task]:
        return await self.get_many(order_by=Task.sort_order)


class UserTaskProgressRepository(AsyncRepository[UserTaskProgress]):
    model = UserTaskProgress

    async def list_for_user_period(
        self, user_id: uuid.UUID, period_date: str
    ) -> list[UserTaskProgress]:
        return await self.get_many(
            UserTaskProgress.user_id == user_id,
            UserTaskProgress.period_date == period_date,
        )


class ExchangeItemRepository(AsyncRepository[ExchangeItem]):
    model = ExchangeItem

    async def list_ordered(self) -> list[ExchangeItem]:
        return await self.get_many(order_by=ExchangeItem.sort_order)
