"""将 git push 的内容同步回填进 ``blog_content``（DB 为权威源）。

由 ``git_http.py`` 在收到 receive-pack 成功后调用；对每个本次变更的 path，
读仓库内容，按 ``push_at > row.updated_at`` 规则决定 upsert 或跳过（DB 更新为准）。
纯逻辑、不依赖 HTTP，便于用 mock git 单测。
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.blog import git_svc
from app.modules.content.blog.errors import BlogErr
from app.modules.content.blog.models import BlogContent, BlogSeries
from core.db.base import now_iso
from core.db.repo import get_or_raise
from core.err import BizError


@dataclass
class BackfillResult:
    """一次 push 回填的统计：本次变更路径 / 已采纳 / 因 DB 更新被跳过。"""

    paths: list[str] = field(default_factory=list)
    upserted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _sha3(content: str) -> str:
    import hashlib

    return hashlib.sha3_256(content.encode("utf-8")).hexdigest()


async def backfill_series_from_git(
    db: AsyncSession,
    repo_name: str,
    series_id: uuid.UUID,
    old_sha: str | None,
    push_at: datetime | None = None,
) -> BackfillResult:
    """对比 git HEAD 前后变更的 path，读最新内容 upsert 进 blog_content。

    - 无对应 BlogContent 行 → 直接插入（version=1）。
    - 有行且 push_at > row.updated_at → 更新内容 + version+1。
    - 有行且 push_at <= row.updated_at → 跳过（DB 更权威），不覆盖。
    返回 BackfillResult 供日志与测试断言。
    """
    await get_or_raise(
        db, BlogSeries, BlogErr.SERIES_NOT_FOUND, BlogSeries.id == series_id
    )
    new_sha = await asyncio.to_thread(git_svc.revparse_or_none, repo_name)
    if not new_sha:
        return BackfillResult()
    push_at = push_at or now_iso()

    changed = await asyncio.to_thread(
        git_svc.diff_tree_names, repo_name, old_sha, new_sha
    )
    result = BackfillResult(paths=changed)

    # 一次性把该 series 下涉及的 path 行取回：循环里逐个 select 是 N+1 往返
    rows_by_path: dict[str, BlogContent] = {
        r.path: r
        for r in (
            (
                await db.execute(
                    select(BlogContent).where(
                        BlogContent.series_id == series_id,
                        BlogContent.path.in_(changed),
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    for path in changed:
        try:
            content = await asyncio.to_thread(git_svc.read_file, repo_name, path)
        except BizError:
            result.skipped.append(path)
            continue
        sha = _sha3(content)

        row = rows_by_path.get(path)

        if row is None:
            db.add(
                BlogContent(
                    series_id=series_id,
                    path=path,
                    content=content,
                    sha3=sha,
                    version=1,
                )
            )
            result.upserted.append(path)
        elif push_at > row.updated_at:
            row.content = content
            row.sha3 = sha
            row.version = row.version + 1
            row.updated_at = now_iso()
            result.upserted.append(path)
        else:
            result.skipped.append(path)

    await db.flush()
    return result
