"""common 共享 helper 与通用响应契约测试（parse_tags / 分页 / PageData 自动 X-Total 头）。"""

from fastapi.responses import JSONResponse

from core.common import (
    PageData,
    PaginateDep,
    PaginateParams,
    paginate_offset,
    paginate_pages,
    parse_tags,
    tag_names_sequence,
)
from core.err import CommonErr, _wrap_result, resp_json


def test_parse_tags_list_passthrough() -> None:
    assert parse_tags(["a", "b"]) == ["a", "b"]


def test_parse_tags_json_string() -> None:
    assert parse_tags('["x", "y"]') == ["x", "y"]


def test_parse_tags_invalid_falls_back_empty() -> None:
    assert parse_tags(123) == []
    assert parse_tags("not-json") == []
    assert parse_tags('{"a":1}') == []  # 非列表回退


def test_paginate_math() -> None:
    assert paginate_offset(1, 20) == 0
    assert paginate_offset(3, 10) == 20
    assert paginate_pages(0, 20) == 0
    assert paginate_pages(20, 20) == 1
    assert paginate_pages(21, 20) == 2


def test_tag_names_sequence_dedups_preserving_order() -> None:
    assert tag_names_sequence(["a", "", "b", "a", "c"]) == ["a", "b", "c"]
    assert tag_names_sequence([]) == []
    assert tag_names_sequence(["", ""]) == []


def test_PageData_shape() -> None:
    data = PageData[int](items=[1, 2], total=2, page=1, pages=1)
    assert data.model_dump()["pages"] == 1


def test_pagedata_resp_includes_x_total() -> None:
    pd = PageData(items=[{"a": "1"}], total=37, page=1, pages=2)
    resp = _wrap_result(pd)
    assert isinstance(resp, JSONResponse)
    assert resp.headers["X-Total"] == "37"


def test_pagedata_resp_x_total_zero_total() -> None:
    resp = _wrap_result(PageData(items=[], total=0, page=1, pages=0))
    assert isinstance(resp, JSONResponse)
    assert resp.headers["X-Total"] == "0"


def test_wrap_result_non_pagedata_no_x_total() -> None:
    resp = _wrap_result([1, 2, 3])
    assert "X-Total" not in resp.headers


def test_resp_json_ok_data() -> None:
    resp = resp_json(CommonErr.OK, data={"x": 1})
    assert resp.status_code == 200


async def test_paginate_defaults() -> None:
    dep = PaginateDep()
    p: PaginateParams = await dep(page=1, limit=20)
    assert p.page == 1
    assert p.limit == 20
    assert p.offset == 0


async def test_paginate_offset_calc() -> None:
    p: PaginateParams = await PaginateDep()(page=3, limit=10)
    assert p.offset == 20
