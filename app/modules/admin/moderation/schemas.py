"""自动审校规则 CRUD 请求/响应模型。"""

from __future__ import annotations

import re
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _assert_compilable(pattern: str, is_regex: bool) -> None:
    """正则规则写入前先编译一次：引擎侧吞 re.error 返回 False，坏 pattern 会
    存成「启用但永不命中」的规则，管理员看到 0 命中却毫无提示。"""
    if not is_regex:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc


class RuleCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=255)
    is_regex: bool = False
    action: str = "derank"  # derank | hide
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    scope: str = "content"
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_regex(self) -> RuleCreate:
        _assert_compilable(self.pattern, self.is_regex)
        return self


class RuleUpdate(BaseModel):
    pattern: str | None = Field(default=None, min_length=1, max_length=255)
    is_regex: bool | None = None
    action: str | None = None
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    scope: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _validate_regex(self) -> RuleUpdate:
        # 只在本次同时给出 pattern 与 is_regex=True 时可判（局部更新时另一字段沿用旧值，
        # 归属 service 的合并后校验；此处至少挡住「自带 pattern 的正则」这一类）
        if self.pattern is not None:
            _assert_compilable(self.pattern, bool(self.is_regex))
        return self


class RuleInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pattern: str
    is_regex: bool
    action: str
    weight: float
    scope: str
    enabled: bool


class RuleTestRequest(BaseModel):
    """规则测试：输入一段文本，试跑当前启用的规则。"""

    text: str = Field(min_length=1, max_length=5000)


class RuleTestHit(BaseModel):
    """命中的单条规则明细（不含 id——测试走启用规则集，规则可静态配置）。"""

    pattern: str
    is_regex: bool
    action: str
    weight: float
    scope: str


class RuleTestResult(BaseModel):
    """规则测试结果：是否命中、累计 penalty、是否应隐藏、命中明细。"""

    matched: bool
    penalty: float
    should_hide: bool
    hits: list[RuleTestHit]
    total_rules: int
