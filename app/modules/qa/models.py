"""问答（QA）域 ORM 模型。

步骤 9 切片 1 自 ``content.models`` 迁出，**表名、列、索引一字不变**，因此不需要任何
迁移；``content_items.qa_question_id`` 仍是普通外键列（字符串 ``ForeignKey`` 指向本表，
content 侧不 import 本模块的类）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (  # 注意是 db.base 不是 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class QAQuestion(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_questions"
    # 问答列表按 category + id 倒序，status 用于状态筛选；无索引则列表页全表扫
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_question_category_id", "category", "id"),
        Index("ix_qa_question_status_id", "status", "id"),
        Index("ix_qa_questions_due", "status", "bounty_expires_at"),
        Index("ix_qa_questions_bounty_sort", "category", "bounty_total", "id"),
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    bounty_people: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bounty_per_person: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounty_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounty_distributed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounty_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )  # open|accepted|closed
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="help"
    )  # help|volunteer（前端 tab 分类）
    accepted_answer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("qa_answers.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )


class QAAnswer(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_answers"
    # 按 question_id 拉回答 / 判定是否已采纳；无索引时按提问拉回答全表扫
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_answer_question", "question_id"),
        Index("ix_qa_answer_question_accepted", "question_id", "is_accepted"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("qa_questions.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False
    )  # S5: auth user_id
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class QAQuestionImage(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "qa_question_images"
    __table_args__: tuple[Index, ...] = (
        Index("ix_qa_question_image_question", "question_id"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("qa_questions.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
