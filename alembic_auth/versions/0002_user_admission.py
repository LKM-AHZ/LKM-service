"""用户准入状态（P2 申请制注册）：admission_status + 申请/审核字段。

``LKM_ADMISSION_MODE=apply`` 时新注册用户落 ``pending``，管理员审核后转 ``active``；
``rejected`` 记录拒绝原因与时间。存量用户一律回填 ``active``（DEFAULT），切模式不改变
既有账号的可登录性。

注意：``0001_auth_baseline`` 用 ``auth_metadata.create_all`` 建表，**新库**在基线就带出
本迁移的列；本迁移只对「表已存在、模型后加列」的既存库生效，故全部用
``IF NOT EXISTS`` 保持两条通道幂等（与基线里 ``contact_links`` 的处理同款）。

Revision ID: 0002_user_admission
Revises: 0001_auth_baseline
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_user_admission"
down_revision: str | Sequence[str] | None = "0001_auth_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("admission_status", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
    ("apply_reason", "TEXT"),
    ("applied_at", "TIMESTAMP WITH TIME ZONE"),
    ("admission_reviewed_at", "TIMESTAMP WITH TIME ZONE"),
    ("admission_reviewed_by", "UUID"),
    ("reject_reason", "TEXT"),
    ("last_rejected_at", "TIMESTAMP WITH TIME ZONE"),
)


def upgrade() -> None:
    for name, ddl in _COLUMNS:
        op.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_admission_status "
        "ON users (admission_status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_admission_status")
    for name, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {name}")
