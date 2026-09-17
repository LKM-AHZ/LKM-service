from app.core.err import NS_NOTIFICATION, ErrCode, register


class NotificationErr(ErrCode):
    NOTIFICATION_NOT_FOUND = NS_NOTIFICATION.err(1)
    INVALID_TYPE = NS_NOTIFICATION.err(2)


register(
    {
        NotificationErr.NOTIFICATION_NOT_FOUND: (404, "通知不存在"),
        NotificationErr.INVALID_TYPE: (422, "未知的通知类型"),
    }
)
