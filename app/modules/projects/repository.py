"""projects 域的仓储子类：把 SQLAlchemy 表达式收在 service 层之外。

基类 :class:`core.db.repository.AsyncRepository` 供通用 CRUD；本文件只放
**projects 域的领域查询**（pending 去重判定、成员预载、公开列表排序）。
``auth.snapshot`` / ``auth.service_authz`` 等跨模块**服务调用**仍留在 service
编排层，不在此收编。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.modules.projects.models import (
    Project,
    ProjectApplication,
    ProjectInvite,
    ProjectJoinRequest,
    ProjectMember,
    ProjectUpdate,
)
from core.db.repository import AsyncRepository
from core.err import BizError, ErrCode


def _project_options() -> tuple[Any, ...]:
    """成员与进展流预加载，避免 async 会话里 lazy 访问。"""
    return (
        selectinload(Project.members),
        selectinload(Project.updates),
    )


class ProjectApplicationRepository(AsyncRepository[ProjectApplication]):
    model = ProjectApplication

    async def list_for_applicant(
        self, applicant_id: uuid.UUID
    ) -> list[ProjectApplication]:
        return await self.get_many(
            ProjectApplication.applicant_id == applicant_id,
            order_by=(ProjectApplication.created_at.desc(),),
        )

    async def pending_duplicate_exists(
        self, *, applicant_id: uuid.UUID, title: str
    ) -> bool:
        """同一申请人同名 pending 申请是否已存在（防重复刷单）。

        比较前统一 strip+小写：否则同一个人用 "LKM"/"LKM "/"lkm" 三种写法就能绕过防刷。
        """
        return await self.exists(
            ProjectApplication.applicant_id == applicant_id,
            ProjectApplication.status == "pending",
            func.lower(ProjectApplication.title) == title.strip().lower(),
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

    async def get_active(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectMember | None:
        return await self.get_one(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.status == "active",
        )

    async def get_any(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectMember | None:
        """含已退出（left）的成员行，用于重新加入时复用同一行。"""
        return await self.get_one(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            order_by=ProjectMember.joined_at.desc(),
        )

    async def list_active(self, project_id: uuid.UUID) -> list[ProjectMember]:
        return await self.get_many(
            ProjectMember.project_id == project_id,
            ProjectMember.status == "active",
            order_by=(ProjectMember.sort_order, ProjectMember.joined_at),
        )

    async def max_sort_order(self, project_id: uuid.UUID) -> int:
        value = await self.db.scalar(
            select(func.max(ProjectMember.sort_order)).where(
                ProjectMember.project_id == project_id
            )
        )
        return int(value or 0)


class ProjectInviteRepository(AsyncRepository[ProjectInvite]):
    model = ProjectInvite

    async def get_pending(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProjectInvite | None:
        return await self.get_one(
            ProjectInvite.project_id == project_id,
            ProjectInvite.user_id == user_id,
            ProjectInvite.status == "invited",
        )

    async def list_for_project(self, project_id: uuid.UUID) -> list[ProjectInvite]:
        return await self.get_many(
            ProjectInvite.project_id == project_id,
            order_by=ProjectInvite.created_at.desc(),
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[ProjectInvite]:
        return await self.get_many(
            ProjectInvite.user_id == user_id,
            order_by=ProjectInvite.created_at.desc(),
        )


class ProjectJoinRequestRepository(AsyncRepository[ProjectJoinRequest]):
    model = ProjectJoinRequest

    async def get_pending(
        self, project_id: uuid.UUID, applicant_id: uuid.UUID
    ) -> ProjectJoinRequest | None:
        return await self.get_one(
            ProjectJoinRequest.project_id == project_id,
            ProjectJoinRequest.applicant_id == applicant_id,
            ProjectJoinRequest.status == "pending",
        )

    async def list_for_project(
        self, project_id: uuid.UUID
    ) -> list[ProjectJoinRequest]:
        return await self.get_many(
            ProjectJoinRequest.project_id == project_id,
            order_by=ProjectJoinRequest.created_at.desc(),
        )

    async def list_for_applicant(
        self, applicant_id: uuid.UUID
    ) -> list[ProjectJoinRequest]:
        return await self.get_many(
            ProjectJoinRequest.applicant_id == applicant_id,
            order_by=ProjectJoinRequest.created_at.desc(),
        )


class ProjectUpdateRepository(AsyncRepository[ProjectUpdate]):
    model = ProjectUpdate

    async def list_for_project(self, project_id: uuid.UUID) -> list[ProjectUpdate]:
        return await self.get_many(
            ProjectUpdate.project_id == project_id,
            order_by=ProjectUpdate.created_at,
        )
