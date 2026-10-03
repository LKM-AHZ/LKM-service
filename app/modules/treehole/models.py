"""Persistent anonymous treehole content and per-visitor actions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, UTCDateTime, now_iso


class Letter(Base):
    __tablename__ = "treehole_letters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))
    privacy: Mapped[str] = mapped_column(String(16))
    codename: Mapped[str] = mapped_column(String(80))
    moods: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    sticker: Mapped[str] = mapped_column(Text, default="")
    paper: Mapped[str] = mapped_column(String(32), default="paper")
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    seal_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=now_iso, onupdate=now_iso
    )

    __table_args__ = (Index("ix_treehole_letters_public", "privacy", "created_at"),)


class Reaction(Base):
    __tablename__ = "treehole_reactions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    letter_id: Mapped[str] = mapped_column(
        ForeignKey("treehole_letters.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    __table_args__ = (UniqueConstraint("owner_id", "letter_id", "kind"),)


class Conversation(Base):
    __tablename__ = "treehole_conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    letter_id: Mapped[str] = mapped_column(
        ForeignKey("treehole_letters.id", ondelete="CASCADE")
    )
    author_id: Mapped[str] = mapped_column(String(64), index=True)
    replier_id: Mapped[str] = mapped_column(String(64), index=True)
    replier_codename: Mapped[str] = mapped_column(String(80))
    author_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    replier_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    author_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    replier_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    author_cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replier_cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)
    __table_args__ = (UniqueConstraint("letter_id", "replier_id"),)


class Message(Base):
    __tablename__ = "treehole_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("treehole_conversations.id", ondelete="CASCADE")
    )
    sender_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)


class Bottle(Base):
    __tablename__ = "treehole_bottles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)
    picked_by: Mapped[str | None] = mapped_column(String(64))
    reply: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Wish(Base):
    __tablename__ = "treehole_wishes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    lights: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)


class WishLight(Base):
    __tablename__ = "treehole_wish_lights"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    wish_id: Mapped[str] = mapped_column(
        ForeignKey("treehole_wishes.id", ondelete="CASCADE")
    )
    owner_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("wish_id", "owner_id"),)


class Report(Base):
    __tablename__ = "treehole_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reporter_id: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_iso)
    __table_args__ = (UniqueConstraint("reporter_id", "target_type", "target_id"),)
