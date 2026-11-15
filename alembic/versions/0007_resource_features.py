"""Resource-level feature flags on boards and columns.

Revision ID: 0007_resource_features
Revises: 0006_content_subscriptions

- ``boards.features`` / ``columns.features``：``{feature_key: bool}`` 资源级功能开关。
- 非空、默认 ``{}``：存量行迁移后即为空表，读取端视未知键为未启用。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_resource_features"
down_revision: str | Sequence[str] | None = "0006_content_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("boards", "columns")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "features",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "features")
