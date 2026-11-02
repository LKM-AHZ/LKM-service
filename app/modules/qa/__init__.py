"""问答（QA）模块：悬赏问答业务域（步骤 9 切片 1，自 ``content`` 拆出）。

拆出的判据是「有自己的表、自己的错误码命名空间、自己的调度任务」：``qa_questions`` /
``qa_answers`` / ``qa_question_images`` 三张表与 ``NS_QA`` 错误码全部随本模块走，路由前缀
仍是 ``/content/qa``（URL 不变）。

与 content 的耦合只有两处，都经端口：

- 提问要同步成论坛可见条目 → ``core.ports.content.publish_qa_item``（同事务写 content_items）；
- feed 的问答源与通知的问答摘要 → 本模块 ``feed_api`` / ``read_api``，由 ``core.ports.qa`` 暴露。
"""

from __future__ import annotations

from typing import Any

_exported_routers: list[Any] | None = None

#: 问答域没有独立的 GraphQL 字段（``ContentItem.qaQuestionId`` 属 content 的查询类型）。
GRAPHQL: list[Any] = []


def __getattr__(name: str) -> Any:
    global _exported_routers
    if name == "ROUTERS":
        if _exported_routers is None:
            from app.modules.qa.router import router

            _exported_routers = [router]
        return _exported_routers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: list[str] = ["GRAPHQL", "ROUTERS"]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
