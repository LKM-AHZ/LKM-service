import datetime
import uuid
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BOUNTY_POINTS = 1000


class QuestionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    situation: str = Field(..., min_length=1, max_length=5000)
    content: str = Field(..., min_length=1, max_length=20000)
    category: Literal["help", "volunteer"] = "help"
    bounty_people: int = Field(default=1, ge=1, le=10)
    bounty_per_person: int = Field(default=0, ge=0, le=MAX_BOUNTY_POINTS)
    bounty_days: int = Field(default=7, ge=7, le=90)
    urgent: bool = False
    images: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list, max_length=9
    )

    @model_validator(mode="after")
    def check_bounty(self) -> "QuestionCreate":
        if self.images:
            raise ValueError("请先创建问题，再通过图片上传接口添加配图")
        if self.bounty_people * self.bounty_per_person > MAX_BOUNTY_POINTS:
            raise ValueError(f"悬赏总额不得超过 {MAX_BOUNTY_POINTS} 积分")
        if self.urgent and (self.bounty_per_person == 0 or self.bounty_days != 7):
            raise ValueError("加急仅适用于 7 天悬赏")
        return self


class QuestionOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    title: str
    situation: str
    content: str
    bounty_people: int
    bounty_per_person: int
    bounty_total: int
    bounty_distributed: int
    bounty_expires_at: datetime.datetime | None = None
    urgent: bool = False
    status: str
    category: str = "help"
    accepted_answer_id: uuid.UUID | None = None
    answer_count: int = 0
    created_at: datetime.datetime
    author_name: str = ""  # 提问者昵称（service 组装，供列表/详情直接展示）


class AnswerCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class AcceptIn(BaseModel):
    """采纳某个回答。字段必填，缺键/拼错由 FastAPI 直接给 422（而非下游 404）。"""

    answer_id: uuid.UUID


class CloseIn(BaseModel):
    """关闭提问（可选地同时采纳一个回答）。"""

    accepted_answer_id: uuid.UUID | None = None


class AnswerOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_id: uuid.UUID
    author_id: uuid.UUID
    content: str
    is_accepted: bool
    created_at: datetime.datetime


class QuestionDetail(QuestionOut):
    answers: list[AnswerOut] = []
    images: list[str] = []
