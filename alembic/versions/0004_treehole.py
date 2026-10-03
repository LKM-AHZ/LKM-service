"""Persistent anonymous treehole.

Revision ID: 0004_treehole
Revises: 0003_role_permissions_enabled
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_treehole"
down_revision: str | Sequence[str] | None = "0003_role_permissions_enabled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "treehole_letters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("privacy", sa.String(16), nullable=False),
        sa.Column("codename", sa.String(80), nullable=False),
        sa.Column("moods", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("sticker", sa.Text(), nullable=False),
        sa.Column("paper", sa.String(32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("seal_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_treehole_letters_owner_id", "treehole_letters", ["owner_id"])
    op.create_index(
        "ix_treehole_letters_public", "treehole_letters", ["privacy", "created_at"]
    )

    op.create_table(
        "treehole_reactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column(
            "letter_id",
            sa.String(36),
            sa.ForeignKey("treehole_letters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.UniqueConstraint("owner_id", "letter_id", "kind"),
    )
    op.create_index(
        "ix_treehole_reactions_owner_id", "treehole_reactions", ["owner_id"]
    )

    op.create_table(
        "treehole_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "letter_id",
            sa.String(36),
            sa.ForeignKey("treehole_letters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_id", sa.String(64), nullable=False),
        sa.Column("replier_id", sa.String(64), nullable=False),
        sa.Column("replier_codename", sa.String(80), nullable=False),
        sa.Column("author_blocked", sa.Boolean(), nullable=False),
        sa.Column("replier_blocked", sa.Boolean(), nullable=False),
        sa.Column("author_hidden", sa.Boolean(), nullable=False),
        sa.Column("replier_hidden", sa.Boolean(), nullable=False),
        sa.Column("author_cleared_at", sa.DateTime(timezone=True)),
        sa.Column("replier_cleared_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("letter_id", "replier_id"),
    )
    op.create_index(
        "ix_treehole_conversations_author_id", "treehole_conversations", ["author_id"]
    )
    op.create_index(
        "ix_treehole_conversations_replier_id", "treehole_conversations", ["replier_id"]
    )
    op.create_table(
        "treehole_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("treehole_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("recalled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "treehole_bottles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("picked_by", sa.String(64)),
        sa.Column("reply", sa.Text()),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_treehole_bottles_owner_id", "treehole_bottles", ["owner_id"])
    op.create_table(
        "treehole_wishes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lights", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_treehole_wishes_owner_id", "treehole_wishes", ["owner_id"])
    op.create_table(
        "treehole_wish_lights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "wish_id",
            sa.String(36),
            sa.ForeignKey("treehole_wishes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("wish_id", "owner_id"),
    )
    op.create_table(
        "treehole_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reporter_id", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reporter_id", "target_type", "target_id"),
    )


def downgrade() -> None:
    for table in (
        "treehole_reports",
        "treehole_wish_lights",
        "treehole_wishes",
        "treehole_bottles",
        "treehole_messages",
        "treehole_conversations",
        "treehole_reactions",
        "treehole_letters",
    ):
        op.drop_table(table)
