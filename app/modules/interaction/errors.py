"""interaction 域错误码：内容交互 + 关注关系。

``FollowErr`` 原属 feed 域，随关注关系迁入本模块。**命名空间 ``NS_FOLLOW`` 保持不变**：
错误码数值是线上契约（前端/日志/告警按数值对账），不能因归属调整而重排。
"""

from app.core.err import NS_FOLLOW, NS_INTERACTION, ErrCode, register


class InteractionErr(ErrCode):
    CONTENT_NOT_FOUND = NS_INTERACTION.err(1)


class FollowErr(ErrCode):
    CANNOT_FOLLOW_SELF = NS_FOLLOW.err(1)
    TARGET_NOT_FOUND = NS_FOLLOW.err(2)


register(
    {
        InteractionErr.CONTENT_NOT_FOUND: (404, "内容不存在"),
        FollowErr.CANNOT_FOLLOW_SELF: (400, "不能关注自己"),
        FollowErr.TARGET_NOT_FOUND: (404, "关注目标不存在"),
    }
)
