"""自动审校规则错误码。"""

from core.err import NS_MODERATION, ErrCode, register


class ModerationErr(ErrCode):
    RULE_NOT_FOUND = NS_MODERATION.err(1)
    INVALID_ACTION = NS_MODERATION.err(2)
    INVALID_SCOPE = NS_MODERATION.err(3)


register(
    {
        ModerationErr.RULE_NOT_FOUND: (404, "审校规则不存在"),
        ModerationErr.INVALID_ACTION: (422, "无效规则动作"),
        ModerationErr.INVALID_SCOPE: (422, "无效规则范围"),
    }
)
