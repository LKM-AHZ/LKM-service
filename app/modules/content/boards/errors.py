from core.err import NS_BOARDS, ErrCode, register


class BoardErr(ErrCode):
    BOARD_NOT_FOUND = NS_BOARDS.err(1)
    SLUG_CONFLICT = NS_BOARDS.err(2)
    APPLICATION_NOT_FOUND = NS_BOARDS.err(3)
    APPLICATION_ALREADY_REVIEWED = NS_BOARDS.err(4)
    NOT_BOARD_OWNER = NS_BOARDS.err(5)
    BOARD_BANNED = NS_BOARDS.err(6)
    BOARD_NOT_PUBLIC = NS_BOARDS.err(7)
    DAILY_POST_LIMIT_REACHED = NS_BOARDS.err(8)
    CERTIFICATION_REQUIRED = NS_BOARDS.err(9)
    ALREADY_BANNED = NS_BOARDS.err(10)


register(
    {
        BoardErr.BOARD_NOT_FOUND: (404, "板块不存在"),
        BoardErr.SLUG_CONFLICT: (409, "板块标识已被使用"),
        BoardErr.APPLICATION_NOT_FOUND: (404, "申请记录不存在"),
        BoardErr.APPLICATION_ALREADY_REVIEWED: (409, "申请已审核"),
        BoardErr.NOT_BOARD_OWNER: (403, "仅板块负责人可操作"),
        BoardErr.BOARD_BANNED: (403, "你已被本板块禁言"),
        BoardErr.BOARD_NOT_PUBLIC: (403, "本板块仅认证成员可发言"),
        BoardErr.DAILY_POST_LIMIT_REACHED: (429, "本板块今日发言已达上限"),
        BoardErr.CERTIFICATION_REQUIRED: (403, "需通过初级通识考试才能在本板块发言"),
        BoardErr.ALREADY_BANNED: (409, "该用户已被禁言"),
    }
)
