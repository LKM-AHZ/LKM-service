"""add content search fts (tsvector + pg_trgm)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-18

M6.9 搜索 P1（PG FTS）：

- ``content_items.search_vector``：可检索文本（title/excerpt/content/summary/
  keywords/tags）的 ``tsvector`` **生成列**，写入时由 PG 自动维护，应用不写。
  用 ``simple`` 分词（中文整段视为一个 lexeme，故 FTS 仅对英文/数字词有效）。
- ``ix_content_search_vector``：生成列的 GIN 索引，服务 ``@@`` 匹配。
- ``ix_content_title_trgm`` / ``ix_content_content_trgm``：``pg_trgm`` GIN 索引，
  服务中文子串 ``ILIKE '%x%'``（PG 内置 contrib 扩展，需先 CREATE EXTENSION）。

中文检索靠 trgm、英文靠 tsvector，两条路互补（见 app/modules/search/service.py）。

downgrade 只删列与索引，**不 DROP EXTENSION**——扩展是库级共享对象，可能被其它
对象/后续迁移引用，删了会牵连。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/modules/content/models.py 的 SEARCH_VECTOR_SQL 同义（迁移内联快照，
# 不 import 应用常量，避免未来改应用表达式时旧迁移语义漂移）。
_SEARCH_VECTOR_SQL = (
    "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(excerpt,'') || "
    "' ' || coalesce(content,'') || ' ' || coalesce(summary,'') || ' ' || "
    "coalesce(keywords,'') || ' ' || coalesce(tags,''))"
)


def upgrade() -> None:
    # 显式装到 public：索引 opclass 写作 ``public.gin_trgm_ops``（不依赖连接的
    # search_path），故扩展必须在该 schema。
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
    op.add_column(
        "content_items",
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed(_SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_content_search_vector",
        "content_items",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_content_title_trgm",
        "content_items",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "public.gin_trgm_ops"},
    )
    op.create_index(
        "ix_content_content_trgm",
        "content_items",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "public.gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_content_content_trgm", table_name="content_items")
    op.drop_index("ix_content_title_trgm", table_name="content_items")
    op.drop_index("ix_content_search_vector", table_name="content_items")
    op.drop_column("content_items", "search_vector")
