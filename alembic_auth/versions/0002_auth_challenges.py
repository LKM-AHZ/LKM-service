"""auth_challenges：挑战-因子轮转的登录挑战表（见 auth/challenge.py）。

Revision ID: 0002_auth_challenges
Revises: 0001_auth_baseline

新库由 ``create_all`` 直接带出该表；本迁移是给「表已由 create_all 建过、再切
alembic 通道」的既存库补列/补表用的，全部幂等（``IF NOT EXISTS``）。
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_auth_challenges"
down_revision: str | Sequence[str] | None = "0001_auth_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS auth_challenges (
    id UUID PRIMARY KEY,
    account VARCHAR(200) NOT NULL,
    purpose VARCHAR(20) NOT NULL DEFAULT 'login',
    device_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    steps_done JSON NOT NULL DEFAULT '[]'::json,
    steps_left JSON NOT NULL DEFAULT '[]'::json,
    attempts INTEGER NOT NULL DEFAULT 0,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_auth_challenges_device_status
    ON auth_challenges (device_hash, status)
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE_SQL)
    op.execute(CREATE_INDEX_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_challenges")
