"""Index content hash lookups used by file deduplication and reference counting.

Revision ID: 0002_files_hash_index
Revises: 0001_uuid_baseline
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_files_hash_index"
down_revision: str | Sequence[str] | None = "0001_uuid_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_library_files_sha3_hash", "library_files", ["sha3_hash"])


def downgrade() -> None:
    op.drop_index("ix_library_files_sha3_hash", table_name="library_files")
