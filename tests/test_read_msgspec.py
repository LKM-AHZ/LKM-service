"""读热 msgspec 序列化（§6.5.2）：与既有 Pydantic 路径 JSON 等价 + envelope/开关/透传。"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest

from app.core.common import ApiResp
from app.core.config import settings
from app.core.err import _wrap_result
from app.core.wire import msgspec_ok
from app.modules.feed.schemas import FeedItem, FeedResponse
from app.modules.feed.wire import to_wire

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


def _item(**over: Any) -> FeedItem:
    base: dict[str, Any] = {
        "item_type": "article",
        "id": 1,
        "author_id": 7,
        "author_name": "张三",
        "title": "标题",
        "content_preview": "预览",
        "created_at": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "sort_score": 1.5,
        "board_id": 3,
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
    old = ApiResp(code=0, msg="OK", data=resp).model_dump(mode="json")
    new = json.loads(msgspec_ok(to_wire(resp)).body)
    assert new == old


def test_envelope_shape_and_snake_case() -> None:
    body = json.loads(msgspec_ok(to_wire(_PAYLOADS[1])).body)
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == 0 and body["msg"] == "OK"
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
    assert on.json() == off.json()
    assert set(on.json()) == {"code", "msg", "data"}
    assert set(on.json()["data"]) == {"items", "next_cursor"}
