"""RBAC 权限点与复合角色定义（代码侧事实源）。

权限点命名两段式 ``域.动作``；对象级权限点加 owner 前缀。复合角色为
``{account_level}:{profile.role}``（见 spec §3.1）。角色→权限默认映射
``DEFAULT_GRANTS`` 供 seed 落库；运行期以 ``role_permissions`` 表为准。
"""

from enum import StrEnum
from typing import NamedTuple


class Permission(StrEnum):
    """全部权限点（代码侧枚举，运行期以 DB 映射为准）。"""

    # member 域
    comment_create = "member.comment_create"
    avatar_update = "member.avatar_update"
    # content 域（统一内容模型）
    content_create = "content.create"
    content_comment_create = "content.comment_create"
    content_like = "content.like"
    # interaction 域（M6.6）
    interaction_favorite = "interaction.favorite"
    interaction_history = "interaction.history"
    # notification 域（M6.8）
    notification_read = "notification.read"
    notification_manage = "notification.manage"
    # boards 域
    boards_create_application = "boards.create_application"
    boards_review_application = "boards.review_application"
    boards_manage = "boards.manage"
    # articles 域（全官方文章，仅 super_admin）
    articles_publish = "articles.publish"
    articles_review = "articles.review"
    articles_category_manage = "articles.category_manage"
    # columns 域
    columns_application_create = "columns.application_create"
    columns_application_review = "columns.application_review"
    columns_publish = "columns.publish"
    # files 域
    files_upload = "files.upload"
    files_download = "files.download"
    files_review = "files.review"
    # projects 域
    projects_application_create = "projects.application_create"
    projects_application_review = "projects.application_review"
    projects_create = "projects.create"
    projects_review = "projects.review"
    # 后台 admin 域
    admin_dashboard = "admin.dashboard"
    admin_reports_view = "admin.reports_view"
    admin_users_manage = "admin.users_manage"
    admin_content_review = "admin.content_review"
    admin_moderation_manage = "admin.moderation_manage"
    admin_analytics_view = "admin.analytics_view"
    # 对象级权限点（配 require_owner 依赖内查属主）
    article_owner_publish = "article.owner_publish"
    article_owner_comment_delete = "article.owner_comment_delete"
    column_owner_publish = "column.owner_publish"
    board_owner_manage = "board.owner_manage"
    content_owner_delete = "content.owner_delete"
    file_owner_delete = "file.owner_delete"
    file_owner_update = "file.owner_update"
    project_owner_update = "project.owner_update"


class Result(StrEnum):
    """默认映射粒度：一种权限点通常多个角色都有，用 Result 标记授予与否。"""

    GRANTED = "granted"


class Grant(NamedTuple):
    permission: Permission
    result: Result = Result.GRANTED


def composible_role(account_level: str, role: str) -> str:
    """复合角色字符串：``{account_level}:{role}``。"""
    return f"{account_level}:{role}"


# 各复合角色默认授予的权限点。KEY = 复合角色名。
DEFAULT_GRANTS: dict[str, tuple[Grant, ...]] = {
    "local:member": (Grant(Permission.avatar_update),),
    "normal:member": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
        Grant(Permission.projects_application_create),
    ),
    "normal:columnist": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.columns_publish),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
        Grant(Permission.projects_application_create),
    ),
    "normal:author": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.columns_publish),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.projects_application_create),
        Grant(Permission.projects_create),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
    ),
    # grant_incubation（auth/service_authz.py）会把通过的项目申请人升为
    # account_level=admin + profile.role=incubated_member，这是 seed 后可达的复合角色。
    # 该角色语义仍是「普通成员」，故按 normal:member 授予成员域权限（缺失会让升格用户
    # 直接掉到零权限）；未授予任何 admin 域权限。
    "admin:incubated_member": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
        Grant(Permission.projects_application_create),
    ),
    "admin:org_member": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.projects_application_create),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
        Grant(Permission.admin_dashboard),
        Grant(Permission.admin_reports_view),
    ),
    "admin:super_admin": (
        Grant(Permission.comment_create),
        Grant(Permission.avatar_update),
        Grant(Permission.content_create),
        Grant(Permission.content_comment_create),
        Grant(Permission.content_like),
        Grant(Permission.files_upload),
        Grant(Permission.files_download),
        Grant(Permission.interaction_favorite),
        Grant(Permission.interaction_history),
        Grant(Permission.notification_read),
        Grant(Permission.notification_manage),
        Grant(Permission.boards_create_application),
        Grant(Permission.columns_application_create),
        Grant(Permission.columns_publish),
        Grant(Permission.projects_application_create),
        Grant(Permission.projects_create),
        Grant(Permission.articles_publish),
        Grant(Permission.articles_review),
        Grant(Permission.articles_category_manage),
        Grant(Permission.columns_application_review),
        Grant(Permission.boards_review_application),
        Grant(Permission.boards_manage),
        Grant(Permission.files_review),
        Grant(Permission.projects_application_review),
        Grant(Permission.projects_review),
        Grant(Permission.admin_dashboard),
        Grant(Permission.admin_reports_view),
        Grant(Permission.admin_users_manage),
        Grant(Permission.admin_content_review),
        Grant(Permission.admin_moderation_manage),
        Grant(Permission.admin_analytics_view),
        Grant(Permission.board_owner_manage),
        Grant(Permission.article_owner_publish),
        Grant(Permission.article_owner_comment_delete),
        Grant(Permission.column_owner_publish),
        Grant(Permission.content_owner_delete),
        Grant(Permission.file_owner_delete),
        Grant(Permission.file_owner_update),
        Grant(Permission.project_owner_update),
    ),
}
