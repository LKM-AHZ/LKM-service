from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    and_,
    column,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import (  # 注意 db.base 而非 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class Project(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "projects"

    title: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    is_incubated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ———————— 项目广场展示字段（后端扩充，供 GET /projects 展示） ————————
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="showcase"
    )  # recruiting(招募中) | showcase(展示)
    is_recruiting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    background: Mapped[str | None] = mapped_column(Text, nullable=True)
    goals: Mapped[str | None] = mapped_column(Text, nullable=True)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_intro: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 三列都用 MutableList 包装：裸 JSON 不跟踪原地修改（append/remove/赋值单元素），
    # 加载后 `project.tags.append(...)` 不会被判脏，flush 时静默丢弃
    recruiting_roles: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), default=list
    )  # [str] 招募角色
    tags: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), default=list
    )  # [str]
    reports: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), default=list
    )  # [{title,content,revision,date}] 进展报告
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active | archived
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    # order_by：ProjectMember.sort_order 是持久化字段却从未被应用，成员顺序成了 DB 任意序
    members: Mapped[list[ProjectMember]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMember.sort_order",
    )
    updates: Mapped[list[ProjectUpdate]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectUpdate.created_at",
    )


class ProjectApplication(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "project_applications"
    __table_args__ = (
        Index(
            "uq_project_applications_pending",
            "applicant_id",
            "title",
            unique=True,
            postgresql_where=column("status") == "pending",
        ),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    # 贡献成员清单：JSON 文本（[{display_name, role_in_project, user_id?}]），跨驱动用 Text
    member_claims: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending|approved|rejected
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


class ProjectMember(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "project_members"
    # 一个用户在同一项目里至多一条「在用」成员行；left 后可重新激活同一行。
    __table_args__ = (
        Index(
            "uq_project_members_active_user",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=and_(
                column("status") == "active", column("user_id").isnot(None)
            ),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )  # S5: auth user_id
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 展示标签（历史字段，保留原样）；权限一律看下面的 role
    role_in_project: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # owner > manager > editor > viewer
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active | left
    joined_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    project: Mapped[Project] = relationship(back_populates="members")


class ProjectInvite(UUIDPrimaryKeyMixin, Base):
    """项目邀请：同一项目+用户仅一条待接受（status=invited）邀请。"""

    __tablename__: str = "project_invites"
    __table_args__ = (
        Index(
            "uq_project_invites_pending",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=column("status") == "invited",
        ),
        Index("ix_project_invites_project_id", "project_id"),
        Index("ix_project_invites_user_id", "user_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="invited"
    )  # invited | accepted | declined | revoked
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class ProjectJoinRequest(UUIDPrimaryKeyMixin, Base):
    """加入项目申请：与孵化 project_applications 分离。"""

    __tablename__: str = "project_join_requests"
    __table_args__ = (
        Index(
            "uq_project_join_requests_pending",
            "project_id",
            "applicant_id",
            unique=True,
            postgresql_where=column("status") == "pending",
        ),
        Index("ix_project_join_requests_project_id", "project_id"),
        Index("ix_project_join_requests_applicant_id", "applicant_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False
    )
    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role_requested: Mapped[str] = mapped_column(
        String(20), nullable=False, default="viewer"
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending | approved | rejected
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


class ProjectUpdate(UUIDPrimaryKeyMixin, Base):
    """项目进展 / 报告追加流（可追溯），旧 reports JSON 保留不动。"""

    __tablename__: str = "project_updates"
    __table_args__ = (
        Index("ix_project_updates_project_created", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="progress")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    project: Mapped[Project] = relationship(back_populates="updates")
