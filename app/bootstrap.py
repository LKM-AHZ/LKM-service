"""app 侧自注册：把业务模块的模型/任务、错误码、seed 步骤与自有端口实现登记进 core。

装配根 ``boot.assemble`` 调用 :func:`register`。core 因而不知道任何业务模块名，
app 也不需要 import auth。

模型与任务清单来自 :mod:`app.modules.spec`（单一事实源），本模块不再维护第二份字面量。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.spec import APP_MODEL_MODULES, APP_TASK_MODULES

_registered = False


class _ContentStatsImpl:
    """``core.ports.content_stats`` 的业务侧实现（core 不得知道业务表）。"""

    async def count_content_created_by_day(self, days: int) -> dict[str, int]:
        from app.modules.content.daily_stats import count_content_created_by_day

        return await count_content_created_by_day(days)


class _PointsWriterImpl:
    """``core.ports.points`` 的业务侧实现：原样转发，保住同事务与幂等语义。"""

    async def enqueue_event(
        self, db: AsyncSession, user_id: uuid.UUID, event: str, ref_id: str
    ) -> None:
        from app.modules.points.rules import enqueue_points_event

        await enqueue_points_event(db, user_id, event, ref_id)

    async def reward(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        delta: int,
        reason: str,
        ref_type: str,
        ref_id: str,
        *,
        allow_negative: bool = False,
    ) -> Any:
        from app.modules.points.service import reward

        return await reward(
            db,
            user_id,
            delta,
            reason,
            ref_type,
            ref_id,
            allow_negative=allow_negative,
        )

    async def spend(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        amount: int,
        reason: str,
        ref_type: str,
        ref_id: str,
    ) -> Any:
        from app.modules.points.service import spend

        return await spend(db, user_id, amount, reason, ref_type, ref_id)


class _ReportsWriterImpl:
    """``core.ports.reports`` 的实现：转发到 admin 域的写入口径。"""

    async def create_if_absent(
        self,
        db: AsyncSession,
        *,
        type: str,
        target_id: str,
        target_title: str,
        reporter_id: uuid.UUID | None,
        reporter_name: str,
        reason: str,
        created_at: str | None = None,
        explicit_id: uuid.UUID | None = None,
    ) -> bool:
        from app.modules.admin.reports_writer import create_if_absent

        return await create_if_absent(
            db,
            type=type,
            target_id=target_id,
            target_title=target_title,
            reporter_id=reporter_id,
            reporter_name=reporter_name,
            reason=reason,
            created_at=created_at,
            explicit_id=explicit_id,
        )


class _NotificationWriterImpl:
    """``core.ports.notification`` 的实现：转发到 notification 域。"""

    def type_value(self, name: str) -> str:
        from app.modules.notification.service import NotificationType

        return NotificationType[name].value

    async def create(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        type: str,
        payload: dict[str, Any] | None = None,
        actor_id: uuid.UUID | None = None,
        target_id: uuid.UUID | None = None,
    ) -> Any:
        from app.modules.notification.service import create_notification

        return await create_notification(
            db,
            user_id=user_id,
            type=type,
            payload=payload,
            actor_id=actor_id,
            target_id=target_id,
        )


class _ModerationEngineImpl:
    """``core.ports.moderation`` 的实现：转发到 admin 域的规则引擎。"""

    async def load_active_rules(self, db: AsyncSession) -> list[Any]:
        from app.modules.admin.moderation.engine import load_active_rules

        return await load_active_rules(db)

    def evaluate(self, text: str, rules: list[Any]) -> Any:
        from app.modules.admin.moderation.engine import evaluate

        return evaluate(text, rules)


class _ProjectsReadImpl:
    """``core.ports.projects`` 的实现：转发到 projects 域的只读 API。"""

    async def list_uploadable_projects(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> list[dict[str, str]]:
        from app.modules.projects.read_api import list_uploadable_projects

        return await list_uploadable_projects(db, user_id)

    def member_exists_clause(self, user_id: uuid.UUID, *, outer_column: Any) -> Any:
        from app.modules.projects.read_api import member_exists_clause

        return member_exists_clause(user_id, outer_column=outer_column)

    async def can_upload_to_project(
        self, db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        from app.modules.projects.read_api import can_upload_to_project

        return await can_upload_to_project(db, project_id, user_id)

    async def is_member(
        self, db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        from app.modules.projects.read_api import is_member

        return await is_member(db, project_id, user_id)

    async def pending_project_applications(
        self, db: AsyncSession
    ) -> tuple[int, Any]:
        from app.modules.projects.read_api import pending_project_applications

        return await pending_project_applications(db)


class _ExamReadImpl:
    """``core.ports.exam`` 的实现：转发到 exam 域的只读判定。"""

    async def has_normal_certification(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> bool:
        from app.modules.exam.read_api import has_normal_certification

        return await has_normal_certification(db, user_id)


class _ContentAdminImpl:
    """``core.ports.content_admin`` 的实现：转发到 content 域的后台操作。"""

    async def list_items(
        self, db: AsyncSession, *, page: int, limit: int, content_type: str | None
    ) -> Any:
        from app.modules.content.admin_api import list_items

        return await list_items(db, page=page, limit=limit, content_type=content_type)

    async def delete_item(
        self,
        db: AsyncSession,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        as_admin: bool = True,
    ) -> uuid.UUID:
        from app.modules.content.admin_api import delete_item

        return await delete_item(db, item_id, actor_id, as_admin=as_admin)

    async def delete_series(
        self,
        db: AsyncSession,
        series_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        as_admin: bool = True,
    ) -> uuid.UUID:
        from app.modules.content.admin_api import delete_series

        return await delete_series(db, series_id, actor_id, as_admin=as_admin)

    async def delete_blog_comment(
        self,
        db: AsyncSession,
        series_id: uuid.UUID,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        as_admin: bool = True,
    ) -> uuid.UUID:
        from app.modules.content.admin_api import delete_blog_comment

        return await delete_blog_comment(
            db, series_id, comment_id, actor_id, as_admin=as_admin
        )

    async def delete_article_comment(
        self,
        db: AsyncSession,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        as_admin: bool = True,
    ) -> uuid.UUID:
        from app.modules.content.admin_api import delete_article_comment

        return await delete_article_comment(db, comment_id, actor_id, as_admin=as_admin)


class _AdminGuardImpl:
    """``core.ports.admin_guard`` 的实现：绑定后台 2FA 依赖对象。"""

    def __init__(self) -> None:
        from app.modules.admin.deps import require_admin_2fa

        self.require_admin_2fa = require_admin_2fa



class _FeedFanoutImpl:
    """``core.ports.feed`` 的实现：转发到 feed 域的物化写入口。"""

    async def backfill_author(self, db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID, limit: int) -> int:
        from app.modules.feed.fanout import backfill_author

        return await backfill_author(db, follower_id, author_id, limit)

    async def backfill_board(self, db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID, limit: int) -> int:
        from app.modules.feed.fanout import backfill_board

        return await backfill_board(db, follower_id, board_id, limit)

    async def remove_author_items(self, db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID, keep_boards: set[uuid.UUID]) -> int:
        from app.modules.feed.fanout import remove_author_items

        return await remove_author_items(db, follower_id, author_id, keep_boards)

    async def remove_board_items(self, db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID, keep_authors: set[uuid.UUID]) -> int:
        from app.modules.feed.fanout import remove_board_items

        return await remove_board_items(db, follower_id, board_id, keep_authors)

    async def remove_source_item(self, db: AsyncSession, source_id: uuid.UUID) -> int:
        from app.modules.feed.fanout import remove_source_item

        return await remove_source_item(db, source_id)


class _InteractionReadImpl:
    """``core.ports.interaction`` 的实现：转发到 interaction 域的读口。"""

    async def get_following_ids(self, db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
        from app.modules.interaction.service import get_following_ids

        return await get_following_ids(db, user_id)

    async def get_followed_board_ids(self, db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
        from app.modules.interaction.service import get_followed_board_ids

        return await get_followed_board_ids(db, user_id)

    async def list_bigv_author_ids(self, db: AsyncSession, *, threshold: int) -> list[uuid.UUID]:
        from app.modules.interaction.service import list_bigv_author_ids

        return await list_bigv_author_ids(db, threshold=threshold)

    async def list_follower_ids(self, db: AsyncSession, author_id: uuid.UUID, *, limit: int) -> list[uuid.UUID]:
        from app.modules.interaction.service import list_follower_ids

        return await list_follower_ids(db, author_id, limit=limit)

    async def list_board_follower_ids(self, db: AsyncSession, board_id: uuid.UUID, *, limit: int) -> list[uuid.UUID]:
        from app.modules.interaction.service import list_board_follower_ids

        return await list_board_follower_ids(db, board_id, limit=limit)

    def favorite_count_subquery(self, outer_column: Any) -> Any:
        from app.modules.interaction.read_api import favorite_count_subquery

        return favorite_count_subquery(outer_column)



class _ContentReadImpl:
    """``core.ports.content`` 的实现：转发到 content 域的只读摘要。"""

    async def item_brief(self, db: AsyncSession, item_id: uuid.UUID) -> Any:
        from app.modules.content.read_api import item_brief

        return await item_brief(db, item_id)

    async def comment_brief(self, db: AsyncSession, comment_id: uuid.UUID) -> Any:
        from app.modules.content.read_api import comment_brief

        return await comment_brief(db, comment_id)

    async def qa_answer_brief(self, db: AsyncSession, answer_id: uuid.UUID) -> Any:
        from app.modules.content.read_api import qa_answer_brief

        return await qa_answer_brief(db, answer_id)

    async def qa_question_brief(self, db: AsyncSession, question_id: uuid.UUID) -> Any:
        from app.modules.content.read_api import qa_question_brief

        return await qa_question_brief(db, question_id)

    async def count_discussions(self, db: AsyncSession) -> int:
        from app.modules.content.read_api import count_discussions

        return await count_discussions(db)

    async def daily_discussion_counts(
        self, db: AsyncSession, start: Any, days: int
    ) -> dict[Any, int]:
        from app.modules.content.read_api import daily_discussion_counts

        return await daily_discussion_counts(db, start, days)

    async def pending_board_applications(
        self, db: AsyncSession
    ) -> tuple[int, Any]:
        from app.modules.content.read_api import pending_board_applications

        return await pending_board_applications(db)

    async def pending_column_applications(
        self, db: AsyncSession
    ) -> tuple[int, Any]:
        from app.modules.content.read_api import pending_column_applications

        return await pending_column_applications(db)

    def search_select(self, term: str, content_type: str | None) -> Any:
        from app.modules.content.search_api import search_select

        return search_select(term, content_type)

    async def search_count(
        self, db: AsyncSession, term: str, content_type: str | None
    ) -> int:
        from app.modules.content.search_api import count_matching

        return await count_matching(db, term=term, content_type=content_type)

    def select_matching(
        self, term: str, content_type: str | None, *, offset: int, limit: int
    ) -> Any:
        from app.modules.content.search_api import select_matching

        return select_matching(term, content_type, offset=offset, limit=limit)

    def select_published_by_ids(self, ids: list[uuid.UUID]) -> Any:
        from app.modules.content.search_api import select_published_by_ids

        return select_published_by_ids(ids)

    async def index_doc(self, db: AsyncSession, item_id: uuid.UUID) -> Any:
        from app.modules.content.search_api import index_doc

        return await index_doc(db, item_id)

    async def index_docs_batch(
        self, db: AsyncSession, *, after_id: uuid.UUID, limit: int
    ) -> Any:
        from app.modules.content.search_api import index_docs_batch

        return await index_docs_batch(db, after_id=after_id, limit=limit)




class _FilesReadImpl:
    """``core.ports.files`` 的实现：转发到 files 域的只读聚合。"""

    async def count_files(self, db: AsyncSession, status: str | None = None) -> int:
        from app.modules.files.read_api import count_files

        return await count_files(db, status)

    async def pending_files(self, db: AsyncSession) -> tuple[int, Any]:
        from app.modules.files.read_api import pending_files

        return await pending_files(db)

    def search_select(self, term: str) -> Any:
        from app.modules.files.search_api import search_select

        return search_select(term)

    def select_public_by_ids(self, ids: list[Any]) -> Any:
        from app.modules.files.search_api import select_public_by_ids

        return select_public_by_ids(ids)

    async def index_doc(self, db: AsyncSession, file_id: Any) -> Any:
        from app.modules.files.search_api import index_doc

        return await index_doc(db, file_id)

    async def index_docs_batch(
        self, db: AsyncSession, *, after_id: Any, limit: int
    ) -> Any:
        from app.modules.files.search_api import index_docs_batch

        return await index_docs_batch(db, after_id=after_id, limit=limit)



def register() -> None:
    """幂等注册：业务模型/任务、错误码、seed 步骤与自有端口实现。"""
    global _registered
    if _registered:
        return

    from core import ports, task_registry
    from core.db import init_db, model_registry

    for path in APP_MODEL_MODULES:
        model_registry.register_module(path)
    for path in APP_TASK_MODULES:
        task_registry.register_module(path)

    # 各业务模块错误码（导入即 register() 副作用）
    from app.modules import registry as _modules

    _modules.load_errors()

    # schema 就绪后种 RBAC 默认权限（原 core.db.init_db 直连业务 seed，改为登记）
    from app.modules.rbac.seed import seed_rbac

    init_db.register_seed_step(seed_rbac)  # type: ignore[arg-type]

    ports.install("content_stats", _ContentStatsImpl())
    ports.install("reports_writer", _ReportsWriterImpl())
    ports.install("notification_writer", _NotificationWriterImpl())
    ports.install("moderation_engine", _ModerationEngineImpl())
    ports.install("projects_read", _ProjectsReadImpl())
    ports.install("exam_read", _ExamReadImpl())
    ports.install("content_admin", _ContentAdminImpl())
    ports.install("admin_guard", _AdminGuardImpl())
    ports.install("feed_fanout", _FeedFanoutImpl())
    ports.install("interaction_read", _InteractionReadImpl())
    ports.install("content_read", _ContentReadImpl())
    ports.install("files_read", _FilesReadImpl())
    ports.install("points_writer", _PointsWriterImpl())

    _registered = True
