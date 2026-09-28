"""interaction 域错误码：内容交互 + 关注关系。

``FollowErr`` 原属 feed 域，随关注关系迁入本模块。**命名空间 ``NS_FOLLOW`` 保持不变**：
错误码数值是线上契约（前端/日志/告警按数值对账），不能因归属调整而重排。
"""

from app.core.err import (
    NS_FOLLOW,
    NS_INTERACTION,
    ErrCode,
    register,
    register_unique_constraint,
)


class InteractionErr(ErrCode):
    CONTENT_NOT_FOUND = NS_INTERACTION.err(1)
    # 重复点赞（蓝图 §6.1 唯一约束语义化）：content_likes 复合主键保证「同一用户对同一
    # 内容最多一条」，并发重复点赞撞主键时由 app/db/session.py 映射到本码。
    DUPLICATE_LIKE = NS_INTERACTION.err(2)


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
