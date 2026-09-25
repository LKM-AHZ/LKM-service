"""GraphQL 聚合（§7）+ 防护（M6.4）：把各模块经 registry 暴露的 Query 类 merge 成统一 schema。

原聚合逻辑在 main.py（merge_types 7 个 Query 类），P5 收敛到此，main 只装配。
新增模块的 GraphQL 查询：模块 __init__.py 暴露 ``GRAPHQL``，registry 自动聚合。

**多端点版本化（§2）**：不采用「单端点 + `@deprecated` 缓冲」（移动端版本碎片化下守不住，
必然堆成废弃字段的大泥球），改为**版本即端点**——``GRAPHQL_VERSIONS`` 登记每个版本由哪些
模块贡献 Query 类，``build_schema(version)`` 各自构建**完全独立的 schema**，main 逐个挂到
``{graphql_path}/{version}``。破坏性变更 = 加一条版本记录 + 一条 APISIX 路由，旧端点保留运行；
网关按 ``X-API-Version`` 把流量分流到对应端点（``deploy/apisix/apisix.yaml``）。

防护三层（阈值见 ``core.config``，默认值由前端现有查询集实测校准后写死）：

1. **深度上限** ``QueryDepthLimiter``（``LKM_GRAPHQL_MAX_DEPTH``）：挡住深层嵌套的放大查询；
2. **成本上限** ``QueryCostLimiter``（``LKM_GRAPHQL_MAX_COST``）：按 schema 真实的字段/列表
   规模计分（见该类 docstring）。取代早先以**词法 token 数**代理复杂度的 ``MaxTokensLimiter``
   ——token 数只反映文档有多大，与「这次查询会让后端取多少数据」无关，一个
   ``items(first: 1000)`` 几乎不增 token 却把工作量放大千倍；
3. **时间预算** ``GraphQLGuard``（``LKM_GRAPHQL_TIMEOUT_S``）：预算耗尽后拒答后续 resolver，
   使查询以受控错误收束而非把 worker 拖满。

**已知局限**（蓝图「查询级超时」的落地口径）：时间预算在**每个 resolver 边界**检查，无法中断
单个已在 ``await`` 中的 resolver（Python 无抢占式取消同步/已进入的协程）。故它保证的是
「扇出型慢查询尽快收束」；**墙钟硬超时**由 HTTP 层的 ``core.middleware.GraphQLTimeoutMiddleware``
（``asyncio.wait_for`` + 504）兜底。

被拒计数与耗时观测：耗时为 ``GraphQLGuard`` 的 per-request 观测；**被拒分类统一在 HTTP 层**
（``GuardedGraphQLRouter.process_result``）按错误消息归类，避免两处统计同一拒绝。

**关于 DataLoader（§2 第 4 条「列表字段必须走 DataLoader/批查询」）**：本仓走的是该条并列的
**批查询**这一支——author / column 的富集在 service 层一次性批量完成（``content.service``
的 ``_author_map`` / ``_column_title_map``，blog / projects / feed 同款），列表读的 DB 语句数
与返回条数无关。**刻意不再叠一层 strawberry DataLoader**：service 的预批量已发生，在其上再加
loader 只会对同一批 id 多跑一次批量查询（净负收益），而不会减少任何 DB 往返。该不变式由
``tests/test_graphql_no_n_plus_1.py`` 守住（按语句计数断言与条数无关）——将来有人把某个嵌套
字段改成逐节点懒查，那个测试会立刻变红。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import strawberry
from graphql import (
    GraphQLError,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
)
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    VariableNode,
)
from strawberry.extensions import QueryDepthLimiter
from strawberry.extensions.base_extension import SchemaExtension
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import merge_types
from strawberry.utils.await_maybe import await_maybe

from app.core.config import settings
from app.core.metrics import (
    graphql_query_duration_seconds,
    graphql_query_rejected_total,
)
from app.modules import registry

# 时间预算耗尽的固定文案：process_result 依此识别并归类为 reason=timeout（前端可据文案提示重试）。
TIMEOUT_MESSAGE = "query exceeded time budget"

# 成本超预算的固定文案（同上：分类靠模块级常量而非模糊子串匹配）。
COST_MESSAGE = "query exceeds cost budget"

# 错误消息标记 → 拒绝原因：与防护实现同源。
# 深度用 graphql-core 的既有文案；成本与超时各用本模块的常量——用自定文案而非模糊子串，
# 业务报错（如 "invalid refresh tokens"）就不会被误算成被拒、污染 rejected_total。
_REASON_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("maximum operation depth",), "depth"),
    ((COST_MESSAGE,), "complexity"),
    ((TIMEOUT_MESSAGE,), "timeout"),
)

# ---- 成本模型常数（§2 第 2 条：field cost 而非词法代理）----
# 每个被选中字段的基础分。
_COST_PER_FIELD = 1
# **无显式分页实参**的列表字段：无法从 AST 得知实际条数，按一个温和常量计分，且**不向下累乘**
# （父字段的分页实参才是真正决定条数的那个；父子都乘会把「contentItems.pageSize × items」
# 这类同一批行重复计价，正常前端查询瞬间超限）。
_IMPLICIT_LIST_COST = 5
# 列表字段分页实参的上限封顶：客户端写 first: 10**9 不该让分数溢出（超出部分由服务端分页夹住）。
_MAX_LIST_MULTIPLIER = 100
# 分页实参名：覆盖本仓 REST/GQL 的分页命名（limit/first/page_size/pageSize/max_results）。
_LIST_LIMIT_ARGS = ("limit", "first", "page_size", "pageSize", "max_results")
# 遍历阶段的硬上限：超限即提前收束（不必算完整个文档），同时兜住分数溢出。
_COST_HARD_CAP = 1_000_000

# ---- 多端点版本化（§2）----
# 版本 → 贡献 Query 类的模块清单。架构级破坏（删字段/改类型/收缩返回/改枚举语义）时**新增**
# 一条记录（如 "v2"），旧端点原样保留服务存量客户端；网关按 X-API-Version 分流。
GRAPHQL_VERSIONS: dict[str, tuple[str, ...]] = {"v1": registry.MODULES}
# 无版本路径 ``/graphql`` 指向的版本（等价 REST 的「不带版本 = 最新」，见 app.main 的挂载）。
GRAPHQL_DEFAULT_VERSION = "v1"


class QueryCostLimiter(SchemaExtension):
    """查询成本上限：按 schema 真实的**声明条数**给分，超 ``settings.graphql_max_cost`` 即拒答。

    计分规则（在 ``on_validate`` 阶段跑，此时文档已解析、变量已解析）：

    - 字段带**显式分页实参**（``limit``/``first``/``page_size``/``pageSize``/``max_results``，
      字面量或变量皆可）→ 该字段计 ``min(实参, 100)`` 分，并把该倍率**向下累乘**；
    - 字段返回**列表**但无显式实参 → 计 ``_IMPLICIT_LIST_COST``，倍率保持父级（见常数注释）；
    - 其余字段 → 计 ``_COST_PER_FIELD``；
    - 总分超阈值 → 抛 ``GraphQLError(COST_MESSAGE)``，由引擎收成 ``errors``（HTTP 仍是 200，
      非 500、非栈），前端可见受控错误。

    **分页实参先于「是否列表」判定**（2026-09-26 真机验收暴露）：本仓分页字段的形态是
    「``contentItems(pageSize: N)`` → **页对象** ``GraphContentPage`` → ``items: [T!]!``」，
    即声明条数的那个字段**本身不是列表**。若只在字段是列表时才读分页实参，
    ``contentItems(pageSize: 100000)`` 会与 ``pageSize: 1`` 同价（实测 23 分），
    限流形同虚设。故按「谁声明了条数，谁负责把这批行计价」处理。

    阈值 ``<= 0`` = 不注册本扩展（本地 GraphiQL 拉 introspection 的逃生口，与旧
    ``MaxTokensLimiter`` 同款约定）。
    """

    def on_validate(self) -> Iterator[None]:
        limit = settings.graphql_max_cost
        if limit > 0 and _query_cost(self.execution_context, limit) > limit:
            raise GraphQLError(COST_MESSAGE)
        yield


def _field_def(parent_type: Any, name: str) -> Any:
    """查字段定义；父类型是 Union 等无字段表的类型时返回 None（当叶字段处理）。"""
    if not isinstance(parent_type, (GraphQLObjectType, GraphQLInterfaceType)):
        return None
    return parent_type.fields.get(name)


def _named_type(graphql_type: Any) -> Any:
    """剥掉 ``List``/``NonNull`` 包装，取内层具名类型。"""
    while isinstance(graphql_type, (GraphQLList, GraphQLNonNull)):
        graphql_type = graphql_type.of_type
    return graphql_type


def _is_list_type(graphql_type: Any) -> bool:
    """判定是否列表（穿透 ``NonNull``；项目里列表字段普遍是 ``[T!]!``）。"""
    while isinstance(graphql_type, GraphQLNonNull):
        graphql_type = graphql_type.of_type
    return isinstance(graphql_type, GraphQLList)


def _limit_multiplier(node: FieldNode, variables: dict[str, Any]) -> int | None:
    """取字段的显式分页实参值；无该实参 / 非整数字面量或变量 → None。

    变量在 ``on_validate`` 阶段已解析（``execution_context.variables``），故
    ``contentItems(pageSize: $n)`` 能按真实 n 计价，而不是退化成默认值。
    """
    for arg in node.arguments:
        if arg.name.value not in _LIST_LIMIT_ARGS:
            continue
        value = arg.value
        raw: Any = None
        if isinstance(value, IntValueNode):
            raw = int(value.value)
        elif isinstance(value, VariableNode):
            raw = variables.get(value.name.value)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return min(max(raw, 1), _MAX_LIST_MULTIPLIER)
    return None


def _selection_cost(
    parent_type: Any,
    selections: Any,
    multiplier: int,
    fragments: dict[str, Any],
    variables: dict[str, Any],
    budget: int,
) -> int:
    """递归计分一个 selection set，超过 ``budget`` 即提前收束（分数只用于判超限）。"""
    total = 0
    for sel in selections:
        if isinstance(sel, FieldNode):
            # __typename 等元字段无字段定义 → 当叶字段（计基础分，不递归）
            fdef = _field_def(parent_type, sel.name.value)
            ftype = getattr(fdef, "type", None)
            child_multiplier = multiplier
            # 分页实参先判：声明了条数的字段负责为这批行计价，**与它自身是不是列表无关**
            # （本仓是「分页字段 → 页对象 → items 列表」，声明条数的那个字段返回的是页对象）。
            explicit = _limit_multiplier(sel, variables)
            if explicit is not None:
                total += explicit * multiplier
                # 声明了条数才向下累乘：父级已按 pageSize 计价的那批行，其子列表（通常是同一页
                # 的 items）不该再乘一次（见 _IMPLICIT_LIST_COST 注释）。
                child_multiplier = multiplier * explicit
            elif ftype is not None and _is_list_type(ftype):
                total += _IMPLICIT_LIST_COST * multiplier
            else:
                total += _COST_PER_FIELD * multiplier
            if fdef is not None and sel.selection_set is not None:
                total += _selection_cost(
                    _named_type(ftype),
                    sel.selection_set.selections,
                    child_multiplier,
                    fragments,
                    variables,
                    budget,
                )
        elif isinstance(sel, InlineFragmentNode):
            inner = _named_type(sel.type_condition) if sel.type_condition else parent_type
            total += _selection_cost(
                inner or parent_type,
                sel.selection_set.selections,
                multiplier,
                fragments,
                variables,
                budget,
            )
        elif isinstance(sel, FragmentSpreadNode):
            frag = fragments.get(sel.name.value)
            if frag is None:
                continue  # 未定义片段交给 graphql-core 的标准校验报错，这里不重复报
            total += _selection_cost(
                _named_type(frag.type_condition) or parent_type,
                frag.selection_set.selections,
                multiplier,
                fragments,
                variables,
                budget,
            )
        if total > budget:
            return total
    return total


def _query_cost(execution_context: Any, budget: int) -> int:
    """整个文档的成本（全部 operation 之和；任一超预算即提前返回）。"""
    document = execution_context.graphql_document
    if document is None:
        return 0
    # strawberry.Schema 的 graphql-core 内核（字段/类型信息只在它上面）
    graphql_schema = execution_context.schema._schema
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    variables = execution_context.variables or {}
    roots = {
        "query": graphql_schema.query_type,
        "mutation": graphql_schema.mutation_type,
        "subscription": graphql_schema.subscription_type,
    }
    total = 0
    for definition in document.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue
        root = roots.get(definition.operation.value)
        if root is None or definition.selection_set is None:
            continue
        total += _selection_cost(
            root, definition.selection_set.selections, 1, fragments, variables, budget
        )
        if total > budget:
            break
    return min(total, _COST_HARD_CAP)


class GraphQLGuard(SchemaExtension):
    """查询级时间预算 + 耗时观测 + 顶级字段串行化（每请求实例化：schema 以**类**形式注册扩展）。

    - ``on_operation``：记起点与截止时刻、建本请求的串行化锁；收束时把耗时写入 histogram
      （被拒的查询也计入）。
    - ``resolve``：每个字段解析前检查预算；耗尽即抛 ``GraphQLError(TIMEOUT_MESSAGE)``
      —— 由 GraphQL 引擎收成 `errors`（HTTP 仍是 200，非 500、非栈），前端可见受控错误。
      顶级字段（``info.path.prev is None``）另经 ``_root_lock`` **串行**进入。

    **为什么要串行顶级字段**（2026-09-26 真机验收暴露的既有缺陷）：graphql-core 用
    ``asyncio.gather`` 并发执行**同级**字段，而所有 resolver 共享同一个
    ``info.context.db``（``get_read_session`` 给的 AsyncSession）——两个都打 DB 的根字段
    并发时 SQLAlchemy 抛 ``This session is provisioning a new connection; concurrent
    operations are not permitted``，随后会话 close 再抛 ``IllegalStateChangeError``，
    最终是 **500**。而蓝图把 GraphQL 定位为「聚合读」，**聚合恰恰就是多根字段**，只是前端
    目前只发单根字段才长期没暴露。

    锁只加在**顶级字段**上：当前唯一的 async resolver 就在这一层（嵌套数据由 service 一次
    批量取好后在 Python 侧组装），而叶字段由同步默认 resolver 解析、既不 await 也不碰 DB，
    逐个加锁纯属白付开销。**将来若新增嵌套的 async resolver，它同样必须经这把锁访问
    context.db**，否则会重新引入本条缺陷。
    """

    # 显式声明并置 None：旧实现用 getattr(self, "_deadline", float("inf")) 把「还没设截止
    # 时刻」默默当成无限预算，等于这条防线失效且毫无信号。正常路径下 on_operation 一定先执行。
    _deadline: float | None = None
    # 顶级字段的串行化锁：**必须每请求一把**（扩展按类注册、strawberry 每请求实例化，
    # 故这里在 on_operation 里新建）。跨请求共用会让无关客户端互相阻塞。
    _root_lock: asyncio.Lock | None = None

    def on_operation(self) -> Iterator[None]:
        started = time.perf_counter()
        self._deadline = started + settings.graphql_timeout_s
        self._root_lock = asyncio.Lock()
        try:
            yield
        finally:
            graphql_query_duration_seconds.observe(time.perf_counter() - started)

    async def _serialized_root(
        self, _next: Any, root: Any, info: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        """在请求级锁内解析一个顶级字段（见类 docstring 的缺陷说明）。

        ``_next`` 可能返回 awaitable 也可能直接返回值（同步 resolver），故用 ``await_maybe``
        统一收口——不能无条件 ``await``（对 list/dict 会抛 "object list can't be used in
        'await'"）。
        """
        assert self._root_lock is not None  # on_operation 必先执行
        async with self._root_lock:
            return await await_maybe(_next(root, info, *args, **kwargs))

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
        # 未初始化（没走 on_operation）按超时拒绝，而不是把预算当成无限静默放行。
        if self._deadline is None or time.perf_counter() > self._deadline:
            raise GraphQLError(TIMEOUT_MESSAGE)
        # 顶级字段（无父路径）串行进入：共享的 AsyncSession 不允许并发使用（见类 docstring）。
        if info.path.prev is None and self._root_lock is not None:
            return self._serialized_root(_next, root, info, args, kwargs)
        return _next(root, info, *args, **kwargs)


def reject_reason(message: str) -> str | None:
    """错误消息 → 防护拒绝原因（depth/complexity/timeout）；非防护错误返回 None。

    只认防护自身的文案，故业务 resolver 的执行错误不会污染 ``rejected_total``。
    """
    low = message.lower()
    for markers, reason in _REASON_MARKERS:
        if all(marker in low for marker in markers):
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


def _all_graphql_types(version: str) -> list[type[Any]]:
    """按版本登记的模块清单取 Query 类（``version`` 未登记即装配期报错，不静默空表）。"""
    names = GRAPHQL_VERSIONS.get(version)
    if names is None:
        raise ValueError(
            f"未登记的 GraphQL 版本 {version!r}；已登记：{sorted(GRAPHQL_VERSIONS)}"
        )
    types: list[type[Any]] = []
    for name in names:
        types.extend(registry.graphql_of(name))
    return types


def build_schema(version: str = GRAPHQL_DEFAULT_VERSION) -> strawberry.Schema:
    """按 ``version`` 合并其登记模块导出的 GraphQL 类型，构建带防护扩展的 schema。

    **各版本 schema 完全独立**（§2 多端点版本化）：破坏性变更开新版本端点、旧端点按原
    schema 继续服务存量客户端，不靠 ``@deprecated`` 在单端点里堆废弃字段。

    注意 registry 的契约只是「任意 strawberry 类型的列表」，本函数把它们统一并进
    ``query=``：模块若导出 Mutation 类，其字段会**变成 query 字段**（真 mutation 操作
    反而校验失败），而 strawberry 的类不带 Query/Mutation 标记、无法按类型分流。
    故这里按约定命名（``XxxMutation``）显式拦下，让这类错误在装配期就响，而不是静默
    把写操作暴露成查询。要真正支持 mutation，须先扩 registry 契约（模块分导出 query
    与 mutation 两类）再传 ``mutation=``。

    扩展以**工厂/类**形式注册（非实例）：strawberry 每请求实例化，避免
    ``GraphQLGuard`` 的计时状态跨并发请求串台（传实例已在新版被标记为弃用）。
    """
    classes = tuple(_all_graphql_types(version))
    mutations = [c.__name__ for c in classes if c.__name__.endswith("Mutation")]
    if mutations:
        raise RuntimeError(
            "GraphQL 聚合暂只支持 Query，但 registry 导出了 Mutation 类："
            f"{mutations}（需先扩展 registry 契约以支持 mutation）"
        )
    merged_query = merge_types("Query", classes)  # type: ignore[arg-type]
    extensions: list[Any] = [
        lambda: QueryDepthLimiter(max_depth=settings.graphql_max_depth),
    ]
    # ``graphql_max_cost <= 0`` = 不注册成本限制（逃生口：需要完全免限时用）。默认 1000 的
    # 余量经本文件校准确认：拿变量 pageSize 的真实前端查询约 140 分，7× 余量充足。
    if settings.graphql_max_cost > 0:
        extensions.append(QueryCostLimiter)
    extensions.append(GraphQLGuard)
    return strawberry.Schema(query=merged_query, extensions=extensions)
