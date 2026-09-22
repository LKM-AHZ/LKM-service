from __future__ import annotations

import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionCreate(BaseModel):
    kind: str = Field(..., pattern="^(single|judge)$")
    content: str = Field(..., min_length=1)
    options: list[dict[str, str]] = Field(default_factory=list)
    answer: str = Field(..., min_length=1, max_length=200)
    analysis: str | None = Field(default=None, max_length=2000)
    difficulty: int = Field(default=1, ge=1, le=3)
    score: int = Field(default=10, ge=1)
    sort_order: int = Field(default=0)

    @model_validator(mode="after")
    def _check_single_choice_answer(self) -> QuestionCreate:
        """单选题目必须给选项且 answer 命中某个 key。

        判分是把考生作答与 answer 直接比较，选项为空或 answer 不在 key 集合里
        等于出一道永远无法得分的题。（判断题为 T/F，允许 options 为空。）
        """
        if self.kind == "single":
            keys = {o.get("key") for o in self.options}
            if not keys or self.answer not in keys:
                raise ValueError("single 题的 answer 必须是 options 中某个 key")
        return self


class QuestionOut(BaseModel):
    """管理端题目视图（含 answer/analysis）。

    刻意与考生侧 DTO 区分：考生侧必须用 :class:`QuestionForAttempt`（不含答案）。
    本类若被接进任何考生可见的响应，答案即泄露、交卷必满分——接线时务必只挂在管理端。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    options: list[dict[str, str]]
    answer: str
    analysis: str | None = None
    difficulty: int
    score: int
    sort_order: int


class ExamCreate(BaseModel):
    type: str = Field(default="exam", pattern="^(exam|competition)$")
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

    @model_validator(mode="after")
    def _check_schedule_window(self) -> ExamCreate:
        # 反序/零长时间窗会让 _check_window 对所有考生恒判 EXAM_NOT_OPEN，
        # 考试创建成功后却永久不可用，且报错完全不指向配置错误 → 建考前就拦下
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at 必须晚于 starts_at")
        return self

    @model_validator(mode="after")
    def _check_pass_score(self) -> ExamCreate:
        """题目非空且 pass_score 不超过满分：否则「全对也不及格」「空卷考试」都能被建出来。"""
        if not self.questions:
            raise ValueError("考试至少需要一道题目")
        total = sum(q.score for q in self.questions)
        if self.pass_score > total:
            raise ValueError(f"pass_score 不能超过满分 {total}")
        return self


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
    question_count: int = 0
    created_at: datetime.datetime
    updated_at: datetime.datetime


class QuestionForAttempt(BaseModel):
    """开考时下发给考生的题目（客户端安全 DTO）。

    刻意省略 answer / analysis，防止考生在作答前就拿到正确答案——否则任何交卷
    都能满分，认证判分与竞赛榜单将失去意义。答案仅留在服务端 exam.questions 供判分。
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


class SubmitAnswersRequest(BaseModel):
    answers: dict[uuid.UUID, str] = Field(default_factory=dict)


class SubmitResult(BaseModel):
    attempt_id: uuid.UUID
    exam_id: uuid.UUID
    score: int
    pass_score: int
    passed: bool
    unlock_level: str | None = None
    unlock_role: str | None = None
    certificate_id: uuid.UUID | None = None


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
