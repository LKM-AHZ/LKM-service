"""Add QA bounty deadline and urgency.

Revision ID: 0006_qa_bounty_deadline
Revises: 0005_file_library
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_qa_bounty_deadline"
down_revision: str | Sequence[str] | None = "0005_file_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qa_questions", sa.Column("bounty_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "qa_questions",
        sa.Column("urgent", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # 旧悬赏保留既有人工关闭语义；只给新版提问设置自动到期。
    op.create_index(
        "ix_qa_questions_due", "qa_questions", ["status", "bounty_expires_at"]
    )
    op.create_index(
        "ix_qa_questions_bounty_sort", "qa_questions", ["category", "bounty_total", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_qa_questions_bounty_sort", table_name="qa_questions")
    op.drop_index("ix_qa_questions_due", table_name="qa_questions")
    op.drop_column("qa_questions", "urgent")
    op.drop_column("qa_questions", "bounty_expires_at")
