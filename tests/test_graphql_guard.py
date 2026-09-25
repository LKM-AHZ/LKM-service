"""M6.4 GraphQL 防护验收：深度上限 / 字段成本上限 / 时间预算 / 硬超时 / 被拒计数与耗时指标。

口径（见 ``app/api/graphql.py`` docstring）：
- 防护阈值是**每请求**从 settings 读（扩展以类注册），故测试可 monkeypatch 阈值后经同一
  app 的 ``/graphql`` 验证——不重建 app、与线上同路径。
- 被拒计数只在 HTTP 层（``GuardedGraphQLRouter.process_result`` / 硬超时中间件）统计，
  故断言必须走 client。
- 只认防护自身文案：业务/校验错误不得计入 ``graphql_query_rejected_total``。

另含**校准守护**：默认阈值必须放行前端现有查询集（2026-09-17 实测最大深度 5），阈值被调小
到会拒真实前端查询时本测试变红。
"""

import asyncio
from types import SimpleNamespace

import pytest
import strawberry
from prometheus_client import REGISTRY

from app.core.config import settings
from tests.conftest import Client

# 前端真实查询（LKM-official-website/src/lib/api/modules/content.graphql.ts 的 CONTENT_ITEMS
# 缩略版：深度 3、字段面与线上一致，且分页实参走**变量**）：默认阈值必须放行。
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


@strawberry.type
class _Item:
    id: str


@strawberry.type
class _Group:
    @strawberry.field
    def items(self, limit: int = 20) -> list[_Item]:
        return []


@strawberry.type
class _Page:
    """本仓真实分页形态：**页对象**持有列表，分页实参在页字段上（非列表字段）。"""

    items: list[_Item]
    total: int


@strawberry.type
class _CostQuery:
    @strawberry.field
    def items(self, limit: int = 20) -> list[_Item]:
        return []

    @strawberry.field
    def groups(self, limit: int = 20) -> list[_Group]:
        return []

    @strawberry.field
    def page(self, pageSize: int = 20) -> _Page:
        return _Page(items=[], total=0)


# 专供成本计分规则本身的最小 schema：真实 schema 的字段面会随模块演进漂移，用它断言
# 「实参缩放 / 嵌套累乘」这类规则才稳定。
_COST_SCHEMA = strawberry.Schema(query=_CostQuery)


def _cost_of(query: str, variables: dict | None = None) -> int:
    """直接调用成本分析器（不经 HTTP / 不校验语义），断言计分规则本身。"""
    from graphql import parse

    from app.api.graphql import _query_cost

    ctx = SimpleNamespace(
        graphql_document=parse(query), schema=_COST_SCHEMA, variables=variables
    )
    # 预算取大值：本助手只关心「算出来多少分」，不触发提前收束
    return _query_cost(ctx, 10**9)


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


async def test_cost_limit_rejects_and_counts(client: Client, monkeypatch) -> None:
    """成本超限 → 受控错误 + rejected{reason=complexity} 递增。

    阈值压到 1：``boards`` 是无显式分页实参的列表字段，计 ``_IMPLICIT_LIST_COST``(5) > 1。
    """
    monkeypatch.setattr(settings, "graphql_max_cost", 1)
    before = _rejected("complexity")

    body = await _post(client, _SHALLOW_QUERY)

    assert body.get("data") is None
    assert any("cost budget" in e["message"] for e in body["errors"]), body
    # 文案必须走模块常量，否则 process_result 无法归类、rejected_total 恒为 0
    assert _rejected("complexity") == before + 1


def test_cost_limit_disabled_when_zero(monkeypatch) -> None:
    """``graphql_max_cost=0`` = **不注册**成本限制（本地 GraphiQL 拉 introspection 的逃生口）。"""
    from app.api.graphql import QueryCostLimiter, build_schema

    monkeypatch.setattr(settings, "graphql_max_cost", 0)
    off = build_schema().get_extensions()
    assert not any(
        e is QueryCostLimiter or isinstance(e, QueryCostLimiter) for e in off
    )

    monkeypatch.setattr(settings, "graphql_max_cost", 1000)
    on = build_schema().get_extensions()
    assert any(e is QueryCostLimiter or isinstance(e, QueryCostLimiter) for e in on)


