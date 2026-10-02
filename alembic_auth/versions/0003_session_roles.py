"""Persist selected session roles across refresh rotation.

Revision ID: 0003_session_roles
Revises: 0002_user_roles
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_session_roles"
down_revision: str | Sequence[str] | None = "0002_user_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Baseline create_all on a fresh database may already include this column.
    op.execute("ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS active_roles JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS active_roles")
