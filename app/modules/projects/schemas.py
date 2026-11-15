import datetime
import uuid
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectRole = Literal["owner", "manager", "editor", "viewer"]
InviteRole = Literal["manager", "editor", "viewer"]


class MemberClaim(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)
    role_in_project: str = Field(..., min_length=1, max_length=100)
    user_id: uuid.UUID | None = None


class ProjectApplicationCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., min_length=1, max_length=300)
    description: str = Field(..., min_length=1, max_length=500)
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
    role: str = "viewer"
    status: str = "active"
    joined_at: datetime.datetime | None = None
    invited_by: uuid.UUID | None = None


class ProjectUpdateOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    author_id: uuid.UUID
    kind: str
    title: str
    content: str
    progress: int | None = None
    created_at: datetime.datetime


class ProjectUpdateCreate(BaseModel):
    kind: Literal["progress", "report", "update"] = "progress"
    title: str = Field(default="", max_length=200)
    content: str = Field(..., min_length=1)
    progress: int | None = Field(default=None, ge=0, le=100)


class ProjectPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    type: Literal["recruiting", "showcase"] | None = None
    is_recruiting: bool | None = None
    recruiting_roles: list[str] | None = None
    tags: list[str] | None = None
    background: str | None = None
    goals: str | None = None
    requirements: str | None = None
    team_intro: str | None = None
    status: Literal["active", "archived"] | None = None
    progress: int | None = Field(default=None, ge=0, le=100)


class MemberRoleUpdate(BaseModel):
    role: ProjectRole


class ProjectInviteCreate(BaseModel):
    user_id: uuid.UUID
    role: InviteRole = "viewer"


class ProjectInviteOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str
    invited_by: uuid.UUID
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ProjectJoinRequestCreate(BaseModel):
    role_requested: InviteRole = "viewer"
    message: str | None = Field(default=None, max_length=500)


class ProjectJoinRequestOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    applicant_id: uuid.UUID
    role_requested: str
    message: str | None = None
    status: str
    reviewer_id: uuid.UUID | None = None
    review_note: str | None = None
    created_at: datetime.datetime
    reviewed_at: datetime.datetime | None = None


class ReviewJoinRequestRequest(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


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
    updates: list[ProjectUpdateOut] = Field(default_factory=list)
