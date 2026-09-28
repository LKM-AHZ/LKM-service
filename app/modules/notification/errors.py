from app.core.err import (
    NS_NOTIFICATION,
    ErrCode,
    register,
    register_unique_constraint,
)


class NotificationErr(ErrCode):
    NOTIFICATION_NOT_FOUND = NS_NOTIFICATION.err(1)
    INVALID_TYPE = NS_NOTIFICATION.err(2)
    # 设备推送令牌重复注册（蓝图 §6.1）：uq_notification_token 撞键时由 app/db/session.py
    # 映射到本码（同一 device token 被另一账号/并发重复登记）。
    DUPLICATE_TOKEN = NS_NOTIFICATION.err(3)


register(
    {
        NotificationErr.NOTIFICATION_NOT_FOUND: (404, "通知不存在"),
        NotificationErr.INVALID_TYPE: (422, "未知的通知类型"),
        NotificationErr.DUPLICATE_TOKEN: (409, "该设备令牌已注册"),
    }
)

register_unique_constraint("notification_token", NotificationErr.DUPLICATE_TOKEN)
