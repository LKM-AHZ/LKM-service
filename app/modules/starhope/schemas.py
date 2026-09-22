import datetime
import json
import uuid
from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, field_validator


def _parse_json_text(v: object, default: Any) -> Any:
    """把 Text 列里的 JSON 字符串解析回 Python 对象；非法/空则回退 default。"""
    if v is None:
        return default
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return default


def _parse_list(v: object, default: list[str]) -> list[str]:
    parsed = _parse_json_text(v, default)
    if isinstance(parsed, list):
        return cast(list[str], parsed)
    return default


def _parse_dict(v: object, default: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_json_text(v, default)
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return default


class _Out(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class StarHopeQuestionOut(_Out):
    id: str
    user_id: uuid.UUID
    type: str
    content: str
    options: list[str] | None = None
    answer: str | list[str]
    analysis: str | None = None
    tags: list[str]
    folder_id: str | None = None
    difficulty: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v: object) -> list[str] | None:
        if v is None:
            return None
        return _parse_list(v, [])

    @field_validator("answer", mode="before")
    @classmethod
    def _answer(cls, v: object) -> str | list[str]:
        # 用 None 作哨兵，区分「非 JSON 文本（解析失败）」与「JSON 字符串字面量」：
        # 若默认值用 ""，纯文本答案（"A"/"对"）会被解析失败的默认值吞成空串，答案丢失。
        parsed = _parse_json_text(v, None)
        if isinstance(parsed, list):
            return cast(list[str], parsed)
        if isinstance(parsed, str):
            return cast(str, parsed)
        # 标量答案本身是纯文本（如 "2"），但与 JSON 数字同形，json.loads 解析成了 int/float；
        # 此时应保留原始字符串，避免 Answer 被错误置空。
        if isinstance(v, str):
            return v
        return ""

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: object) -> list[str]:
        return _parse_list(v, [])


class StarHopeQuestionIn(BaseModel):
    id: str
    type: str
    content: str
    options: list[str] | None = None
    answer: str | list[str]
    analysis: str | None = None
    tags: list[str]
    folder_id: str | None = None
    difficulty: int
    updated_at: datetime.datetime


class StarHopeFolderOut(_Out):
    id: str
    user_id: uuid.UUID
    name: str
    parent_id: str | None = None
    sort: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class StarHopeFolderIn(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    sort: int
    updated_at: datetime.datetime


class StarHopeSessionOut(_Out):
    id: str
    user_id: uuid.UUID
    type: str
    mode: str
    question_ids: list[str]
    answers: dict[str, str | list[str]]
    results: dict[str, dict[str, Any]] | None = None
    status: str
    started_at: datetime.datetime
    completed_at: datetime.datetime | None = None
    time_limit: int | None = None
    passing_grade: int | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_validator("question_ids", mode="before")
    @classmethod
    def _question_ids(cls, v: object) -> list[str]:
        return _parse_list(v, [])

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, v: object) -> dict[str, str | list[str]]:
        return cast(dict[str, str | list[str]], _parse_dict(v, {}))

    @field_validator("results", mode="before")
    @classmethod
    def _results(cls, v: object) -> dict[str, dict[str, Any]] | None:
        if v is None:
            return None
        return cast(dict[str, dict[str, Any]], _parse_dict(v, {}))


class StarHopeSessionIn(BaseModel):
    id: str
    type: str
    mode: str
    question_ids: list[str]
    answers: dict[str, str | list[str]]
    results: dict[str, dict[str, Any]] | None = None
    status: str
    started_at: datetime.datetime
    completed_at: datetime.datetime | None = None
    time_limit: int | None = None
    passing_grade: int | None = None
    updated_at: datetime.datetime


class StarHopeAgentOut(_Out):
    id: str
    user_id: uuid.UUID
    name: str
    avatar: str | None = None
    system_prompt: str
    service: str
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class StarHopeAgentIn(BaseModel):
    id: str
    name: str
    avatar: str | None = None
    system_prompt: str
    service: str
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    updated_at: datetime.datetime


class StarHopeTombstone(BaseModel):
    id: str
    deleted_at: datetime.datetime


class StarHopePullData[T](BaseModel):
    items: list[T]
    tombstones: list[StarHopeTombstone]
    server_time: datetime.datetime


class StarHopePushData(BaseModel):
    upserts: list[dict[str, Any]]
    deletes: list[StarHopeTombstone]


class StarHopePushResult(BaseModel):
    synced: int
    server_time: datetime.datetime
