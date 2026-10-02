"""Preserve explicit permission revocations across RBAC seed runs.

Revision ID: 0003_role_permissions_enabled
Revises: 0002_files_hash_index
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_role_permissions_enabled"
down_revision: str | Sequence[str] | None = "0002_files_hash_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "role_permissions",
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("role_permissions", "enabled")
