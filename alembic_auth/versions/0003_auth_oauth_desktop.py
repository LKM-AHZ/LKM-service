"""auth_oauth_desktop：GitHub OAuth 的桌面 loopback 回跳支撑。

Revision ID: 0003_auth_oauth_desktop
Revises: 0002_auth_challenges

``oauth_states`` 增加两个可空列；一次性兑换码落 ``oauth_desktop_codes``。
全部幂等（``IF NOT EXISTS``），新库由 ``create_all`` 直接带出。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_auth_oauth_desktop"
down_revision: str | Sequence[str] | None = "0002_auth_challenges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALTER_STATE_SQL = """
ALTER TABLE oauth_states
    ADD COLUMN IF NOT EXISTS desktop_redirect VARCHAR(2048),
    ADD COLUMN IF NOT EXISTS desktop_challenge VARCHAR(128)
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS oauth_desktop_codes (
    id UUID PRIMARY KEY,
    code VARCHAR(128) NOT NULL UNIQUE,
    challenge VARCHAR(128) NOT NULL,
    result JSON NOT NULL,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

def upgrade() -> None:
    op.execute(ALTER_STATE_SQL)
    op.execute(CREATE_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS oauth_desktop_codes")
    op.execute(
        "ALTER TABLE oauth_states "
        "DROP COLUMN IF EXISTS desktop_redirect, "
        "DROP COLUMN IF EXISTS desktop_challenge"
    )
