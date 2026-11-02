"""评论（comment）域错误码。

**沿用 content 命名空间与编号**（``NS_CONTENT`` 17.2）：错误码是 API 契约的一部分，
`errcode` 数值落在响应体里，拆域不得改号。故本模块定义一个与 content 并列的
``CommentErr``，只把「评论不存在」这一条从 ``ContentErr`` 接过来（content 侧同步移除，
否则同一码会重复 register）。
"""

from core.err import NS_CONTENT, ErrCode, register


class CommentErr(ErrCode):
    COMMENT_NOT_FOUND = NS_CONTENT.err(2)


register(
    {
        CommentErr.COMMENT_NOT_FOUND: (404, "Content comment not found"),
    }
)
