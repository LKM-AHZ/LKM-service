"""待处理注册补申请理由（P2 apply 模式表单）。

正常注册是「发起（存 pending）→ 验证码验证（建 user）」两段式；申请理由在第一段收集，
必须随 pending 行暂存到验证完成，故给 ``pending_registrations`` 补 ``apply_reason``。
新库由基线 ``create_all`` 带出该列，本迁移用 ``IF NOT EXISTS`` 只补既存库。

Revision ID: 0003_pending_apply_reason
Revises: 0002_user_admission
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_pending_apply_reason"
down_revision: str | Sequence[str] | None = "0002_user_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pending_registrations "
        "ADD COLUMN IF NOT EXISTS apply_reason TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pending_registrations DROP COLUMN IF EXISTS apply_reason")
