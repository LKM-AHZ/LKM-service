from app.core.err import NS_BLOG, ErrCode, register


class BlogErr(ErrCode):
    SERIES_NOT_FOUND = NS_BLOG.err(1)
    COMMENT_NOT_FOUND = NS_BLOG.err(2)
    GIT_ERROR = NS_BLOG.err(3)
    FILE_NOT_FOUND = NS_BLOG.err(4)


register(
    {
        BlogErr.SERIES_NOT_FOUND: (404, "Blog series not found"),
        BlogErr.COMMENT_NOT_FOUND: (404, "Comment not found"),
        BlogErr.GIT_ERROR: (500, "Git operation failed"),
        BlogErr.FILE_NOT_FOUND: (404, "Blog file not found"),
    }
)
