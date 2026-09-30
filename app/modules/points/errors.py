from core.err import NS_POINTS, ErrCode, register


class PointsErr(ErrCode):
    INSUFFICIENT_BALANCE = NS_POINTS.err(1)
    DUPLICATE_REWARD = NS_POINTS.err(2)
    NO_BALANCE_RECORD = NS_POINTS.err(3)
    INVALID_PERIOD = NS_POINTS.err(4)


register(
    {
        PointsErr.INSUFFICIENT_BALANCE: (409, "积分余额不足"),
        PointsErr.DUPLICATE_REWARD: (409, "该事件积分已发放"),
        PointsErr.NO_BALANCE_RECORD: (404, "暂无积分记录"),
        PointsErr.INVALID_PERIOD: (400, "无效排名周期"),
    }
)
