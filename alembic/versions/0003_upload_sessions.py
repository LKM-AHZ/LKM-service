"""upload sessions（预签名直传会话落 DB）

新增 ``upload_sessions`` 表（见 ``app/modules/files/models.py::UploadSession``）。关掉 Redis
持久化后，直传元数据不能再存在无 TTL 的 Redis 键上 —— 重启会让在途会话全部蒸发、``up/<uid>``
孤儿对象失控（清扫原本靠 SCAN 那些键）。

**幂等**：``create_all`` 通道（dev/测试）会直接建出本表；生产走本迁移。先探存在性再建，
两条通道都安全。

Revision ID: 0003_upload_sessions
Revises: 0002_optimistic_lock_version
Create Date: 2026-09-29 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_upload_sessions"
down_revision: str | Sequence[str] | None = "0002_optimistic_lock_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "upload_sessions"
_IX_CREATED = "ix_upload_sessions_created"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("upload_id", sa.String(64), primary_key=True),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("meta", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_IX_CREATED, _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        op.drop_table(_TABLE)
