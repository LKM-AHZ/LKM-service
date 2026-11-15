from __future__ import annotations

import datetime
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionCreate(BaseModel):
    kind: str = Field(..., pattern="^(single|multiple|judge|fill|text)$")
    content: str = Field(..., min_length=1)
    group_id: uuid.UUID | None = None
    options: list[dict[str, str]] = Field(default_factory=list)
    answer: str | None = Field(default=None, max_length=200)
    answer_data: dict[str, Any] | None = None
    grading_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    analysis: str | None = Field(default=None, max_length=2000)
    difficulty: int = Field(default=1, ge=1, le=3)
    score: int = Field(default=10, ge=1)
    sort_order: int = Field(default=0)
    config: dict[str, Any] | None = None
    review_status: str = Field(default="approved", pattern="^(draft|approved|rejected)$")

    @model_validator(mode="after")
    def _check_answer_representation(self) -> QuestionCreate:
        """按题型校验答案表示，避免建出永远无法得分的题。

        - single：answer 必填且命中 options 某个 key（沿用旧规则）。
        - judge：answer 必填（T/F）。
        - multiple：answer_data={"keys": [...]}，且 keys 非空、命中选项。
        - fill：answer_data={"text": "..."} 非空；match 规则可放 config。
        - text：人工评分，answer_data 可空（reference 仅作阅卷参考）。
        """
        if self.kind == "single":
            keys = {o.get("key") for o in self.options}
            if not keys or self.answer not in keys:
                raise ValueError("single 题的 answer 必须是 options 中某个 key")
        elif self.kind == "judge":
            if self.answer not in ("T", "F"):
                raise ValueError("judge 题的 answer 必须是 T 或 F")
        elif self.kind == "multiple":
            keys = {o.get("key") for o in self.options}
            ans_keys = (self.answer_data or {}).get("keys")
            if not isinstance(ans_keys, list) or not ans_keys:
                raise ValueError("multiple 题需要 answer_data.keys 非空列表")
            if not keys or not set(ans_keys).issubset(keys):
                raise ValueError("multiple 题的 answer_data.keys 必须是 options 的子集")
        elif self.kind == "fill":
            text = (self.answer_data or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("fill 题需要 answer_data.text 非空")
        elif self.kind == "text":
            # 自由文本一律人工阅卷，忽略调用方误传的 auto。
            self.grading_mode = "manual"
        return self

    def resolved_answer(self) -> str:
        """旧 answer 列不允许 NULL：新题型统一落空字符串。"""
        return self.answer or ""

    def resolved_grading_mode(self) -> str:
        return "manual" if self.kind == "text" else self.grading_mode


class QuestionUpdate(BaseModel):
    group_id: uuid.UUID | None = None
    content: str | None = Field(default=None, min_length=1)
    options: list[dict[str, str]] | None = None
    answer: str | None = Field(default=None, max_length=200)
    answer_data: dict[str, Any] | None = None
    grading_mode: str | None = Field(default=None, pattern="^(auto|manual)$")
    analysis: str | None = Field(default=None, max_length=2000)
    difficulty: int | None = Field(default=None, ge=1, le=3)
    score: int | None = Field(default=None, ge=1)
    sort_order: int | None = None
    config: dict[str, Any] | None = None
    review_status: str | None = Field(
        default=None, pattern="^(draft|approved|rejected)$"
    )


class QuestionOut(BaseModel):
    """管理端题目视图（含 answer/answer_data/analysis）。

    刻意与考生侧 DTO 区分：考生侧必须用 :class:`QuestionForAttempt`（不含答案）。
    本类若被接进任何考生可见的响应，答案即泄露、交卷必满分——接线时务必只挂在管理端。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_id: uuid.UUID | None = None
    kind: str
    content: str
    options: list[dict[str, str]]
    answer: str
    answer_data: dict[str, Any] | None = None
    grading_mode: str
    analysis: str | None = None
    difficulty: int
    score: int
    sort_order: int
    review_status: str = "approved"
    source_exam_id: uuid.UUID | None = None
    config: dict[str, Any] | None = None
    deleted_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


class QuestionReviewRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")


class ImportQuestionsRequest(BaseModel):
    group_id: uuid.UUID
    questions: list[QuestionCreate] = Field(default_factory=list)


class GroupCreate(BaseModel):
    key: str | None = Field(default=None, max_length=128)
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    config: dict[str, Any] | None = None


class GroupUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    config: dict[str, Any] | None = None


class GroupOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    title: str
    description: str | None = None
    config: dict[str, Any] | None = None
    question_count: int = 0
    deleted_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


class ExamCreate(BaseModel):
    type: str = Field(default="exam", pattern="^(exam|competition|practice)$")
    title: str = Field(..., min_length=1, max_length=200)
    subject: str = Field(default="", max_length=50)
    difficulty: int = Field(default=1, ge=1, le=3)
    description: str | None = Field(default=None, max_length=2000)
    pass_score: int = Field(default=60, ge=1)
    time_limit_min: int = Field(default=30, ge=1)
    unlock_level: str | None = Field(default=None, pattern="^(local|normal|admin)$")
    unlock_role: str | None = Field(default=None, max_length=20)
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    questions: list[QuestionCreate] = Field(default_factory=list)
    group_ids: list[uuid.UUID] = Field(default_factory=list)
    is_listed: bool = True
    requires_registration: bool = False
    max_attempts: int | None = Field(default=None, ge=1)
    attempt_period_days: int = Field(default=365, ge=1)
    shuffle_questions: bool = False
    random_question_count: int | None = Field(default=None, ge=1)
    config: dict[str, Any] | None = None
    prerequisite_exam_id: uuid.UUID | None = None
    required_level: str | None = Field(default=None, pattern="^(local|normal|admin)$")
    required_role: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _check_schedule_window(self) -> ExamCreate:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at 必须晚于 starts_at")
        return self

    @model_validator(mode="after")
    def _check_pass_score(self) -> ExamCreate:
        """有内联题目时校验 pass_score 不超过满分；纯题组卷在装配后校验。"""
        if not self.questions and not self.group_ids:
            raise ValueError("考试至少需要一道题目或一个题组")
        if self.questions:
            total = sum(q.score for q in self.questions)
            if self.pass_score > total:
                raise ValueError(f"pass_score 不能超过满分 {total}")
        return self


class ExamUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, max_length=50)
    difficulty: int | None = Field(default=None, ge=1, le=3)
    description: str | None = Field(default=None, max_length=2000)
    pass_score: int | None = Field(default=None, ge=1)
    time_limit_min: int | None = Field(default=None, ge=1)
    unlock_level: str | None = Field(default=None, pattern="^(local|normal|admin)$")
    unlock_role: str | None = Field(default=None, max_length=20)
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    is_listed: bool | None = None
    requires_registration: bool | None = None
    max_attempts: int | None = Field(default=None, ge=1)
    attempt_period_days: int | None = Field(default=None, ge=1)
    shuffle_questions: bool | None = None
    random_question_count: int | None = Field(default=None, ge=1)
    config: dict[str, Any] | None = None


class AssignGroupsRequest(BaseModel):
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class ExamOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    subject: str
    difficulty: int
    description: str | None = None
    pass_score: int
    time_limit_min: int
    is_published: bool
    unlock_level: str | None = None
    unlock_role: str | None = None
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    is_listed: bool = True
    is_archived: bool = False
    requires_registration: bool = False
    max_attempts: int | None = None
    attempt_period_days: int = 365
    shuffle_questions: bool = False
    random_question_count: int | None = None
    config: dict[str, Any] | None = None
    question_count: int = 0
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ExamDetailOut(ExamOut):
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class QuestionForAttempt(BaseModel):
    """开考时下发给考生的题目（客户端安全 DTO）。

    刻意省略 answer / answer_data / analysis，防止考生在作答前就拿到正确答案——否则任何
    交卷都能满分，认证判分与竞赛榜单将失去意义。答案仅留在服务端快照供判分。
    """

    id: uuid.UUID
    kind: str
    content: str
    options: list[dict[str, str]]
    difficulty: int
    score: int
    sort_order: int


class AttemptStartResp(BaseModel):
    attempt_id: uuid.UUID
    exam_id: uuid.UUID
    questions: list[QuestionForAttempt]
    time_limit_min: int
    deadline: datetime.datetime | None = None
    mode: str = "certification"
    is_trial: bool = False


class SubmitAnswersRequest(BaseModel):
    # 旧客户端传 str；新题型传 dict / list（如 {"keys": ["A","C"]} 或 {"text": "..."}）
    answers: dict[uuid.UUID, Any] = Field(default_factory=dict)


class SubmitResult(BaseModel):
    attempt_id: uuid.UUID
    exam_id: uuid.UUID
    score: int
    pass_score: int
    passed: bool
    status: str = "passed"
    pending_review: bool = False
    unlock_level: str | None = None
    unlock_role: str | None = None
    certificate_id: uuid.UUID | None = None


class AttemptQuestionResult(BaseModel):
    question_id: uuid.UUID
    kind: str
    content: str
    options: list[dict[str, str]] = Field(default_factory=list)
    score: int
    value: Any = None
    is_correct: bool | None = None
    awarded_points: float | None = None
    review_note: str | None = None
    correct_answer: Any = None
    analysis: str | None = None


class AttemptResultOut(BaseModel):
    attempt_id: uuid.UUID
    exam_id: uuid.UUID
    status: str
    score: int
    pass_score: int = 0
    passed: bool
    pending_review: bool = False
    mode: str = "certification"
    is_trial: bool = False
    started_at: datetime.datetime
    submitted_at: datetime.datetime | None = None
    deadline_at: datetime.datetime | None = None
    reviewed_at: datetime.datetime | None = None
    questions: list[AttemptQuestionResult] = Field(default_factory=list)


class CertificateOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exam_id: uuid.UUID
    user_id: uuid.UUID
    exam_title: str = ""
    score: int
    passed: bool
    cert_no: str
    issued_at: datetime.datetime


class LeaderboardEntry(BaseModel):
    user_id: uuid.UUID
    display_name: str
    score: int
    certified: bool = False


class RegistrationOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exam_id: uuid.UUID
    user_id: uuid.UUID
    status: str
    registered_at: datetime.datetime
    cancelled_at: datetime.datetime | None = None


class MyRegistrationOut(RegistrationOut):
    exam_title: str = ""


class PracticeStartRequest(BaseModel):
    exam_id: uuid.UUID
    group_id: uuid.UUID | None = None
    wrong_only: bool = False


class PracticeGroupOut(BaseModel):
    id: uuid.UUID
    key: str
    title: str
    description: str | None = None
    question_count: int = 0
    exam_ids: list[uuid.UUID] = Field(default_factory=list)


class WrongAnswerOut(BaseModel):
    question_id: uuid.UUID
    group_id: uuid.UUID | None = None
    kind: str
    content: str
    options: list[dict[str, str]] = Field(default_factory=list)
    difficulty: int = 1
    wrong_count: int
    correct_streak: int
    mastered: bool
    last_wrong_at: datetime.datetime | None = None
    last_correct_at: datetime.datetime | None = None
    unavailable: bool = False


class TrialOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exam_id: uuid.UUID
    key: str
    title: str
    description: str | None = None
    is_published: bool


class TrialCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    is_published: bool = True


class ReviewAnswerRequest(BaseModel):
    is_correct: bool
    awarded_points: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=4000)


class AdminAttemptOut(BaseModel):
    attempt_id: uuid.UUID
    exam_id: uuid.UUID
    exam_title: str = ""
    user_id: uuid.UUID
    status: str
    mode: str
    score: int
    passed: bool
    is_trial: bool
    started_at: datetime.datetime
    submitted_at: datetime.datetime | None = None
    deadline_at: datetime.datetime | None = None
    reviewed_at: datetime.datetime | None = None
    reviewed_by: uuid.UUID | None = None


class PrerequisiteOut(BaseModel):
    required_exam_id: uuid.UUID | None = None
    required_level: str | None = None
    required_role: str | None = None
