"""projects 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`app.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**projects 域的领域查询**（pending 去重判定、成员预载、公开列表排序）。
``auth.snapshot`` / ``auth.service_authz`` 等跨模块**服务调用**仍留在 service
编排层，不在此收编。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import selectinload

from app.core.err import BizError, ErrCode
from app.db.repository import AsyncRepository
from app.modules.projects.models import Project, ProjectApplication, ProjectMember


def _project_options() -> tuple[Any, ...]:
    """成员预加载，避免 async 会话里 lazy 访问。申请人展示名改由读缝批量提供。"""
    return (selectinload(Project.members),)


class ProjectApplicationRepository(AsyncRepository[ProjectApplication]):
    model = ProjectApplication

    async def pending_duplicate_exists(
        self, *, applicant_id: uuid.UUID, title: str
    ) -> bool:
        """同一申请人同名 pending 申请是否已存在（防重复刷单）。"""
        return await self.exists(
            ProjectApplication.applicant_id == applicant_id,
            ProjectApplication.status == "pending",
            ProjectApplication.title == title,
        )


class ProjectRepository(AsyncRepository[Project]):
    model = Project

    async def get_with_members_or_raise(
        self, project_id: uuid.UUID, errcode: ErrCode, *, detail: str | None = None
    ) -> Project:
        project = await self.get_one(
            Project.id == project_id, options=_project_options()
        )
        if project is None:
            raise BizError(errcode, detail)
        return project

    async def list_active_with_members(self) -> list[Project]:
        """公开项目列表：仅 active，置顶优先、id 倒序。"""
        return await self.get_many(
            Project.status == "active",
            order_by=(Project.is_pinned.desc(), Project.id.desc()),
            options=_project_options(),
        )


class ProjectMemberRepository(AsyncRepository[ProjectMember]):
    model = ProjectMember

    async def add_all(self, members: list[ProjectMember]) -> None:
        """批量落成员（不 flush：调用方在纳入升级后统一 flush，保持原事务时序）。"""
        for member in members:
            self.db.add(member)