def test_cost_scales_with_explicit_pagination_arg() -> None:
    """显式分页实参决定成本：同一字段 first:50 的成本必须高于 first:5（字面量与变量皆然）。

    这是本限流器取代「词法 token 数」代理的**核心理由**——token 数对两者几乎相同，
    而真实工作量差 10 倍。
    """
    small = _cost_of("query { items(limit: 5) { id } }")
    large = _cost_of("query { items(limit: 50) { id } }")
    assert large > small

    var_small = _cost_of("query Q($n: Int!) { items(limit: $n) { id } }", {"n": 5})
    var_large = _cost_of("query Q($n: Int!) { items(limit: $n) { id } }", {"n": 50})
    assert var_large == large  # 变量与字面量同价（变量在 on_validate 阶段已解析）
    assert var_small == small


def test_cost_compounds_through_nested_pagination() -> None:
    """嵌套分页必须**累乘**：列表套列表正是「单请求聚合过多数据」的放大形态。

    同样一条 ``groups(limit: 10)`` 之下，内层列表**带**分页实参（10×10 条）的成本必须
    显著高于内层**不带**实参（按 ``_IMPLICIT_LIST_COST`` 计价）的同形查询。
    """
    implicit = _cost_of("query { groups(limit: 10) { items { id } } }")
    explicit = _cost_of("query { groups(limit: 10) { items(limit: 10) { id } } }")

    assert explicit > implicit


def test_cost_counts_pagination_arg_on_non_list_page_field() -> None:
    """**真机验收暴露的缺陷回归**：声明条数的字段本身不是列表时，分页实参也必须计价。

    本仓形态是「``contentItems(pageSize: N)`` → 页对象 → ``items: [T!]!``」，
    若只在字段是列表时才读分页实参，``pageSize: 100000`` 会与 ``pageSize: 1`` 同价
    （实测只差几分），限流形同虚设。这里直接锁住「按 pageSize 缩放」这一条。
    """
    small = _cost_of("query { page(pageSize: 1) { items { id } total } }")
    large = _cost_of("query { page(pageSize: 100) { items { id } total } }")

    assert large > small * 10, (small, large)


async def test_timeout_budget_rejects_and_counts(client: Client, monkeypatch) -> None:
    """resolver 边界的时间预算耗尽 → 受控错误 + rejected{reason=timeout} 递增。"""
    monkeypatch.setattr(settings, "graphql_timeout_s", -1.0)  # 起跑即过期
    before = _rejected("timeout")

    body = await _post(client, _SHALLOW_QUERY)

    assert body.get("data") is None
    assert any("time budget" in e["message"] for e in body["errors"]), body
    assert _rejected("timeout") == before + 1


async def test_hard_timeout_returns_504_envelope(client: Client, monkeypatch) -> None:
    """已在 await 里的慢 resolver 必须被**硬**中断（504 + 信封），而不是无限执行。

    这是与上一条的分工：``GraphQLGuard`` 只在 resolver 边界检查，拦不住已进入 await 的
    协程；硬超时由 HTTP 层 ``asyncio.wait_for`` 兜底。
    """
    # 查询级预算调到远超兜底值：本用例要验的是「边界检查够不到时墙钟兜底仍生效」，
    # 让 GraphQLGuard 有机会先响应就测不到兜底了
    monkeypatch.setattr(settings, "graphql_timeout_s", 30.0)
    monkeypatch.setattr(settings, "graphql_hard_timeout_s", 0.2)

    async def _never_returns(*_args: object, **_kwargs: object) -> list[object]:
        await asyncio.sleep(30)
        return []

    # 解析器在模块顶层绑定了该名字，故必须打在 graphql 模块上（打 service 模块不生效）
    monkeypatch.setattr("app.modules.content.graphql.list_boards", _never_returns)
    before = _rejected("timeout")

    resp = await client.post("/graphql/v1", json={"query": _SHALLOW_QUERY})

    assert resp.status_code == 504, resp.text
    body = resp.json()
    assert body["code"] != 0 and body["message"] and body["data"] is None
    assert body["request_id"] == resp.headers["X-Request-ID"]  # 超时也留得下可检索的 id
    assert resp.headers["X-API-Version"] == "v1"  # 带版本端点的响应头
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
        ("query exceeds cost budget", "complexity"),
        ("query exceeded time budget", "timeout"),
        ("Document contains more than 1000 tokens. Parsing aborted.", None),
        ("Cannot query field 'foo' on type 'Query'.", None),
    ],
)
def test_reject_reason_classification(message: str, expected: str | None) -> None:
    from app.api.graphql import reject_reason

    assert reject_reason(message) == expected
