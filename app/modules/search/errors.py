from app.core.err import NS_SEARCH, ErrCode, register


class SearchErr(ErrCode):
    EMPTY_QUERY = NS_SEARCH.err(1)


register(
    {
        SearchErr.EMPTY_QUERY: (422, "Search query must not be blank"),
    }
)
