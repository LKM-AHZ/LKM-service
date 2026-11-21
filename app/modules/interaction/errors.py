"""interaction 域错误码：内容交互 + 关注关系。

``FollowErr`` 原属 feed 域，随关注关系迁入本模块。**命名空间 ``NS_FOLLOW`` 保持不变**：
错误码数值是线上契约（前端/日志/告警按数值对账），不能因归属调整而重排。
"""

from core.err import (
    NS_FOLLOW,
    NS_INTERACTION,
    ErrCode,
    register,
    register_unique_constraint,
)


class InteractionErr(ErrCode):
    CONTENT_NOT_FOUND = NS_INTERACTION.err(1)
    DUPLICATE_LIKE = NS_INTERACTION.err(2)
    # block / mute（用户关系）
    CANNOT_RELATE_SELF = NS_INTERACTION.err(3)
    RELATION_TARGET_NOT_FOUND = NS_INTERACTION.err(4)
    BLOCKED = NS_INTERACTION.err(5)
    # 统一内容订阅：非法 target_type/目标格式，或目标（board/column）不存在
    INVALID_TARGET_TYPE = NS_INTERACTION.err(6)
    SUBSCRIPTION_TARGET_NOT_FOUND = NS_INTERACTION.err(7)
    # 好友关系（Stargate 兼容面）
    FRIEND_REQUEST_NOT_FOUND = NS_INTERACTION.err(8)
    NOT_FRIENDS = NS_INTERACTION.err(9)
    INVALID_RELATIONSHIP_STATUS = NS_INTERACTION.err(10)
    FRIEND_ALREADY_EXISTS = NS_INTERACTION.err(11)


class FollowErr(ErrCode):
    CANNOT_FOLLOW_SELF = NS_FOLLOW.err(1)
    TARGET_NOT_FOUND = NS_FOLLOW.err(2)
    # 重复关注（蓝图 §6.1）：uq_user_follows_pair / uq_board_follows_pair 撞键时由
    # app/db/session.py 映射到本码（并发双击关注、或服务层「先查后插」的竞态窗口）。
    DUPLICATE_FOLLOW = NS_FOLLOW.err(3)


register(
    {
        InteractionErr.CONTENT_NOT_FOUND: (404, "内容不存在"),
        InteractionErr.DUPLICATE_LIKE: (409, "Duplicate like"),
        InteractionErr.CANNOT_RELATE_SELF: (400, "不能对自己执行该操作"),
        InteractionErr.RELATION_TARGET_NOT_FOUND: (404, "目标用户不存在"),
        InteractionErr.BLOCKED: (403, "已被屏蔽"),
        InteractionErr.INVALID_TARGET_TYPE: (400, "非法订阅目标"),
        InteractionErr.SUBSCRIPTION_TARGET_NOT_FOUND: (404, "订阅目标不存在"),
        InteractionErr.FRIEND_REQUEST_NOT_FOUND: (404, "好友请求不存在"),
        InteractionErr.NOT_FRIENDS: (404, "对方不是你的好友"),
        InteractionErr.INVALID_RELATIONSHIP_STATUS: (400, "非法关系状态"),
        InteractionErr.FRIEND_ALREADY_EXISTS: (409, "已经是好友"),
        FollowErr.CANNOT_FOLLOW_SELF: (400, "不能关注自己"),
        FollowErr.TARGET_NOT_FOUND: (404, "关注目标不存在"),
        FollowErr.DUPLICATE_FOLLOW: (409, "已关注该目标"),
    }
)

# 唯一约束语义化（蓝图 §6.1）：content_likes 的复合唯一键/主键撞键时由
# app/db/session.py 映射到本码。片段取 ``like``（覆盖 content_likes_pkey 等隐式命名）。
register_unique_constraint("like", InteractionErr.DUPLICATE_LIKE)
# 关注关系两表（用户关注 / 板块关注）各一条复合唯一键，撞键即「已关注」。
register_unique_constraint("user_follows", FollowErr.DUPLICATE_FOLLOW)
register_unique_constraint("board_follows", FollowErr.DUPLICATE_FOLLOW)
# 好友配对唯一键：并发重复请求收敛为「已经是好友」。
register_unique_constraint(
    "friend_relationships", InteractionErr.FRIEND_ALREADY_EXISTS
)
