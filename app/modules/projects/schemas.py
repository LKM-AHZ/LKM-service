import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class MemberClaim(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)
    role_in_project: str = Field(..., min_length=1, max_length=100)
    user_id: uuid.UUID | None = None


class ProjectApplicationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., min_length=1, max_length=300)
    description: str = Field(..., min_length=1, max_length=500)
    # 每个标量字段都有上限，这个列表原先没有：整份 claims 会序列化进
    # project_applications.member_claims(Text)，审核时再解析/遍历 → 不限量即廉价的
    # 资源耗尽面。50 与「项目成员」这一业务量级相称（远超实际，但足以封顶）。
    member_claims: list[MemberClaim] = Field(default_factory=list, max_length=50)


class ProjectApplicationOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    applicant_id: uuid.UUID
    title: str
    summary: str
    description: str
    status: str
    member_claims: list[dict] = Field(
        default_factory=list
    )  # 请求原样回显（存 JSON 文本）
    reviewer_id: uuid.UUID | None = None
    review_note: str | None = None
    created_at: datetime.datetime
    reviewed_at: datetime.datetime | None = None


class ReviewProjectApplicationRequest(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


class ProjectMemberOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID | None
    display_name: str
    role_in_project: str
    sort_order: int


class ProjectOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    summary: str
    description: str
    applicant_id: uuid.UUID
    is_incubated: bool
    # 项目广场展示字段
    type: str = "showcase"
    is_recruiting: bool = False
    is_pinned: bool = False
    progress: int = 0
    background: str | None = None
    goals: str | None = None
    requirements: str | None = None
    team_intro: str | None = None
    recruiting_roles: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    reports: list[dict] = Field(default_factory=list)
    applicant_name: str = ""
    status: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    members: list[ProjectMemberOut] = Field(default_factory=list)
