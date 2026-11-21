"""Drop the removed StarHope learning assistant tables.

StarHope 小程序与 ``app/modules/starhope`` 已整体移除，四张同步表不再有读取方。
downgrade 按 ``0001_initial`` + ``0002_starhope_sync_receipts`` 的结构原样重建，
以便回滚到旧代码时数据面仍然可用。
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_drop_starhope"
down_revision = "0009_friend_relationships"
branch_labels = None
depends_on = None

TABLES = (
    "starhope_questions",
    "starhope_folders",
    "starhope_practice_sessions",
    "starhope_ai_agents",
)


def upgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)


def downgrade() -> None:
    op.create_table(
        "starhope_ai_agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("avatar", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("service", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("top_p", sa.Float(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_starhope_ai_agents_user_id"), "starhope_ai_agents", ["user_id"]
    )
    op.create_table(
        "starhope_folders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_starhope_folders_user_id"), "starhope_folders", ["user_id"]
    )
    op.create_table(
        "starhope_practice_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("question_ids", sa.Text(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=False),
        sa.Column("results", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_limit", sa.Integer(), nullable=True),
        sa.Column("passing_grade", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_starhope_practice_sessions_user_id"),
        "starhope_practice_sessions",
        ["user_id"],
    )
    op.create_table(
        "starhope_questions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("folder_id", sa.String(length=36), nullable=True),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_starhope_questions_user_id"), "starhope_questions", ["user_id"]
    )
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(f"ix_{table}_received_at", table, ["received_at"])
        op.alter_column(table, "received_at", server_default=None)
