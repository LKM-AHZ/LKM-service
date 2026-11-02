"""评论 DTO（content 与 comment 两个业务模块共用）。

``ContentCommentInfo`` 原先定义在 ``content/schemas.py``：步骤 9 切片 2 把评论拆成独立
模块后，评论的读口归 comment 域，但 content 的 GraphQL 仍要用它组装评论树——两侧都要
这个形状，于是上移到共享内核（与 ``ContentItemInfo`` 同一处置）。
"""

from __future__ import annotations

import datetime
import uuid
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ContentCommentInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    author_id: uuid.UUID = Field(validation_alias="user_id")
    author_name: str = ""
    content: str
    floor_number: int
    parent_id: uuid.UUID | None = None
    like_count: int
    # 当前读口所属用户是否点过赞；未登录/未指定 viewer 时恒 False
    liked: bool = False
    created_at: datetime.datetime


__all__ = ["ContentCommentInfo"]
