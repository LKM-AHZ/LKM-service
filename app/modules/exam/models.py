from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (  # 注意 db.base 而非 db.models
    Base,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)


class Exam(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exams"

    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="exam"
    )  # exam | competition
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pass_score: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    time_limit_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 认证考试：通过后自动升级的目标（竞赛为 None）。unlock_level 升 account_level，unlock_role 升 profile.role。
    unlock_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    unlock_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 竞赛时间窗（认证考试通常常开，starts_at/ends_at 为空即不限时窗）
    starts_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    ends_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    questions: Mapped[list[ExamQuestion]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )


class ExamQuestion(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_questions"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # single | judge
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 字符串：[{"key":"A","text":"..."}] 单选多选项；judge 为空列表
    options: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    answer: Mapped[str] = mapped_column(
        String(200), nullable=False
    )  # single: "A"；judge: "T"/"F"
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    exam: Mapped[Exam] = relationship(back_populates="questions")


class ExamAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_attempts"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress"
    )
    # JSON 字符串：{question_id: "用户答案"}（judge 存 "T"/"F"）
    answers: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    time_spent_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExamCertificate(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_certificates"

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cert_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    exam: Mapped[Exam] = relationship()
