"""starhope 域的仓储子类：四类同步实体（questions/folders/sessions/agents）的拉取与合并写。

四张表字段同名同构（``user_id`` / ``id`` 字符串主键 / ``updated_at`` / ``deleted_at``），
故仿 ``auth.repository.VerificationRepository`` 的做法：``model`` 由构造参数按 entity
运行期注入，pull/push 两套算法只写一遍。

``push`` 的「按 id 合并 + 最后写赢 + 墓碑软删」是单个事务内的批量写，故整段收在仓储里
（新建的行用 ``self.db.add`` 暂存，末尾单次 flush），service 只负责解析/校验入参。
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select

from core.db.base import now_iso
from core.db.repository import AsyncRepository, DbSession, ValuesDict


class StarHopeRepository(AsyncRepository[Any]):
    """starhope 同步表的通用仓储（模型运行期选定，非类属性绑定）。"""

    def __init__(self, db: DbSession, model: type[Any]) -> None:
        super().__init__(db)
        self.model = model

    async def sync_time(self, user_id: uuid.UUID) -> datetime.datetime:
        """Serialize a workspace's reads/writes through commit before taking its cursor.

        A timestamp taken before a competing transaction commits would skip its
        receipts. PostgreSQL's transaction lock covers both pull and push; the
        database clock also avoids disagreement between application servers.
        """
        if self.db.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(
                    f"starhope:{self.model.__tablename__}:{user_id}".encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
            await self.db.execute(select(func.pg_advisory_xact_lock(key)))
            timestamp = await self.db.scalar(select(func.clock_timestamp()))
            if not isinstance(timestamp, datetime.datetime):
                raise RuntimeError("Missing database sync timestamp")
            return timestamp
        return now_iso()

    async def list_changed(
        self, *, user_id: uuid.UUID, since: datetime.datetime | None
    ) -> list[Any]:
        """按服务端接收时间增量读取，客户端离线时间只参与 LWW。"""
        conditions: list[Any] = [self.model.user_id == user_id]
        if since is not None:
            conditions.append(self.model.received_at >= since)
        return await self.get_many(*conditions)

    async def list_tombstones(
        self, *, user_id: uuid.UUID, since: datetime.datetime | None
    ) -> list[tuple[str, datetime.datetime]]:
        """已软删实体的 ``(id, deleted_at)``；``since`` 非空时只取此后的墓碑。"""
        stmt = select(self.model.id, self.model.deleted_at).where(
            self.model.user_id == user_id,
            self.model.deleted_at.is_not(None),
        )
        if since is not None:
            stmt = stmt.where(self.model.received_at >= since)
        return [
            (entity_id, deleted_at)
            for entity_id, deleted_at in (await self.db.execute(stmt)).all()
            if deleted_at is not None
        ]

    async def push(
        self,
        *,
        user_id: uuid.UUID,
        upserts: list[tuple[str, ValuesDict, datetime.datetime]],
        deletes: list[tuple[str, datetime.datetime]],
    ) -> int:
        """合并写 upserts/deletes，返回实际写入条数；末尾单次 flush。

        ``upserts`` 每项为 ``(id, values, updated_at)``，``values`` 已含 ``user_id``
        与 JSON 序列化后的字段；``deletes`` 每项为 ``(id, deleted_at)``。冲突口径：
        已软删行仅被更新的 incoming 复活；incoming 不新于现存 ``updated_at`` 时跳过。
        """
        ids = {rid for rid, _, _ in upserts} | {rid for rid, _ in deletes}
        received_at = await self.sync_time(user_id)
        existing_map: dict[str, Any] = {}
        if ids:
            rows = await self.get_many(
                self.model.id.in_(ids),
                self.model.user_id == user_id,
                include_deleted=True,
            )
            existing_map = {row.id: row for row in rows}

        synced = 0
        for rid, data, updated_at in upserts:
            existing = existing_map.get(rid)
            if existing is None:
                obj = self.model(**data, received_at=received_at)
                self.db.add(obj)
                # 登记进 existing_map：同一批次里重复的 id 走下面的合并分支，
                # 不会暂存两个同主键实例（flush 时双 INSERT 撞键）；也令随后的
                # delete 分支能看到这条新行并打墓碑。
                existing_map[rid] = obj
                synced += 1
                continue
            # 已软删：只有 incoming 更新才恢复
            if existing.deleted_at is not None and updated_at < existing.deleted_at:
                continue
            if updated_at >= existing.updated_at:
                for key, value in data.items():
                    if key not in ("id", "user_id"):
                        setattr(existing, key, value)
                existing.deleted_at = None
                existing.received_at = received_at
                synced += 1

        for rid, deleted_at in deletes:
            existing = existing_map.get(rid)
            if existing is None:
                continue
            last_write = existing.deleted_at or existing.updated_at
            if deleted_at > last_write:
                existing.deleted_at = deleted_at
                existing.updated_at = deleted_at
                existing.received_at = received_at
                synced += 1

        await self.flush()
        return synced


__all__ = ["StarHopeRepository"]
