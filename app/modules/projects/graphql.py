"""projects(项目) 只读 GraphQL。复用 service;members 已由 selectinload 加载。"""

import uuid

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.core.err import BizError
from app.modules.projects.errors import ProjectErr
from app.modules.projects.schemas import ProjectMemberOut, ProjectOut
from app.modules.projects.service import get_project_ex, list_projects


@strawberry.type
class GraphProjectMember:
    id: strawberry.ID
    projectId: strawberry.ID
    userId: strawberry.ID | None
    displayName: str
    roleInProject: str
    sortOrder: int


@strawberry.type
class GraphProject:
    id: strawberry.ID
    title: str
    summary: str
    description: str
    applicantId: strawberry.ID
    isIncubated: bool
    status: str
    members: list[GraphProjectMember]


@strawberry.type
class GraphProjectList:
    items: list[GraphProject]


def _map_member(m: ProjectMemberOut) -> GraphProjectMember:
    return GraphProjectMember(
        id=m.id,
        projectId=m.project_id,
        userId=m.user_id,
        displayName=m.display_name,
        roleInProject=m.role_in_project,
        sortOrder=m.sort_order,
    )


def _map_project(p: ProjectOut) -> GraphProject:
    return GraphProject(
        id=p.id,
        title=p.title,
        summary=p.summary,
        description=p.description,
        applicantId=p.applicant_id,
        isIncubated=p.is_incubated,
        status=p.status,
        members=[_map_member(m) for m in p.members],
    )


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


@strawberry.type
class ProjectsQuery:
    @strawberry.field
    async def projects(self, info: Info) -> GraphProjectList:
        db = _get_db(info)
        rows = await list_projects(db)
        return GraphProjectList(items=[_map_project(p) for p in rows])

    @strawberry.field
    async def project(
        self, info: Info, projectId: strawberry.ID
    ) -> GraphProject | None:
        db = _get_db(info)
        # GraphQL 的 ID 落到这里是 str：REST 侧由 FastAPI 的 uuid.UUID 路径类型校验，
        # 这里没有。畸形值过去会直达驱动抛 DataError/StatementError（表现为未处理的
        # GraphQL 执行错误），与「查不到 → None」的契约不符，故显式转换并对非法输入返回 None。
        try:
            project_uuid = uuid.UUID(str(projectId))
        except ValueError:
            return None
        try:
            p = await get_project_ex(db, project_uuid)
        except BizError as e:
            if e.errcode != ProjectErr.PROJECT_NOT_FOUND:
                raise
            return None
        return _map_project(p)
