"""projects 域只读端口：归属判定与可上传项目列表。

files 域此前直接 ``from app.modules.projects.models import Project, ProjectMember`` 组装
SQL。项目归属口径（申请人 / 成员）属 projects 域，收进端口后调用方不再持有其 ORM。

``member_exists_clause`` 返回的是 ``EXISTS`` 谓词而不是布尔：调用方要把「属于该用户」与
自己的过滤条件 and_/or_ 组合，因此必须留在 SQL 层求值，不能先查一次再传集合。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def list_uploadable_projects(
    db: AsyncSession, user_id: uuid.UUID
) -> list[dict[str, str]]:
    """用户可上传文件的项目（本人申请或为成员），按创建时间倒序。"""
    return await get("projects_read").list_uploadable_projects(db, user_id)


def member_exists_clause(user_id: uuid.UUID, *, outer_column: Any) -> Any:
    """「该用户是 ``outer_column`` 所指项目的成员」的 ``EXISTS`` 谓词。"""
    return get("projects_read").member_exists_clause(user_id, outer_column=outer_column)


async def can_upload_to_project(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """项目存在且用户可上传：本人申请，或本人为成员。"""
    return await get("projects_read").can_upload_to_project(db, project_id, user_id)


async def is_member(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """用户是否为该项目成员。"""
    return await get("projects_read").is_member(db, project_id, user_id)


async def pending_project_applications(db: AsyncSession) -> tuple[int, Any]:
    """待审项目申请：``(数量, 最早提交时间)``。"""
    return await get("projects_read").pending_project_applications(db)
