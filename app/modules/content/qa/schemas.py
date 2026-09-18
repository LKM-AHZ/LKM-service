import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class QuestionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    situation: str = Field(..., min_length=1, max_length=5000)
    content: str = Field(..., min_length=1, max_length=20000)
    category: str = Field(default="help", max_length=20)  # help|volunteer
    bounty_people: int = Field(..., ge=1, le=10)
    bounty_per_person: int = Field(..., ge=0)
    images: list[str] = Field(default_factory=list)  # 附件 URL/引用（后接真上传）


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
    status: str
    category: str = "help"
    accepted_answer_id: uuid.UUID | None = None
    answer_count: int = 0
    created_at: datetime.datetime
    author_name: str = ""  # 提问者昵称（service 组装，供列表/详情直接展示）


class AnswerCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


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
