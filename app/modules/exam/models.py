from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    column,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import (  # 注意 db.base 而非 db.models
    Base,
    SoftDeleteMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    now_iso,
)

# 题型：single/judge 沿用旧 answer 字符串；multiple/fill/text 走 answer_data JSONB。
QUESTION_KINDS = ("single", "multiple", "judge", "fill", "text")
GRADING_MODES = ("auto", "manual")


class Exam(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exams"

    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="exam"
    )  # exam | competition | practice
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
    # —— 题库与考试拆分新增（均可空/带默认值，保证旧数据可用）——
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_registration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365
    )
    shuffle_questions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    random_question_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    questions: Mapped[list[ExamQuestion]] = relationship(
        back_populates="exam",
        cascade="all, delete-orphan",
        foreign_keys="ExamQuestion.exam_id",
    )
    group_assignments: Mapped[list[ExamGroupAssignment]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )


class ExamQuestionGroup(SoftDeleteMixin, UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_question_groups"

    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso, onupdate=now_iso
    )

    questions: Mapped[list[ExamQuestion]] = relationship(
        back_populates="group", order_by="ExamQuestion.sort_order"
    )


class ExamGroupAssignment(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_group_assignments"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("exam_id", "group_id", name="uq_exam_group_assignments"),
    )

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exam_question_groups.id"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    exam: Mapped[Exam] = relationship(back_populates="group_assignments")
    group: Mapped[ExamQuestionGroup] = relationship()


class ExamQuestion(SoftDeleteMixin, UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_questions"
    __table_args__: tuple[Any, ...] = (
        Index("ix_exam_questions_group_id_sort_order", "group_id", "sort_order"),
    )

    # exam_id 保留作旧读路径兼容；题库化后题目归属题组，可为空。
    exam_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("exam_question_groups.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # single|multiple|judge|fill|text
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 字符串：[{"key":"A","text":"..."}] 单选多选项；judge 为空列表
    options: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    answer: Mapped[str] = mapped_column(
        String(200), nullable=False, default=""
    )  # single: "A"；judge: "T"/"F"；新题型留空
    answer_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    grading_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="auto"
    )
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_exam_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=True, index=True
    )
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", index=True
    )  # draft | approved | rejected
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )

    exam: Mapped[Exam | None] = relationship(
        back_populates="questions", foreign_keys=[exam_id]
    )
    group: Mapped[ExamQuestionGroup | None] = relationship(back_populates="questions")


class ExamAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_attempts"
    __table_args__: tuple[Any, ...] = (
        Index("ix_exam_attempts_exam_user_status", "exam_id", "user_id", "status"),
        Index("ix_exam_attempts_user_started_at", "user_id", "started_at"),
    )

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # in_progress | pending_review | passed | failed | expired（旧数据兼容 submitted）
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress"
    )
    # JSON 字符串：{question_id: "用户答案"}（judge 存 "T"/"F"）
    answers: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="certification"
    )  # certification | competition | practice
    deadline_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    # 开考时固化的题目/分值/判分校验信息，交卷/阅卷只按 snapshot 判分
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trial_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("exam_trials.id"), nullable=True
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    time_spent_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    answer_rows: Mapped[list[ExamAttemptAnswer]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class ExamAttemptAnswer(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_attempt_answers"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("attempt_id", "question_id", name="uq_exam_attempt_answers"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exam_attempts.id"), nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    awarded_points: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    attempt: Mapped[ExamAttempt] = relationship(back_populates="answer_rows")


class ExamWrongAnswer(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_wrong_answers"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("user_id", "question_id", name="uq_exam_wrong_answers"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exam_questions.id"), nullable=False, index=True
    )
    wrong_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_wrong_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    last_correct_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    correct_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mastered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ExamRegistration(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_registrations"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("exam_id", "user_id", name="uq_exam_registrations"),
    )

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="registered"
    )  # registered | cancelled | disqualified
    registered_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


class ExamTrial(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_trials"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("key", name="uq_exam_trials_key"),
    )

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=now_iso
    )


class ExamPrerequisite(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_prerequisites"
    __table_args__: tuple[Any, ...] = (
        UniqueConstraint("exam_id", name="uq_exam_prerequisites_exam"),
    )

    exam_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=False, index=True
    )
    # 三个条件可空；全部非空时须同时满足（AND）。全空表示无门槛。
    required_exam_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("exams.id"), nullable=True
    )
    required_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    required_role: Mapped[str | None] = mapped_column(String(20), nullable=True)


class ExamCertificate(UUIDPrimaryKeyMixin, Base):
    __tablename__: str = "exam_certificates"
    __table_args__: tuple[Any, ...] = (
        # 同一 (exam, user) 只保留一张通过证书；重复通过只更新最高分。
        Index(
            "uq_exam_certificates_passed_user",
            "exam_id",
            "user_id",
            unique=True,
            postgresql_where=column("passed").is_(True),
        ),
    )

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
