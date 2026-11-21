from core.err import NS_CALENDAR, ErrCode, register


class CalendarErr(ErrCode):
    EVENT_NOT_FOUND = NS_CALENDAR.err(1)
    NOT_EVENT_OWNER = NS_CALENDAR.err(2)
    INVALID_RECURRENCE = NS_CALENDAR.err(3)
    INVALID_TIME_RANGE = NS_CALENDAR.err(4)
    INVALID_VISIBILITY = NS_CALENDAR.err(5)


register(
    {
        CalendarErr.EVENT_NOT_FOUND: (404, "日历事件不存在或不可见"),
        CalendarErr.NOT_EVENT_OWNER: (403, "只有事件创建者可以修改该事件"),
        CalendarErr.INVALID_RECURRENCE: (422, "无效的重复规则"),
        CalendarErr.INVALID_TIME_RANGE: (422, "结束时间必须晚于开始时间"),
        CalendarErr.INVALID_VISIBILITY: (422, "无效的可见性级别"),
    }
)
