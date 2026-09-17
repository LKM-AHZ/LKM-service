"""GraphQL 聚合（§7）+ 防护（M6.4）：把各模块经 registry 暴露的 Query 类 merge 成统一 schema。

原聚合逻辑在 main.py（merge_types 7 个 Query 类），P5 收敛到此，main 只装配。
新增模块的 GraphQL 查询：模块 __init__.py 暴露 ``GRAPHQL``，registry 自动聚合。

防护三层（阈值见 ``core.config``，默认值由前端现有查询集实测校准后写死）：

1. **深度上限** ``QueryDepthLimiter``（``LKM_GRAPHQL_MAX_DEPTH``）：挡住深层嵌套的放大查询；
2. **文档规模上限** ``MaxTokensLimiter``（``LKM_GRAPHQL_MAX_TOKENS``）：strawberry 不提供成本
   分析器，故以词法 token 数作「复杂度/成本」的代理——同量级下文档越大，校验与解析成本越高；
3. **时间预算** ``GraphQLGuard``（``LKM_GRAPHQL_TIMEOUT_S``）：预算耗尽后拒答后续 resolver，
   使查询以受控错误收束而非把 worker 拖满。

**已知局限**（蓝图「查询级超时」的落地口径）：时间预算在**每个 resolver 边界**检查，无法中断
单个已在 ``await`` 中的 resolver（Python 无抢占式取消同步/已进入的协程）。故它保证的是
「扇出型慢查询尽快收束」，而不是「墙钟硬超时」——后者应由网关超时（APISIX）兜底。

被拒计数与耗时观测：耗时为 ``GraphQLGuard`` 的 per-request 观测；**被拒分类统一在 HTTP 层**
（``GuardedGraphQLRouter.process_result``）按错误消息归类，避免两处统计同一拒绝。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import strawberry
from graphql import GraphQLError
from strawberry.extensions import MaxTokensLimiter, QueryDepthLimiter
from strawberry.extensions.base_extension import SchemaExtension
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import merge_types

from app.core.config import settings
from app.core.metrics import (
    graphql_query_duration_seconds,
    graphql_query_rejected_total,
)
from app.modules import registry

# 时间预算耗尽的固定文案：process_result 依此识别并归类为 reason=timeout（前端可据文案提示重试）。
TIMEOUT_MESSAGE = "query exceeded time budget"

# 错误消息标记 → 拒绝原因：与防护实现同源（strawberry/graphql-core 的既有文案）。
_REASON_MARKERS: tuple[tuple[str, str], ...] = (
    ("maximum operation depth", "depth"),
    ("tokens", "complexity"),
    (TIMEOUT_MESSAGE, "timeout"),
)


class GraphQLGuard(SchemaExtension):
    """查询级时间预算 + 耗时观测（每请求实例化：schema 以**类**形式注册扩展）。

    - ``on_operation``：记起点与截止时刻，收束时把耗时写入 histogram（被拒的查询也计入）。
    - ``resolve``：每个字段解析前检查预算；耗尽即抛 ``GraphQLError(TIMEOUT_MESSAGE)``
      —— 由 GraphQL 引擎收成 `errors`（HTTP 仍是 200，非 500、非栈），前端可见受控错误。
    """

    def on_operation(self) -> Iterator[None]:
        started = time.perf_counter()
        self._deadline = started + settings.graphql_timeout_s
        try:
            yield
        finally:
            graphql_query_duration_seconds.observe(time.perf_counter() - started)

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: str,
        **kwargs: Any,
    ) -> Any:
        # 原样返回 _next 的结果（sync/async 混合由 strawberry 处理，不能在此强制 await：
        # 同步 resolver 返回 list/dict 时 await 会抛 "object list can't be used in 'await'"）。
        if time.perf_counter() > getattr(self, "_deadline", float("inf")):
            raise GraphQLError(TIMEOUT_MESSAGE)
        return _next(root, info, *args, **kwargs)


def reject_reason(message: str) -> str | None:
    """错误消息 → 防护拒绝原因（depth/complexity/timeout）；非防护错误返回 None。

    只认防护自身的文案，故业务 resolver 的执行错误不会污染 ``rejected_total``。
    """
    low = message.lower()
    for marker, reason in _REASON_MARKERS:
        if marker in low:
            return reason
    return None


def record_rejections(result: Any) -> None:
    """按错误归类统计防护拒绝（HTTP 层单一入口，见模块 docstring）。

    **按请求去重**：深度/规模校验对同一操作可能为每个越界字段各报一条错误，逐条 inc 会把
    「一次被拒请求」放大成 N 次。指标语义是「被拒**请求**数」，故每请求每原因最多计 1。
    """
    reasons = {
        reason
        for err in (getattr(result, "errors", None) or [])
        if (reason := reject_reason(getattr(err, "message", "") or "")) is not None
    }
    for reason in reasons:
        graphql_query_rejected_total.labels(reason).inc()


class GuardedGraphQLRouter(GraphQLRouter):
    """``GraphQLRouter`` + 防护拒绝计数：在响应生成前统计被拒原因。

    计数只覆盖 HTTP 入口（``/graphql``）——测试里直接 ``schema.execute`` 不经此处，
    故防护拒绝的计数断言应经 client 打接口（与线上同路径）。
    """

    async def process_result(self, request: Any, result: Any) -> Any:
        record_rejections(result)
        return await super().process_result(request, result)


def _all_graphql_types() -> list[type[Any]]:
    types: list[type[Any]] = []
    for name in registry.MODULES:
        types.extend(registry.graphql_of(name))
    return types


def build_schema() -> strawberry.Schema:
    """合并全部模块 GraphQL Query/Mutation 类，构建带防护扩展的 schema。

    扩展以**工厂/类**形式注册（非实例）：strawberry 每请求实例化，避免
    ``GraphQLGuard`` 的计时状态跨并发请求串台（传实例已在新版被标记为弃用）。
    """
    classes = tuple(_all_graphql_types())
    merged_query = merge_types("Query", classes)  # type: ignore[arg-type]
    extensions: list[Any] = [
        lambda: QueryDepthLimiter(max_depth=settings.graphql_max_depth),
    ]
    # ``graphql_max_tokens <= 0`` = 不注册规模限制（供本地 GraphiQL 拉 introspection 等大文档；
    # 生产不建议关闭——这是唯一挡超大文档的一层）。
    if settings.graphql_max_tokens > 0:
        extensions.append(
            lambda: MaxTokensLimiter(max_token_count=settings.graphql_max_tokens)
        )
    extensions.append(GraphQLGuard)
    return strawberry.Schema(query=merged_query, extensions=extensions)
