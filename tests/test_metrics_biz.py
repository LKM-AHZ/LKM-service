"""M0.5.2 业务指标：post_created_total / notify_failed_total 接线断言。

验收声明（相对增量，同 M0.5.1 避免绝对等值——全局默认 REGISTRY 跨测试/跨文件累积）：
- 发布一帖（成功走到 create_item 的返回路径）→ post_created_total{content_type="discussion"} +1；
- post_created_total 按 content_type 分系列；notify_failed_total 亦已注册（占位在 /metrics 可见）。
"""

import uuid

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content import service as content_service
from app.modules.content.schemas import ContentItemCreate


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """需 auth(user)+biz(board/content) 单 schema（融合）。"""
    return fused_db_session


def _sample_value(name: str, labels: dict[str, str]) -> float:
    """取 REGISTRY 某 series 现值；series 未出现过视为 0（不抛错）。"""
    val = REGISTRY.get_sample_value(name, labels)
    return val if val is not None else 0.0


async def _discussion_created(
    db: AsyncSession, author_id: uuid.UUID | None = None
) -> None:
    """直接调统一 content/service.create_item 成功建一帖（真实计数链路之一）。

    content_items.board_id(强 FK)+author_id(设了就需存在) 在 PG 是被校验的外键：
    裸 board_id / author_id 而 schema 无真实 board/user 会被 FK 拦截。
    这里先落真实 board + 作者，取其 uuid 主键作父。
    """
    from app.modules.auth.models import User
    from app.modules.content.models import Board

    board = Board(slug="m", title="指标板", description="")
    db.add(board)
    await db.flush()
    if author_id is None:
        author = User(username="metric-author", hashed_password="x")
        db.add(author)
        await db.flush()
        author_id = author.id
    item = await content_service.create_item(
        db,
        author_id,
        ContentItemCreate(
            board_id=board.id,
            title="指标帖",
            content="post_created_total 递增验证",
            tags=["metrics"],
        ),
    )
    assert item.content_type == "discussion"
    assert item.status == "published"


async def test_post_created_total_increments_on_publish(
    db: AsyncSession, monkeypatch
) -> None:
    # 聚焦“成功发帖→+1”，绕过鉴权：post_allowed 让行。
    # （批 3 后 board 存在性走 BoardRepository.get_or_raise，不再有模块级 get_or_raise
    #   可 patch；_discussion_created 本就落真实 board，无需替身。）
    async def _allow(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.modules.content.boards.service.check_post_allowed", _allow)

    before = _sample_value("post_created_total", {"content_type": "discussion"})

    await _discussion_created(db)

    after = _sample_value("post_created_total", {"content_type": "discussion"})
    assert after == before + 1


async def test_biz_metric_series_registered(client, db) -> None:
    # 在文本层断言 M0.5.2 两处业务计数对 /metrics 暴露文本可见：
    # - post_created_total 需前一手真实发帖样本（本进程既有 create_item +1），故文本可见其 sample 行；
    # - notify_failed_total 尚无投递失败样本时 Prometheus 文本不输出其行，这里改从 REGISTRY
    #   采集器侧面确认其已注册（prometheus_client 内部对 *_total 命名剥离后缀存为 'notify_failed'）。
    # prometheus_client 会为 Counter「名称，内部把 *_total 后缀剥离」。断言后端真名已注册。
    metric_names = {m.name for m in REGISTRY.collect() if m.type == "counter"}
    assert "post_created" in metric_names
    assert "notify_failed" in metric_names

    body = (await client.get("/metrics")).text
    # 有真实发帖样本后文本可见完整 *_total 名称
    assert "post_created_total" in body
