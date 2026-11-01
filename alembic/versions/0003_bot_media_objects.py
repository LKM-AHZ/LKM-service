"""bot 产出媒体登记表：``bot_media_objects``。

背景：LKM-bot 的 TTS 音频 / T2I 图片此前只落在 bot 容器本地盘（``data/temp``），靠进程内存的
token 表对外提供短期下载。引入 bot → backend 服务通道后，媒体改由后端对象存储承载，需要一张
登记表来支撑内容寻址去重、引用计数与保留期清理。

为什么不并入 ``library_files``：那张表的核心语义是「社区文件库条目」——有 ``uploader_id``
（auth realm 用户）、document_code 版本链、审阅状态机与公开可见性。bot 媒体三者皆无，混入会让
文件库查询全部要加 ``uploader 非空`` 过滤，且两套清理策略互相污染。故独立建表，仅共享底层
``storage`` 后端。

Revision ID: 0003_bot_media_objects
Revises: 0002_application_guards
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_bot_media_objects"
down_revision: str | Sequence[str] | None = "0002_application_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_media_objects",
        # uuid_generate_v7 由 init_db/部署 initdb 建在 public schema，故 server_default 必须
        # 显式限定 schema（schema-per-test 的 search_path 不含 public）。
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("public.uuid_generate_v7()"),
        ),
        sa.Column("sha3_hash", sa.String(length=64), nullable=False),
        sa.Column("bucket_key", sa.String(length=255), nullable=False),
        sa.Column(
            "storage_path", sa.String(length=1024), nullable=False, server_default=""
        ),
        sa.Column(
            "original_name", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column(
            "mime_type",
            sa.String(length=100),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source", sa.String(length=20), nullable=False, server_default="other"
        ),
        sa.Column("ref_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extracted_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("sha3_hash", name="uq_bot_media_sha3_hash"),
    )
    op.create_index(
        "ix_bot_media_expires", "bot_media_objects", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_bot_media_expires", table_name="bot_media_objects")
    op.drop_table("bot_media_objects")
