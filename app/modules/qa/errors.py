from core.err import NS_QA, ErrCode, register


class QaErr(ErrCode):
    QUESTION_NOT_FOUND = NS_QA.err(1)
    ANSWER_NOT_FOUND = NS_QA.err(2)
    QUESTION_NOT_OPEN = NS_QA.err(3)
    NOT_ASKER = NS_QA.err(4)
    BOUNTY_EXHAUSTED = NS_QA.err(5)
    CERTIFICATION_REQUIRED = NS_QA.err(6)
    SELF_ACCEPT_FORBIDDEN = NS_QA.err(7)
    BOUNTY_EXPIRED = NS_QA.err(8)
    SELF_ANSWER_FORBIDDEN = NS_QA.err(9)


register(
    {
        QaErr.QUESTION_NOT_FOUND: (404, "问题不存在"),
        QaErr.ANSWER_NOT_FOUND: (404, "回答不存在"),
        QaErr.QUESTION_NOT_OPEN: (409, "问题已关闭，不可操作"),
        QaErr.NOT_ASKER: (403, "仅提问者本人可操作"),
        QaErr.BOUNTY_EXHAUSTED: (409, "悬赏已派发完毕"),
        QaErr.CERTIFICATION_REQUIRED: (403, "需认证用户才可提问或回答"),
        QaErr.SELF_ACCEPT_FORBIDDEN: (403, "不能采纳自己的回答"),
        QaErr.BOUNTY_EXPIRED: (409, "悬赏已到期，等待自动退还"),
        QaErr.SELF_ANSWER_FORBIDDEN: (403, "不能回答自己的问题"),
    }
)
