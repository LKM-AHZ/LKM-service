"""外部检索引擎（B2）：工厂择一、读路径分支与 fail-open、事件同步、全量重建。

全部用 ``FakeSearchEngine``（内存替身）驱动，不需要 Meilisearch / OpenSearch 容器——
真机连通属部署验证（受阻于出网时登记为未真机，见路线图 §8）。
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.metrics import search_engine_fallback_total
from app.flows import search_reindex_body
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import create_item, delete_item
from app.modules.search import service as search_service
from app.modules.search import sync as search_sync
from app.modules.search.engines import factory as engine_factory
from app.modules.search.engines.meili import MeiliSearchEngine
from tests.conftest import AuthUser, auth_user_uid
from tests.fakes import FakeSearchEngine


@pytest.fixture(autouse=True)
def _reset_engine_cache() -> Iterator[None]:
    """``get_engine`` 带进程内缓存：每例前后清掉，免得 monkeypatch 的配置被上一例缓存污染。"""
    engine_factory.reset_engine_cache()
    yield
    engine_factory.reset_engine_cache()


async def _au(auth_db: AsyncSession, username: str = "searcher") -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level="normal",
    )


async def _make_board(db: AsyncSession, slug: str) -> uuid.UUID:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _make_item(
    db: AsyncSession, auth_db: AsyncSession, *, title: str, content: str = "正文"
) -> uuid.UUID:
    # 用户名/板块 slug 都取随机后缀：同一测试内可能造多条，固定值会撞 users_username_key
    au = await _au(auth_db, f"u{uuid.uuid4().hex[:8]}")
    bid = await _make_board(db, f"b-{uuid.uuid4().hex[:8]}")
    info = await create_item(
        db, au.id, ContentItemCreate(board_id=bid, title=title, content=content)
    )
    return info.id


# ---- 工厂择一 ----


def test_factory_returns_none_for_pg() -> None:
    assert settings.search_engine == "pg"
    assert engine_factory.get_engine() is None


def test_factory_none_when_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "search_engine", "meilisearch")
    monkeypatch.setattr(settings, "search_meili_url", "")
    assert engine_factory.get_engine() is None


def test_factory_builds_meilisearch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "search_engine", "meilisearch")
    monkeypatch.setattr(settings, "search_meili_url", "http://meili:7700")
    monkeypatch.setattr(settings, "search_meili_index", "content")
    engine = engine_factory.get_engine()
    assert isinstance(engine, MeiliSearchEngine)
    assert engine.name == "meilisearch"


def test_factory_builds_opensearch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "search_engine", "opensearch")
    monkeypatch.setattr(settings, "search_opensearch_url", "http://os:9200")
    engine = engine_factory.get_engine()
    assert engine is not None
    assert engine.name == "opensearch"


# ---- 读路径：引擎命中 + DB 权威回填 + fail-open ----


async def test_engine_hits_are_backfilled_and_filtered(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = await _make_item(db, auth_db, title="黎曼猜想入门")
    engine = FakeSearchEngine()
    engine.docs[str(item_id)] = {
        "id": str(item_id),
        "content_type": "discussion",
        "title": "黎曼猜想入门",
    }
    # 引擎里还有一条 DB 不存在（已硬删/陈旧）的命中：回填时必须被过滤掉
    ghost = str(uuid.uuid4())
    engine.docs[ghost] = {
        "id": ghost,
        "content_type": "discussion",
        "title": "黎曼猜想幽灵",
    }
    monkeypatch.setattr(search_service, "get_engine", lambda: engine)

    page = await search_service.search_items(db, "黎曼猜想")
    assert [h.id for h in page.items] == [item_id]


async def test_engine_failure_falls_back_to_pg(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_item(db, auth_db, title="费马大定理速览")
    engine = FakeSearchEngine(fail_on="search_ids")
    monkeypatch.setattr(search_service, "get_engine", lambda: engine)
    before = search_engine_fallback_total.labels("fake")._value.get()

    page = await search_service.search_items(db, "费马大定理")

    assert page.total == 1  # 结果来自 PG 兜底
    after = search_engine_fallback_total.labels("fake")._value.get()
    assert after == pytest.approx(before + 1)


# ---- 事件同步：可见性口径 ----


async def test_sync_upserts_visible_and_deletes_invisible(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    engine = FakeSearchEngine()
    au = await _au(auth_db, "syncer")
    bid = await _make_board(db, "sync-board")
    info = await create_item(
        db, au.id, ContentItemCreate(board_id=bid, title="可见内容", content="正文")
    )
    item_id = info.id

    assert await search_sync.sync_item(db, engine, item_id) == "upsert"
    assert engine.docs[str(item_id)]["author_name"] == "syncer"

    await delete_item(db, item_id, au.id)
    # 软删后：行仍在（include_deleted 可见），但必须从索引移除
    assert await search_sync.sync_item(db, engine, item_id) == "delete"
    assert str(item_id) not in engine.docs


# ---- 全量重建 ----


async def test_rebuild_index_drops_ensures_and_batches(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [await _make_item(db, auth_db, title=f"回填目标 {n}") for n in range(3)]
    engine = FakeSearchEngine()
    monkeypatch.setattr(search_reindex_body, "get_engine", lambda: engine)

    result = await search_reindex_body.rebuild_index(batch_size=1, db=db)

    assert result == {"engine": "fake", "indexed": 3}
    assert engine.ensured == 1
    assert engine.dropped == 1
    assert set(engine.docs) == {str(i) for i in ids}


async def test_rebuild_index_noop_without_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_reindex_body, "get_engine", lambda: None)
    result = await search_reindex_body.rebuild_index()
    assert result["engine"] is None
    assert result["indexed"] == 0
