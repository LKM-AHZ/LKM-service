"""M6.4 GraphQL 防护验收：深度上限 / 文档规模上限 / 时间预算 / 被拒计数与耗时指标。

口径（见 ``app/api/graphql.py`` docstring）：
- 防护阈值是**每请求**从 settings 读（扩展以工厂/类注册），故测试可 monkeypatch 阈值后
  经同一 app 的 ``/graphql`` 验证——不重建 app、与线上同路径。
- 被拒计数只在 HTTP 层（``GuardedGraphQLRouter.process_result``）统计，故断言必须走 client。
- 只认防护自身文案：业务/校验错误不得计入 ``graphql_query_rejected_total``。

另含**校准守护**：默认阈值必须放行前端现有查询集（2026-09-17 实测最大深度 5、最大文档
≈70 token，见路线图 §8 #31），阈值被调小到会拒真实前端查询时本测试变红。
"""

import pytest
from prometheus_client import REGISTRY

from app.core.config import settings
from tests.conftest import Client

# 前端真实查询（LKM-official-website/src/lib/api/modules/content.graphql.ts 的 CONTENT_ITEMS
# 缩略版：深度 3、字段面与线上一致）：默认阈值必须放行。
_FRONTEND_QUERY = """
query ContentItems($page: Int!, $pageSize: Int!) {
  contentItems(page: $page, pageSize: $pageSize) {
    items {
      id
      title
      contentType
      likeCount
      createdAt
    }
    total
    page
    pages
  }
}
"""

_SHALLOW_QUERY = "query { boards { id slug title } }"


def _rejected(reason: str) -> float:
    """读防护拒绝计数；从未出现过该 label 时视为 0。"""
    val = REGISTRY.get_sample_value(
        "graphql_query_rejected_total", {"reason": reason}
    )
    return val if val is not None else 0.0


def _duration_count() -> float:
    val = REGISTRY.get_sample_value("graphql_query_duration_seconds_count")
    return val if val is not None else 0.0


async def _post(client: Client, query: str, variables: dict | None = None) -> dict:
    resp = await client.post("/graphql", json={"query": query, "variables": variables})
    assert resp.status_code == 200, resp.text  # 防护拒绝是受控错误，不是 5xx
    return resp.json()


async def test_default_thresholds_allow_frontend_queries(client: Client) -> None:
    """默认阈值放行前端真实查询（校准守护）：无 errors，且耗时指标有观测。"""
    before = _duration_count()

    body = await _post(client, _FRONTEND_QUERY, {"page": 1, "pageSize": 10})
    assert body.get("errors") is None, body
    assert body["data"]["contentItems"] is not None
    assert _duration_count() > before  # 每次操作都观测耗时


async def test_depth_limit_rejects_and_counts(client: Client, monkeypatch) -> None:
    """深度超限 → 受控错误（HTTP 200 + errors，非栈）+ rejected{reason=depth} 递增。

    阈值压到 0：按 strawberry 口径叶字段不计深度，``boards { id }`` 深度为 1 > 0 即被拒；
    这样无需构造深查询就能验证「深度检查确实在链路上生效」。
    """
    monkeypatch.setattr(settings, "graphql_max_depth", 0)
    before = _rejected("depth")

    body = await _post(client, _SHALLOW_QUERY)

    assert body.get("data") is None
    msgs = [e["message"] for e in body["errors"]]
    assert any("depth" in m.lower() for m in msgs), msgs
    assert _rejected("depth") == before + 1


async def test_complexity_limit_rejects_and_counts(client: Client, monkeypatch) -> None:
    """文档规模（token）超限 → 受控错误 + rejected{reason=complexity} 递增。"""
    monkeypatch.setattr(settings, "graphql_max_tokens", 10)
    before = _rejected("complexity")

    body = await _post(client, _FRONTEND_QUERY)

    assert body.get("data") is None
    assert any("token" in e["message"].lower() for e in body["errors"]), body
    assert _rejected("complexity") == before + 1


def test_complexity_limit_disabled_when_zero(monkeypatch) -> None:
    """``graphql_max_tokens=0`` = **不注册**规模限制（本地 GraphiQL 拉 introspection 的逃生口）。

    该项在 schema 装配期决定（限制器实例属性固定），故在 build_schema 层面断言扩展构成。
    """
    from strawberry.extensions import MaxTokensLimiter

    from app.api.graphql import build_schema

    monkeypatch.setattr(settings, "graphql_max_tokens", 0)
    off = build_schema().get_extensions()
    assert not any(isinstance(e, MaxTokensLimiter) for e in off)

    monkeypatch.setattr(settings, "graphql_max_tokens", 1000)
    on = build_schema().get_extensions()
    assert any(isinstance(e, MaxTokensLimiter) for e in on)


async def test_timeout_budget_rejects_and_counts(client: Client, monkeypatch) -> None:
    """时间预算耗尽 → 受控错误 + rejected{reason=timeout} 递增。"""
    monkeypatch.setattr(settings, "graphql_timeout_s", -1.0)  # 起跑即过期
    before = _rejected("timeout")

    body = await _post(client, _SHALLOW_QUERY)

    assert body.get("data") is None
    assert any("time budget" in e["message"] for e in body["errors"]), body
    assert _rejected("timeout") == before + 1


async def test_business_error_not_counted_as_rejection(
    client: Client, monkeypatch
) -> None:
    """业务/校验类错误不得计入防护拒绝（rejected_total 只记防护自身）。"""
    depth, complexity, timeout = (
        _rejected("depth"),
        _rejected("complexity"),
        _rejected("timeout"),
    )

    body = await _post(client, "query { noSuchField }")

    assert body.get("errors"), body  # 校验失败（字段不存在）
    assert (
        _rejected("depth"),
        _rejected("complexity"),
        _rejected("timeout"),
    ) == (depth, complexity, timeout)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("'x' exceeds maximum operation depth of 10", "depth"),
        ("Document contains more than 1000 tokens. Parsing aborted.", "complexity"),
        ("query exceeded time budget", "timeout"),
        ("Cannot query field 'foo' on type 'Query'.", None),
    ],
)
def test_reject_reason_classification(message: str, expected: str | None) -> None:
    from app.api.graphql import reject_reason

    assert reject_reason(message) == expected
