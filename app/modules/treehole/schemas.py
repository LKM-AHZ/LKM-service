from typing import Literal

from pydantic import BaseModel, Field


class LetterInput(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    category: str = Field(min_length=1, max_length=32)
    privacy: Literal["public", "self", "random"] = "public"
    codename: str = Field(min_length=1, max_length=80)
    moods: list[str] = Field(default_factory=list, max_length=3)
    tags: list[str] = Field(default_factory=list, max_length=3)
    sticker: str = Field(default="", max_length=2000)
    paper: str = Field(default="paper", max_length=32)
    scheduledAt: int | None = None
    sealUntil: int | None = None


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ReportInput(BaseModel):
    targetType: Literal["letter", "bottle", "wish"]
    targetId: str
    reason: str = Field(min_length=1, max_length=80)
    detail: str = Field(default="", max_length=1000)
