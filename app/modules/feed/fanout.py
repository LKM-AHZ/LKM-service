"""时间线写扩散（M6.11）：把新内容 fanout 给关注者，写入物化读模型。

驱动方式：**cron 水位扫描**（每源一条 ``(created_at, id)`` 水位，见
``FeedFanoutState``），而不是给 6 个内容源都挂事件钩子——这样新增源只需进
``feed.SOURCES``/``FOLLOW_SOURCES`` 即可被覆盖，且不引入新的 topic / worker 容器。

- 水位单调推进到「已完整 fanout 的最大条目」；中途失败不推进 → 下轮重放，
  重复由 ``feed_items`` 的 ``(user_id, item_type, source_id)`` 唯一约束吸收。
- **大 V 封顶**：作者关注者数（含关注该内容版块者）超过 ``feed_fanout_max_followers``
  时整条跳过、只把作者记入 Redis 大 V 集合；读路径对这些作者走实时合流补齐
  （见 ``service.get_timeline`` 的物化分支），既不写放大也不丢内容。
- **Article 不参与**：它无作者外键（follow 源里也不参与个性化），无法按作者扩散。

关注者变更时的回填见 :func:`backfill_author`（新关注一位作者时补其最近 N 条）。
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_client
from app.core.cache import make_key
from app.core.config import settings
from app.modules.feed import feed as feed_src
from app.modules.feed.models import FeedFanoutState, FeedItemMaterialized
from app.modules.feed.schemas import FeedItem

logger = logging.getLogger(__name__)


def _bigv_key() -> str:
    return make_key("feed", "bigv")


async def bigv_authors() -> set[uuid.UUID]:
    """当前被标记为「大 V」的作者 id 集合（Redis 不可用 → 空集，读路径退化为纯物化）。"""
    client = await redis_client.get_redis()
    if client is None:
        return set()
    try:
        members = await client.smembers(_bigv_key())
    except Exception:
        # 返回空集 = 读路径关闭大 V 实时补拉（这些作者的内容会「凭空消失」），
        # 排障时必须能看出是这里降级，不能静默吞掉
        logger.warning("read bigv authors failed", exc_info=True)
        return set()
    out: set[uuid.UUID] = set()
    for m in members or ():
        try:
            out.add(uuid.UUID(m))
        except (TypeError, ValueError):
            continue
    return out


async def _mark_bigv(author_id: uuid.UUID) -> bool:
    """把作者记入大 V 集合；返回是否标记成功（读路径据此决定能否实时补拉）。"""
    client = await redis_client.get_redis()
    if client is None:
        return False
    try:
        await client.sadd(_bigv_key(), str(author_id))
    except Exception:
        logger.warning("mark bigv failed for author %s", author_id, exc_info=True)
        return False
    return True


async def _audience(
    db: AsyncSession, author_id: uuid.UUID | None, board_id: uuid.UUID | None
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """``(作者关注者, 版块关注者)`` 两路**分开**返回（均过滤软删）。

    每路最多取 ``cap + 1`` 行：上限判定只关心「是否超过 cap」，取到 cap+1 即已足够
    下结论，无需把超大作者/版块的全部关注者 id 拉进内存。低于 cap 时该 limit 不生效，
    集合仍完整。分开返回是为了按维度分别判定封顶——并集判定会把「版块受众超限」的
    条目连作者关注者也一起跳过，而读路径的实时补拉只认大 V 作者。

    **读者归属**：``user_follows``/``board_follows`` 属 interaction 域（蓝图 §7.2），
    故这里经其 service 公开读口取，不直查那两张表。
    """
    # 惰性 import 破环：interaction.service 在模块级引用本模块（回填/清理入口），
    # 顶层互相 import 会在任一侧先加载时构成循环。
    from app.modules.interaction import service as interaction_service

    limit = settings.feed_fanout_max_followers + 1
    authors: set[uuid.UUID] = set()
    boards: set[uuid.UUID] = set()
    if author_id is not None:
        authors |= set(
            await interaction_service.list_follower_ids(db, author_id, limit=limit)
        )
    if board_id is not None:
        boards |= set(
            await interaction_service.list_board_follower_ids(db, board_id, limit=limit)
        )
    return authors, boards


async def _fanout_item(db: AsyncSession, item: FeedItem) -> int:
    """把一条内容写入其受众的物化 feed；返回写入的受众数（跳过时 0）。

    封顶按**维度**分别判定：作者维超限才跳过作者扩散（标记大 V，由读路径实时补），
    版块维超限只跳过版块扩散。此前按两路并集判定，导致「小作者 + 大版块」的条目连
    作者关注者也一起不写，而大 V 实时补拉只覆盖关注作者的人。
    """
    if item.author_id is None and item.board_id is None:
        return 0

    cap = settings.feed_fanout_max_followers
    author_followers, board_followers = await _audience(
        db, item.author_id, item.board_id
    )
    if len(author_followers) > cap:
        # 大 V：标记后由读路径实时补齐（避免 O(关注者) 写入突刺）。标记失败则**不能**跳过
        # 写扩散——读路径的 bigv_authors() 同样读 Redis，拿不到标记就不会补拉，跳过等于
        # 永久丢内容（此时回退为照常写，行数已被上面的 limit 钉在 cap+1 量级）。
        if item.author_id is not None and await _mark_bigv(item.author_id):
            author_followers = set()
        else:
            logger.warning(
                "bigv 标记失败，回退写扩散以免内容丢失: %s#%s",
                item.item_type,
                item.id,
            )
    if len(board_followers) > cap:
        # 版块维超限：读路径暂无版块级补拉通道，只能跳过（已知缺口，登记于路线图 §8）
        logger.warning(
            "skip board fanout for %s#%s: %d board followers exceed cap",
            item.item_type,
            item.id,
            len(board_followers),
        )
        board_followers = set()

    followers = author_followers | board_followers
    if not followers:
        logger.info(
            "skip fanout for %s#%s: followers exceed cap",
            item.item_type,
            item.id,
        )
        return 0

    values = [
        {
            "user_id": uid,
            "item_type": item.item_type,
            "source_id": item.id,
            "author_id": item.author_id,
            "board_id": item.board_id,
            "sort_score": item.sort_score,
            "title": item.title,
            "content_preview": item.content_preview,
            "url": item.url,
            "created_at": item.created_at,
        }
        for uid in sorted(followers)
    ]
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    return len(values)


async def _load_state(db: AsyncSession, source: str) -> FeedFanoutState:
    """取源水位；首次出现时以「现在」为起点（存量内容不回填，由实时合流兜底读）。"""
    state = await db.get(FeedFanoutState, source)
    if state is None:
        state = FeedFanoutState(
            source=source,
            last_created_at=datetime.datetime.now(datetime.UTC),
            last_id=None,
        )
        db.add(state)
        await db.flush()
    return state


async def fanout_batch(db: AsyncSession, per_source_limit: int = 200) -> int:
    """扫描各源水位之后的新内容并 fanout，返回本轮处理条目数。

    每个源每轮最多处理 ``per_source_limit`` 条（升序），水位推进到本批最大条目；
    还有积压时下一轮继续（不会跳过）。
    """
    processed = 0
    for name in feed_src.FOLLOW_SOURCES:
        state = await _load_state(db, name)
        fetch = feed_src.SOURCES[name]
        items: list[FeedItem] = await fetch(
            db,
            None,
            None,
            None,
            None,
            per_source_limit,
            after_time=state.last_created_at,
            after_id=state.last_id,
        )
        if not items:
            continue
        for item in items:
            await _fanout_item(db, item)
        state.last_created_at = items[-1].created_at
        state.last_id = items[-1].id
        processed += len(items)
        await db.flush()
    return processed


async def backfill_author(
    db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID, limit: int
) -> int:
    """把作者最近 ``limit`` 条内容补进该关注者的物化 feed（新关注时调用，幂等）。

    大 V 作者直接跳过：其条目本就不写物化、改由读路径实时补拉，而物化读路径把
    「物化行 + 实时结果」直接拼接、不做 (item_type, id) 去重，回填会让同一条内容
    在时间线上出现两次。
    """
    if author_id in await bigv_authors():
        return 0
    values: list[dict[str, object]] = []
    for name in feed_src.FOLLOW_SOURCES:
        fetch = feed_src.SOURCES[name]
        # discussion 源要求 author/board 两个集合都给才按作者过滤，故传空版块集
        items: list[FeedItem] = await fetch(db, {author_id}, set(), None, None, limit)
        values.extend(
            {
                "user_id": follower_id,
                "item_type": item.item_type,
                "source_id": item.id,
                "author_id": item.author_id or author_id,
                "board_id": item.board_id,
                "sort_score": item.sort_score,
                "title": item.title,
                "content_preview": item.content_preview,
                "url": item.url,
                "created_at": item.created_at,
            }
            for item in items
        )
    if not values:
        return 0
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    await db.flush()
    return len(values)


async def backfill_board(
    db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID, limit: int
) -> int:
    """把版块最近 ``limit`` 条讨论帖补进该关注者的物化 feed（新关注版块时调用）。"""
    items: list[FeedItem] = await feed_src.SOURCES["discussion"](
        db, set(), {board_id}, None, None, limit
    )
    if not items:
        return 0
    values = [
        {
            "user_id": follower_id,
            "item_type": item.item_type,
            "source_id": item.id,
            "author_id": item.author_id,
            "board_id": item.board_id,
            "sort_score": item.sort_score,
            "title": item.title,
            "content_preview": item.content_preview,
            "url": item.url,
            "created_at": item.created_at,
        }
        for item in items
    ]
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    await db.flush()
    return len(values)


async def remove_author_items(
    db: AsyncSession,
    follower_id: uuid.UUID,
    author_id: uuid.UUID,
    keep_boards: set[uuid.UUID],
) -> int:
    """取消关注某作者时清理其条目。

    与 :func:`remove_board_items` 对称：物化行同时记 ``author_id`` 与 ``board_id``，
    同一内容可能既因作者、也因版块进入 feed。取关作者时若该行所属版块仍在关注列表里
    必须保留，否则会静默丢掉仍应可见的版块内容。
    """
    conds = [
        FeedItemMaterialized.user_id == follower_id,
        FeedItemMaterialized.author_id == author_id,
    ]
    if keep_boards:
        conds.append(
            FeedItemMaterialized.board_id.is_(None)
            | FeedItemMaterialized.board_id.notin_(keep_boards)
        )
    result = await db.execute(sa_delete(FeedItemMaterialized).where(*conds))
    await db.flush()
    return int(result.rowcount or 0)


async def remove_board_items(
    db: AsyncSession,
    follower_id: uuid.UUID,
    board_id: uuid.UUID,
    keep_authors: set[uuid.UUID],
) -> int:
    """取消关注版块时清理该版块条目。

    仅删「不再因作者关注而保留」的行——``board_id`` 匹配但作者仍在关注列表里的条目
    保留（同一内容可能同时因作者与版块进入 feed）。
    """
    conds = [
        FeedItemMaterialized.user_id == follower_id,
        FeedItemMaterialized.board_id == board_id,
    ]
    if keep_authors:
        conds.append(
            FeedItemMaterialized.author_id.is_(None)
            | FeedItemMaterialized.author_id.notin_(keep_authors)
        )
    result = await db.execute(sa_delete(FeedItemMaterialized).where(*conds))
    await db.flush()
    return int(result.rowcount or 0)


async def remove_source_item(db: AsyncSession, source_id: uuid.UUID) -> int:
    """按源条目 id 清理物化行，返回删除行数。

    内容软删（批 4）后必须调用：物化表存的是**写入时快照**，不会随源行软删自动消失，
    否则关注者时间线仍能看到已删内容。``content_items`` 软删是它当前的唯一调用方。
    """
    result = await db.execute(
        sa_delete(FeedItemMaterialized).where(
            FeedItemMaterialized.source_id == source_id
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


async def count_materialized(db: AsyncSession, user_id: uuid.UUID) -> int:
    """某用户物化 feed 的条目数（读路径判「物化是否可用」，以及测试断言用）。"""
    return (
        await db.scalar(
            select(func.count())
            .select_from(FeedItemMaterialized)
            .where(FeedItemMaterialized.user_id == user_id)
        )
    ) or 0
