"""QA 服务 —— 并入 content 单一 service 命名空间的薄 re-export 桩。

M2.2 content 子域收中：qa 的服务逻辑统一收口到 ``app/modules/content/service.py``
（本文件的历史实现在其中，含 escrow 锁定/采纳派发/退回与独立 qa_questions/qa_answers
表写作）。此文件保留仅为向后兼容的进口点（qa/router、测试按原路径
``app.modules.content.qa.service`` 导函数），改由 content.service 供给同名符号。
REST/GraphQL/error-code 均不破。
"""

from app.modules.content.service import (
    accept_answer,
    close_question,
    create_answer,
    create_question,
    expire_due_questions,
    get_question,
    list_questions,
)

__all__ = [
    "accept_answer",
    "close_question",
    "create_answer",
    "create_question",
    "expire_due_questions",
    "get_question",
    "list_questions",
]
