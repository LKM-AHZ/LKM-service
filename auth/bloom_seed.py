"""user id 白名单位图的全量预热（蓝图 §5.6）。

**为什么需要预热**：`app/core/bloom.py` 的位图是 ``user:snap`` 的 id **白名单**——「不在位图里」
被解读为「该 id 从未存在」并直接短路。这只有在位图**完整**（每个合法 id 都被 ``add`` 过）时才成立。
增量靠「建号即 add」（``auth.service_auth.create_user_with_profile`` 等），存量靠本模块。

**为什么只增不重建**：布隆不可删，但已删除用户的 id 留在位图里是无害的——``might_contain`` 返
``True`` → 落回真实查找 → 由负值缓存兜。故这里只做单调追加，重跑幂等，不需要双缓冲/换键。

**门禁**：预热**完整成功**后才 :func:`bloom.mark_seeded`（带 TTL）。未预热/打标记失败/扫到空库 →
不打标记，读取侧（:func:`bloom.definitely_absent`）就整段不生效——拿一份可能残缺的位图去拒绝
用户是最坏结果，宁可漏拦。

**完整性的兜底**：``add_many`` 返回实际写入数，任何一块写入失败即**不打标记**并提前返回。

会话工厂做成模块级可替换（同 ``auth.user_dim_sync._session_factory`` / ``auth.seed_users``），
便于测试把预热会话指到测试专属 auth 库。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db.session import new_auth_session
from auth.models import User
from core import bloom

logger = logging.getLogger("lkm.auth.bloom_seed")

_session_factory: Callable[[], Awaitable[AsyncSession]] = new_auth_session

# keyset 分页大小：uuid 可全序比较，按 ``id > cursor`` 翻页避免 OFFSET 的深分页成本。
_PAGE = 1000


async def backfill_user_ids() -> int:
    """全量扫描 auth 库 ``users.id`` 写入白名单位图；**完整成功**才打「已预热」门禁标记。

    返回写入的 id 数。幂等（``add`` 只是重写同样的位），可反复跑。扫到 0 个用户不打标记
    （空白名单会把所有人拦掉）；任一页写入不完整也立即停并**不**打标记。
    """
    db = await _session_factory()
    total = 0
    try:
        cursor: uuid.UUID | None = None
        while True:
            stmt = select(User.id).order_by(User.id).limit(_PAGE)
            if cursor is not None:
                stmt = stmt.where(User.id > cursor)
            rows = (await db.execute(stmt)).scalars().all()
            if not rows:
                break
            chunk = [str(uid) for uid in rows]
            written = await bloom.add_many(chunk)
            if written != len(chunk):
                logger.warning(
                    "bloom seed 中断：本页应写 %d 实写 %d，不打 seeded 标记",
                    len(chunk),
                    written,
                )
                return total
            total += len(rows)
            cursor = rows[-1]
            if len(rows) < _PAGE:
                break
    finally:
        await db.close()

    if total == 0:
        logger.warning("bloom seed: users 表为空，跳过标记（避免空白名单误拒）")
        return 0
    if await bloom.mark_seeded():
        logger.info("bloom seed: 已写入 %d 个 user id 并打上 seeded 标记", total)
    else:
        logger.warning(
            "bloom seed: 位图写入 %d 条但打标记失败（Redis 不可用/开关关？）——不生效",
            total,
        )
    return total
