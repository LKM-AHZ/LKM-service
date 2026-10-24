"""专栏服务 —— 并入 content 单一 service 命名空间的薄 re-export 桩。

M2.2 content 子域收中：columns 的服务逻辑统一收口到
``app/modules/content/service.py``（本文件的历史实现在其中）。
此文件保留仅为向后兼容的进口点（columns/router、columns/graphql、测试按原路径
``app.modules.content.columns.service`` 导函数），改由 content.service 供给同名符号，
不再自重账号/生命周期。REST/GraphQL/error-code 均不破。
"""

from app.modules.content.service import (
    create_application,
    create_post,
    get_application,
    get_column,
    get_column_by_slug,
    get_column_plan,
    get_post,
    list_applications,
    list_columns,
    list_my_column_applications,
    list_posts,
    review_application,
)

__all__ = [
    "create_application",
    "create_post",
    "get_application",
    "get_column",
    "get_column_by_slug",
    "get_column_plan",
    "get_post",
    "list_applications",
    "list_columns",
    "list_my_column_applications",
    "list_posts",
    "review_application",
]
