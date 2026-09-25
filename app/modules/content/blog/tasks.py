"""blog 模块队列任务：孤儿博客 git 仓库周对账。

隔离/回收无 blog_series 记录的裸仓库。每周四 04:00 由 scheduler 发布 cron.reconcile
到 system/cron topic，本任务经 jobs 订阅消费执行。

任务无请求上下文，自建独立会话（模块级 ``_session_factory`` seam，测试可替换
为 conftest 的内存会话）。Redis 锁防自相竞争，删前复查 blog_series 存在性防误删。
"""

import asyncio
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select

from app.core.config import settings
from app.core.messaging import RKEY_RECONCILE, SUB_JOBS
from app.core.redis import get_redis
from app.core.task_registry import register_cron_job, register_task
from app.db.session import new_worker_session as new_session
from app.modules.content.blog.models import BlogRepoQuarantine, BlogSeries

logger = logging.getLogger(__name__)

_QUARANTINE_DAYS = 7  # 隔离保留天数，之后才物理删除
_LOCK_KEY = "blog:reconcile:lock"

_session_factory = new_session  # seam：测试可替换

_lock_token: str | None = None  # 本进程持锁时写入的 token（释放时比对，防误删别人的锁）


def _iter_repo_dirs() -> list[str]:
    """遍历 blog_repos 目录下所有裸仓库目录名(带 .git 后缀，不带路径)。

    只认目录：普通文件（如误建的 ``x.git`` 文件）也会 endswith(".git")，而下游对
    文件的 rmtree 会静默失败却仍删掉台账行，于是每次对账都重复隔离/清理同一个文件。
    """
    base = os.path.abspath(settings.blog_repo_dir)
    if not os.path.isdir(base):
        return []
    return [
        d
        for d in os.listdir(base)
        if d.endswith(".git") and os.path.isdir(os.path.join(base, d))
    ]


async def _should_lock() -> bool:
    """拿 Redis 原子锁；拿不到返回 False(跳过本次对账)，防自相竞争。

    锁值用本次获取的随机 token：固定值时若本次运行超过 1h TTL（另一实例已重新拿到锁），
    收尾的无条件 delete 会删掉**别人**的锁，互斥就此失效。
    """
    global _lock_token
    redis = await get_redis()
    if redis is None:
        return True  # 无 Redis 简化为放行(测试场景)
    token = uuid.uuid4().hex
    got = await redis.set(_LOCK_KEY, token, ex=3600, nx=True)  # 1h 后自动释放
    if got:
        _lock_token = token
    return bool(got)


async def _release_lock() -> None:
    """compare-and-delete：只删自己那把锁（Lua 保证比对与删除原子）。"""
    global _lock_token
    token, _lock_token = _lock_token, None
    redis = await get_redis()
    if redis is None or token is None:
        return
    with suppress(Exception):
        # 返回值 cast 成 Awaitable 以符合 redis 5.x 的 `Awaitable[str]|str` 存根
        await cast(
            Awaitable[int],
            redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                _LOCK_KEY,
                token,
            ),
        )


async def reconcile_blog_repos() -> None:
    if not await _should_lock():
        logger.info("blog 对账已被其他实例执行, 本次跳过")
        return

    db = await _session_factory()
    try:
        # abspath 仅字符串运算不阻塞，周任务可接受
        base = os.path.abspath(settings.blog_repo_dir)  # noqa: ASYNC240
        # 已存在的 blog_series.repo_name 集合
        live = set((await db.execute(select(BlogSeries.repo_name))).scalars().all())
        quarantined = {
            q.repo_name: q
            for q in (await db.execute(select(BlogRepoQuarantine))).scalars().all()
        }
        now = datetime.now(UTC)

        seen: set[str] = set()
        for name in await asyncio.to_thread(_iter_repo_dirs):
            repo_name = name[:-4]  # 去掉 .git
            seen.add(repo_name)
            q = quarantined.get(repo_name)
            if repo_name in live:
                if q is not None:
                    # 曾隔离的仓库又有了 blog_series 行（重建/恢复）→ 台账行已失效
                    await db.delete(q)
                    logger.info("blog 清理已失效的隔离台账: %s", repo_name)
                continue
            if q is None:
                # 首次发现：隔离入库（目录不动）
                db.add(
                    BlogRepoQuarantine(
                        repo_name=repo_name,
                        src_dir=os.path.join(base, name),
                        quarantined_at=now,
                    )
                )
                logger.info("blog 隔离孤儿仓库: %s", repo_name)
            elif now - q.quarantined_at > timedelta(days=_QUARANTINE_DAYS):
                # 超龄：删前复查，仍无记录才物理删除
                still_live = await db.scalar(
                    select(BlogSeries.id).where(BlogSeries.repo_name == repo_name)
                )
                if still_live is not None:
                    continue  # 期间已建记录，取消删除
                await asyncio.to_thread(shutil.rmtree, q.src_dir, True)
                if await asyncio.to_thread(os.path.exists, q.src_dir):
                    # 删除没真正生效（权限/占用）：保留台账行下次再试，别记成已清理
                    logger.warning("blog 隔离仓库删除未生效，保留台账: %s", repo_name)
                    continue
                await db.delete(q)
                logger.info("blog 清理超龄隔离仓库: %s", repo_name)

        # 目录已不在磁盘的台账行同样作废（人工删除/卷丢失），否则永久滞留成幽灵记录
        for repo_name, q in quarantined.items():
            if repo_name in seen:
                continue
            await db.delete(q)
            logger.info("blog 清理目录已消失的隔离台账: %s", repo_name)

        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        # 注意：不 close（见模块 docstring）——测试注入的会话由 conftest 关闭，
        # 生产会话交连接池/GC。释放 Redis 锁（只删自己那把）。
        await _release_lock()


register_task(SUB_JOBS.name, "reconcile_blog_repos", reconcile_blog_repos)
register_cron_job(
    job_id="reconcile_blog_repos",
    cron="0 4 * * 4",  # 每周四 04:00
    routing_key=RKEY_RECONCILE,
    fn="reconcile_blog_repos",
)
