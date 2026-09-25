"""GraphQL 读侧的「批量而非 N+1」保证（蓝图 §2 第 4 条）。

蓝图原话是「深查询内的列表字段**必须走 DataLoader/批查询**（§5.4 批量接口的读侧复用），
避免 N+1」。本仓走的是**批查询**这一支：author / column 的富集在 service 层一次性批量完成
（``content/service.py:_author_map`` / ``_column_title_map``，blog/projects/feed 同款），
而不是逐个节点查一次。

**这里守的就是那个不变式**：一次列表读的 DB 语句数必须与返回条数**无关**。这比断言实现细节
（某函数被调了几次）更贴近承诺——将来有人把 author 改成逐个节点懒查，本文件立刻变红。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.models import Column
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import create_item
from tests.conftest import AuthUser, auth_user_uid

_QUERY = """
query Items($pageSize: Int!) {
  contentItems(page: 1, pageSize: $pageSize) {
    items { id authorName columnTitle title }
    total
  }
}
"""


def _count_statements(db: AsyncSession) -> tuple[dict[str, int], Callable[[], None]]:
    """挂业务库语句计数器（只计 SELECT/DML，不含会话管理用的 BEGIN/COMMIT）。"""
    engine = db.sync_session.bind
    counter = {"n": 0}

    def _on_execute(*_args: Any, **_kwargs: Any) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _on_execute)
    return counter, lambda: event.remove(engine, "before_cursor_execute", _on_execute)


async def _seed(db: AsyncSession, auth_db: AsyncSession, count: int) -> uuid.UUID:
    """造 count 条**带专栏**的已发布内容：专栏非空才能覆盖 column_title 的批量查询路径。"""
    author: AuthUser = await auth_user_uid(
        auth_db, username="n1_author", email="n1@example.com", account_level="normal"
    )
    board = (
        await create_board_ex(db, BoardCreate(slug="n1", title="n1", description="d"), None)
    ).id
    col = Column(
        owner_id=author.id, title="专栏", description="d", slug="n1-col", board_id=board
    )
    db.add(col)
    await db.flush()
    for i in range(count):
        await create_item(
            db,
            author.id,
            ContentItemCreate(
                content_type="column_post",
                board_id=board,
                column_id=col.id,
                title=f"标题{i}",
                content="正文内容足够长以产生阅读时长",
            ),
        )
    await db.flush()
    return board


async def _post(client: Any, page_size: int) -> dict[str, Any]:
    resp = await client.post(
        "/graphql/v1", json={"query": _QUERY, "variables": {"pageSize": page_size}}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("errors") is None, body
    return body


async def test_list_reads_are_constant_in_page_size(
    client: Any, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """同一列表按 pageSize=2 与 pageSize=6 读，DB 语句数必须相同（与条数无关）。"""
    await _seed(db, auth_db, 6)

    counter, detach = _count_statements(db)
    try:
        small = await _post(client, 2)
        after_small = counter["n"]
        large = await _post(client, 6)
        after_large = counter["n"] - after_small
    finally:
        detach()

    assert len(small["data"]["contentItems"]["items"]) == 2
    assert len(large["data"]["contentItems"]["items"]) == 6
    # 条数涨 3 倍而语句数**不变** → 富集确实是批量做的，没有逐条回查
    assert after_small == after_large, (after_small, after_large)
    # 顺带守住「富集真发生了」：批量也得是非零有限次，退化成 0 说明压根没查
    assert after_small > 0


async def test_author_names_are_enriched_not_per_item(
    client: Any, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """富集结果正确：批量不能以「拿不到作者名」为代价。"""
    await _seed(db, auth_db, 3)

    body = await _post(client, 3)

    names = {i["authorName"] for i in body["data"]["contentItems"]["items"]}
    assert names == {"n1_author"}, body
