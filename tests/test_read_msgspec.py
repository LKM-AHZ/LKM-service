"""读热 msgspec 序列化（§6.5.2）：与既有 Pydantic 路径 JSON 等价 + envelope/开关/透传。"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import pytest

from app.core.common import ApiResp, PageData
from app.core.config import settings
from app.core.err import _wrap_result
from app.core.wire import msgspec_ok
from app.modules.feed.schemas import FeedItem, FeedResponse
from app.modules.feed.wire import to_wire
from app.modules.files.schemas import FileInfo
from app.modules.files.wire import to_wire as files_to_wire
from app.modules.interaction.schemas import FavoriteItem, HistoryItem
from app.modules.interaction.wire import favorites_to_wire, history_to_wire
from app.modules.notification.schemas import NotificationOut
from app.modules.notification.wire import to_wire as notification_to_wire
from app.modules.search.schemas import SearchHit
from app.modules.search.wire import to_wire as search_to_wire

_EXPECTED_KEYS = {
    "item_type",
    "id",
    "author_id",
    "author_name",
    "title",
    "content_preview",
    "created_at",
    "sort_score",
    "board_id",
    "url",
}

_ITEM_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """剔除 request_id 后的信封——它每请求生成，跨两次请求比对时必须排除。

    该字段由 ``test_request_id.py`` 专门守（与 X-Request-ID 头同值），这里只管
    「除它以外两条序列化路径逐字段相同」。
    """
    return {k: v for k, v in body.items() if k != "request_id"}
_AUTHOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000007")
_BOARD_ID = uuid.UUID("00000000-0000-7000-8000-000000000003")


def _item(**over: Any) -> FeedItem:
    base: dict[str, Any] = {
        "item_type": "article",
        "id": _ITEM_ID,
        "author_id": _AUTHOR_ID,
        "author_name": "张三",
        "title": "标题",
        "content_preview": "预览",
        "created_at": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "sort_score": 1.5,
        "board_id": _BOARD_ID,
        "url": "/content/1",
    }
    base.update(over)
    return FeedItem(**base)


_PAYLOADS = [
    FeedResponse(items=[], next_cursor=None),
    FeedResponse(items=[_item()], next_cursor="Y3Vyc29y|42"),
    FeedResponse(items=[_item(author_id=None, board_id=None)], next_cursor=None),
    FeedResponse(
        items=[
            _item(
                created_at=datetime.datetime(2026, 1, 2, 3, 4, 5),
                sort_score=-1e20,
            )
        ],
        next_cursor=None,
    ),
    FeedResponse(
        items=[
            _item(
                item_type="qa",
                author_name="emoji 😀",
                title='换行\n引号"',
                sort_score=0.1,
                created_at=datetime.datetime(
                    2026,
                    1,
                    2,
                    3,
                    4,
                    5,
                    123456,
                    tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
                ),
            )
        ],
        next_cursor=None,
    ),
]


@pytest.mark.parametrize("resp", _PAYLOADS)
def test_msgspec_json_equivalent_to_pydantic(resp: FeedResponse) -> None:
    """msgspec 输出与既有 model_dump(mode="json") 解析后逐字段等价。"""
    old = ApiResp(code=0, message="OK", data=resp).model_dump(mode="json")
    new = json.loads(msgspec_ok(to_wire(resp)).body)
    assert new == old


def test_envelope_shape_and_snake_case() -> None:
    body = json.loads(msgspec_ok(to_wire(_PAYLOADS[1])).body)
    assert set(body) == {"code", "message", "data", "request_id"}
    assert body["code"] == 0 and body["message"] == "OK"
    assert set(body["data"]) == {"items", "next_cursor"}
    assert set(body["data"]["items"][0]) == _EXPECTED_KEYS  # 无 camelCase 字段


def test_wrap_result_passes_through_response() -> None:
    resp = msgspec_ok({"x": 1})
    assert _wrap_result(resp) is resp


async def test_endpoint_msgspec_matches_legacy_path(
    client: Any, monkeypatch: Any
) -> None:
    """GET /timeline 开关开/关两条路径 200 且 JSON 等价、字段保持 snake_case。"""
    monkeypatch.setattr(settings, "read_msgspec_enabled", True)
    on = await client.get("/api/v1/timeline", params={"mode": "hot"})
    monkeypatch.setattr(settings, "read_msgspec_enabled", False)
    off = await client.get("/api/v1/timeline", params={"mode": "hot"})

    assert on.status_code == off.status_code == 200
    # request_id 是**每请求**生成的，两次请求本就不同 → 比对时剔掉它，其余必须逐字段相同
    assert _without_request_id(on.json()) == _without_request_id(off.json())
    assert set(on.json()) == {"code", "message", "data", "request_id"}
    assert on.json()["request_id"] and off.json()["request_id"]
    assert set(on.json()["data"]) == {"items", "next_cursor"}


# ---- B6c：search 端点扩面（命中列表，与 timeline 同类收益面）----


def _hit(**over: Any) -> SearchHit:
    base: dict[str, Any] = {
        "id": _ITEM_ID,
        "content_type": "discussion",
        "board_id": _BOARD_ID,
        "title": "黎曼猜想",
        "excerpt": "从直觉理解",
        "slug": None,
        "author_id": _AUTHOR_ID,
        "author_name": "张三",
        "like_count": 3,
        "comment_count": 1,
        "view_count": 42,
        "published_at": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "created_at": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    }
    base.update(over)
    return SearchHit(**base)


_SEARCH_PAGES = [
    PageData[SearchHit](items=[], total=0, page=1, pages=0),
    PageData[SearchHit](items=[_hit()], total=1, page=1, pages=1),
    PageData[SearchHit](
        items=[_hit(slug="riemann", author_id=None, published_at=None)],
        total=1,
        page=1,
        pages=1,
    ),
    PageData[SearchHit](
        items=[_hit(author_name="emoji 😀", title='引号"与换行\n', view_count=0)],
        total=1,
        page=1,
        pages=1,
    ),
]


@pytest.mark.parametrize("page", _SEARCH_PAGES)
def test_search_msgspec_json_equivalent(page: PageData[SearchHit]) -> None:
    old = ApiResp(code=0, message="OK", data=page).model_dump(mode="json")
    new = json.loads(msgspec_ok(search_to_wire(page)).body)
    assert new == old


async def test_search_endpoint_msgspec_matches_legacy_path(
    client: Any, monkeypatch: Any
) -> None:
    """GET /search 开关开/关两条路径 200 且 JSON 等价。"""
    monkeypatch.setattr(settings, "read_msgspec_enabled", True)
    on = await client.get("/api/v1/search", params={"q": "黎曼"})
    monkeypatch.setattr(settings, "read_msgspec_enabled", False)
    off = await client.get("/api/v1/search", params={"q": "黎曼"})

    assert on.status_code == off.status_code == 200
    # 同上：request_id 每请求不同，剔掉后比对
    assert _without_request_id(on.json()) == _without_request_id(off.json())


# ---- B6c：interaction / notification / files 列表扩面（纯 JSON 等价）----

_DT = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)


def _assert_equivalent(page: Any, wire: Any) -> None:
    old = ApiResp(code=0, message="OK", data=page).model_dump(mode="json")
    new = json.loads(msgspec_ok(wire).body)
    assert new == old


def test_interaction_favorites_json_equivalent() -> None:
    page = PageData[FavoriteItem](
        items=[
            FavoriteItem(
                content_id=_ITEM_ID,
                content_type="discussion",
                title="标题",
                slug=None,
                board_id=_BOARD_ID,
                created_at=_DT,
            )
        ],
        total=1,
        page=1,
        pages=1,
    )
    _assert_equivalent(page, favorites_to_wire(page))


def test_interaction_history_json_equivalent() -> None:
    page = PageData[HistoryItem](
        items=[
            HistoryItem(
                content_id=_ITEM_ID,
                content_type="article",
                title="标题",
                slug="slug-1",
                board_id=_BOARD_ID,
                viewed_at=_DT,
            )
        ],
        total=1,
        page=2,
        pages=3,
    )
    _assert_equivalent(page, history_to_wire(page))


def test_notification_json_equivalent() -> None:
    page = PageData[NotificationOut](
        items=[
            NotificationOut(
                id=_ITEM_ID,
                type="like",
                actor_id=_AUTHOR_ID,
                target_id=None,
                payload={"k": "v", "n": 1},
                read_at=None,
                created_at=_DT,
            )
        ],
        total=1,
        page=1,
        pages=1,
    )
    _assert_equivalent(page, notification_to_wire(page))


def test_files_json_equivalent() -> None:
    page = PageData[FileInfo](
        items=[
            FileInfo(
                id=_ITEM_ID,
                original_name="a.pdf",
                uploader_id=_AUTHOR_ID,
                mime_type="application/pdf",
                size=123,
                category_id="cat",
                description="说明",
                tags=["x"],
                status="approved",
                download_count=3,
                view_count=4,
                created_at=_DT,
            )
        ],
        total=1,
        page=1,
        pages=1,
    )
    _assert_equivalent(page, files_to_wire(page))
