"""Project collaboration: resource-level roles, invites, join requests, updates.

Revision ID: 0004_projects_collaboration
Revises: 0003_exam_question_bank

只做加性变更：project_members 加列（带默认值），新增邀请/加入申请/更新三张表，
并为既有项目回填 owner 成员行。create_all 通道不执行回填；服务层对缺少成员行的
老项目会把申请人按 owner 惰性补齐。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_projects_collaboration"
down_revision: str | Sequence[str] | None = "0003_exam_question_bank"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVITE_PENDING_INDEX = "uq_project_invites_pending"
_JOIN_PENDING_INDEX = "uq_project_join_requests_pending"
_MEMBER_ACTIVE_INDEX = "uq_project_members_active_user"


def _uuid_default() -> sa.TextClause:
    return sa.text("public.uuid_generate_v7()")


def upgrade() -> None:
    # ── project_members 加列 ────────────────────────────────────────────
    op.add_column(
        "project_members",
        sa.Column(
            "role", sa.String(length=20), nullable=False, server_default="viewer"
        ),
    )
    op.add_column(
        "project_members",
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
    )
    op.add_column(
        "project_members",
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "project_members", sa.Column("invited_by", sa.Uuid(), nullable=True)
    )
    for column in ("role", "status", "joined_at"):
        op.alter_column("project_members", column, server_default=None)

    # ── 新表 ────────────────────────────────────────────────────────────
    op.create_table(
        "project_invites",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=_uuid_default(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        _INVITE_PENDING_INDEX,
        "project_invites",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'invited'"),
    )
    op.create_index(
        "ix_project_invites_user_id", "project_invites", ["user_id"]
    )
    op.create_index(
        "ix_project_invites_project_id", "project_invites", ["project_id"]
    )

    op.create_table(
        "project_join_requests",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("role_requested", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=_uuid_default(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        _JOIN_PENDING_INDEX,
        "project_join_requests",
        ["project_id", "applicant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_project_join_requests_project_id",
        "project_join_requests",
        ["project_id"],
    )
    op.create_index(
        "ix_project_join_requests_applicant_id",
        "project_join_requests",
        ["applicant_id"],
    )

    op.create_table(
        "project_updates",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=_uuid_default(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_updates_project_created",
        "project_updates",
        ["project_id", "created_at"],
    )

    # ── 回填：既有项目的申请人即 owner ──────────────────────────────────
    op.execute(
        """
        INSERT INTO project_members
            (id, project_id, user_id, display_name, role_in_project, sort_order,
             role, status, joined_at)
        SELECT public.uuid_generate_v7(), p.id, p.applicant_id,
               '项目发起人', '负责人', -1, 'owner', 'active', now()
        FROM projects p
        WHERE NOT EXISTS (
            SELECT 1 FROM project_members m
            WHERE m.project_id = p.id AND m.user_id = p.applicant_id
        )
        """
    )

    op.create_index(
        _MEMBER_ACTIVE_INDEX,
        "project_members",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_MEMBER_ACTIVE_INDEX, table_name="project_members")
    # 回填出来的 owner 行在旧结构里只是普通成员行，删除以避免重复展示。
    op.execute("DELETE FROM project_members WHERE role = 'owner'")
    op.drop_column("project_members", "invited_by")
    op.drop_column("project_members", "joined_at")
    op.drop_column("project_members", "status")
    op.drop_column("project_members", "role")

    op.drop_index(
        "ix_project_updates_project_created", table_name="project_updates"
    )
    op.drop_table("project_updates")
    op.drop_index(
        "ix_project_join_requests_applicant_id", table_name="project_join_requests"
    )
    op.drop_index(
        "ix_project_join_requests_project_id", table_name="project_join_requests"
    )
    op.drop_index(_JOIN_PENDING_INDEX, table_name="project_join_requests")
    op.drop_table("project_join_requests")
    op.drop_index(
        "ix_project_invites_project_id", table_name="project_invites"
    )
    op.drop_index("ix_project_invites_user_id", table_name="project_invites")
    op.drop_index(_INVITE_PENDING_INDEX, table_name="project_invites")
    op.drop_table("project_invites")
