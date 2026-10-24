"""板块服务 —— 并入 content 单一 service 命名空间的薄 re-export 桩。

M2.2 content 子域收中：boards 的服务逻辑统一收口到
``app/modules/content/service.py``（本文件的历史实现在其中）。此文件保留仅为向后
兼容的进口点（boards/router、content/graphql、测试按原路径
``app.modules.content.boards.service`` 导函数），改由 content.service 供给同名符号，
不再自重生命周期。棋盘跨凭证鉴权(exam)等在 content.service 一处维护。注意：测试会按
``boards.service.__dict__`` 的 ``check_post_allowed`` 做 monkeypatch，content.service
写入路径运行时经懒 import 引用同名，语义不变。REST/GraphQL/error-code 均不破。
"""

from app.modules.content.service import (
    ban_user,
    check_post_allowed,
    create_board_ex,
    get_board_ex,
    is_banned,
    list_boards,
    list_my_board_applications,
    review_board_application,
    submit_application,
    unban_user,
    update_board_ex,
)

# boards 的 "review_application"(审核板块申请) 与 columns 的同名(审核栏目申请)语义不同，
# 在 content/service 单一命名空间内以 review_board_application 收中后，此桩以原公共名
# ``review_application`` 重导出，维持 boards/router 与测试按原路径 import 的名字不变。
review_application = review_board_application

__all__ = [
    "ban_user",
    "check_post_allowed",
    "create_board_ex",
    "get_board_ex",
    "is_banned",
    "list_boards",
    "list_my_board_applications",
    "review_application",
    "submit_application",
    "unban_user",
    "update_board_ex",
]
