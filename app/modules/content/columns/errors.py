from core.err import NS_COLUMNS, ErrCode, register


class ColumnErr(ErrCode):
    APPLICATION_NOT_FOUND = NS_COLUMNS.err(1)
    NOT_FOUND = NS_COLUMNS.err(2)
    POST_NOT_FOUND = NS_COLUMNS.err(3)
    APPLICATION_ALREADY_REVIEWED = NS_COLUMNS.err(4)


register(
    {
        ColumnErr.APPLICATION_NOT_FOUND: (404, "Column application not found"),
        ColumnErr.NOT_FOUND: (404, "Column not found"),
        ColumnErr.POST_NOT_FOUND: (404, "Column post not found"),
        ColumnErr.APPLICATION_ALREADY_REVIEWED: (
            409,
            "Column application already reviewed",
        ),
    }
)
