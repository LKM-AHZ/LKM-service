"""projects 域对外只读 API（``core.ports.projects`` 的实现去处）。

只暴露**判定/列表**能力，不把 ORM 实例交给调用方；需要参与调用方查询的地方返回一个
``EXISTS`` 谓词，由调用方传入自己的外键列做关联——projects 域不认识别的域的表。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Project, ProjectApplication, ProjectMember


async def list_uploadable_projects(
    db: AsyncSession, user_id: uuid.UUID
) -> list[dict[str, str]]:
    """用户可上传文件的项目：本人申请的 + 本人为成员的，按创建时间倒序。"""
    member = (
        select(ProjectMember.id)
        .where(ProjectMember.project_id == Project.id, ProjectMember.user_id == user_id)
        .exists()
    )
    rows = (
        await db.execute(
            select(Project.id, Project.title)
            .where(or_(Project.applicant_id == user_id, member))
            .order_by(Project.created_at.desc())
        )
    ).all()
    return [{"id": str(row.id), "title": row.title} for row in rows]


def member_exists_clause(user_id: uuid.UUID, *, outer_column: Any) -> Any:
    """「该用户是 ``outer_column`` 所指项目的成员」的 ``EXISTS`` 谓词。

    ``outer_column`` 由调用方传入（如 ``LibraryFile.project_id``），projects 域只用自己的表。
    """
    return (
        select(ProjectMember.id)
        .where(
            ProjectMember.project_id == outer_column,
            ProjectMember.user_id == user_id,
        )
        .exists()
    )


async def can_upload_to_project(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """项目存在且用户可向其上传：本人申请的项目，或本人是项目成员。"""
    project = await db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        return False
    if project.applicant_id == user_id:
        return True
    return await is_member(db, project_id, user_id)


async def is_member(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """用户是否是该项目的成员。"""
    row = await db.scalar(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    return row is not None


async def pending_project_applications(
    db: AsyncSession,
) -> tuple[int, datetime.datetime | None]:
    """待审项目申请：``(数量, 最早提交时间)``。"""
    row = (
        await db.execute(
            select(
                func.count(ProjectApplication.id),
                func.min(ProjectApplication.created_at),
            ).where(ProjectApplication.status == "pending")
        )
    ).one()
    return int(row[0]), row[1]
