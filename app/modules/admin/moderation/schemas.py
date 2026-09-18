"""自动审校规则 CRUD 请求/响应模型。"""

import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class RuleCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=255)
    is_regex: bool = False
    action: str = "derank"  # derank | hide
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    scope: str = "content"
    enabled: bool = True


class RuleUpdate(BaseModel):
    pattern: str | None = Field(default=None, min_length=1, max_length=255)
    is_regex: bool | None = None
    action: str | None = None
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    scope: str | None = None
    enabled: bool | None = None


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
