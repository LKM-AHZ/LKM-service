"""Document identity, classification, project link and version metadata.

Revision ID: 0005_file_library
Revises: 0004_treehole
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_file_library"
down_revision: str | Sequence[str] | None = "0004_treehole"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("library_files", sa.Column("document_code", sa.String(40)))
    op.add_column(
        "library_files",
        sa.Column("classification", sa.String(20), server_default="public", nullable=False),
    )
    op.add_column("library_files", sa.Column("project_id", sa.Uuid()))
    op.add_column(
        "library_files", sa.Column("version", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("library_files", sa.Column("root_file_id", sa.Uuid()))
    op.add_column(
        "library_files",
        sa.Column("extracted_text", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "library_files",
        sa.Column("archive_state", sa.String(20), server_default="active", nullable=False),
    )
    op.add_column("library_files", sa.Column("backed_up_at", sa.DateTime(timezone=True)))
    # 旧记录按年份和创建顺序稳定编号；保留现有 id 和审核状态。
    op.execute(
        """
        WITH numbered AS (
            SELECT id, EXTRACT(YEAR FROM created_at AT TIME ZONE 'UTC')::int AS yr,
                   row_number() OVER (
                       PARTITION BY EXTRACT(YEAR FROM created_at AT TIME ZONE 'UTC')
                       ORDER BY created_at, id
                   ) AS seq
            FROM library_files
        )
        UPDATE library_files AS f
        SET document_code = 'WL-SYBG-' || numbered.yr || '-' ||
                            lpad(numbered.seq::text, 3, '0'),
            root_file_id = f.id
        FROM numbered WHERE f.id = numbered.id
        """
    )
    op.create_foreign_key(
        "fk_library_files_project", "library_files", "projects", ["project_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_library_files_root", "library_files", "library_files", ["root_file_id"], ["id"]
    )
    op.create_unique_constraint(
        "uq_library_document_version", "library_files", ["document_code", "version"]
    )
    op.create_index("ix_library_files_project", "library_files", ["project_id"])
    op.create_index(
        "ix_library_files_root_version", "library_files", ["root_file_id", "version"]
    )
    op.create_index(
        "ix_library_files_name_trgm", "library_files", ["original_name"],
        postgresql_using="gin", postgresql_ops={"original_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_library_files_text_trgm", "library_files", ["extracted_text"],
        postgresql_using="gin", postgresql_ops={"extracted_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_library_files_text_trgm", table_name="library_files")
    op.drop_index("ix_library_files_name_trgm", table_name="library_files")
    op.drop_index("ix_library_files_root_version", table_name="library_files")
    op.drop_index("ix_library_files_project", table_name="library_files")
    op.drop_constraint("uq_library_document_version", "library_files", type_="unique")
    op.drop_constraint("fk_library_files_root", "library_files", type_="foreignkey")
    op.drop_constraint("fk_library_files_project", "library_files", type_="foreignkey")
    for name in (
        "backed_up_at", "archive_state", "extracted_text", "root_file_id", "version",
        "project_id", "classification", "document_code",
    ):
        op.drop_column("library_files", name)
