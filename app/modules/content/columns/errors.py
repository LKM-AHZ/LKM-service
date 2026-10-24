from core.err import (
    NS_COLUMNS,
    ErrCode,
    register,
    register_unique_constraint,
)


class ColumnErr(ErrCode):
    APPLICATION_NOT_FOUND = NS_COLUMNS.err(1)
    NOT_FOUND = NS_COLUMNS.err(2)
    POST_NOT_FOUND = NS_COLUMNS.err(3)
    APPLICATION_ALREADY_REVIEWED = NS_COLUMNS.err(4)
    APPLICATION_PENDING_EXISTS = NS_COLUMNS.err(5)
    APPLICATION_COOLDOWN = NS_COLUMNS.err(6)


register(
    {
        ColumnErr.APPLICATION_NOT_FOUND: (404, "Column application not found"),
        ColumnErr.NOT_FOUND: (404, "Column not found"),
        ColumnErr.POST_NOT_FOUND: (404, "Column post not found"),
        ColumnErr.APPLICATION_ALREADY_REVIEWED: (
            409,
            "Column application already reviewed",
        ),
        ColumnErr.APPLICATION_PENDING_EXISTS: (
            409,
            "Column application already pending review",
        ),
        ColumnErr.APPLICATION_COOLDOWN: (
            429,
            "Column application rejected recently; retry after cooldown",
        ),
    }
)

# 部分唯一索引（同一用户仅一条 pending）：并发下撞索引时由 get_session 翻译成 409。
register_unique_constraint(
    "uq_column_applications_pending_user", ColumnErr.APPLICATION_PENDING_EXISTS
)
