"""评论域的写入口入参。

读出口的 ``ContentCommentInfo`` 放在 ``app/contracts/comment.py``：content 的 GraphQL
也要用它组装评论树，属两侧共用的 DTO，不该由任一业务模块持有。
"""

import uuid

from pydantic import BaseModel, Field


class ContentCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: uuid.UUID | None = Field(default=None)
