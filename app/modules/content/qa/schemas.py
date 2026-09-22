import datetime
import uuid
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    situation: str = Field(..., min_length=1, max_length=5000)
    content: str = Field(..., min_length=1, max_length=20000)
    # 取值域与 models.QAQuestion.category 注释、前端 QaCategory = "help" | "volunteer" 一致：
    # 原先只限长度，任意串都能落库，而列表按 category 逐字过滤 → 脏值会把 tab 内容切碎。
    category: Literal["help", "volunteer"] = "help"
    bounty_people: int = Field(..., ge=1, le=10)
    bounty_per_person: int = Field(..., ge=0)
    # 附件 URL/引用（后接真上传）：每条一行落库，故逐条限长 + 限条数，
    # 否则单请求可推入任意多条任意长的 URL（不可控写放大）
    images: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list, max_length=9
    )


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
