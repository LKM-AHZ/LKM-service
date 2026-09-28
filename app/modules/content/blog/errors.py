from app.core.err import NS_BLOG, ErrCode, register, register_unique_constraint


class BlogErr(ErrCode):
    SERIES_NOT_FOUND = NS_BLOG.err(1)
    COMMENT_NOT_FOUND = NS_BLOG.err(2)
    GIT_ERROR = NS_BLOG.err(3)
    FILE_NOT_FOUND = NS_BLOG.err(4)
    # 仓库名重复（蓝图 §6.1）：blog_series.repo_name 唯一（撞键时由 app/db/session.py 映射）。
    REPO_NAME_TAKEN = NS_BLOG.err(5)


register(
    {
        BlogErr.SERIES_NOT_FOUND: (404, "Blog series not found"),
        BlogErr.COMMENT_NOT_FOUND: (404, "Comment not found"),
        BlogErr.GIT_ERROR: (500, "Git operation failed"),
        BlogErr.FILE_NOT_FOUND: (404, "Blog file not found"),
        BlogErr.REPO_NAME_TAKEN: (409, "Repository name already taken"),
    }
)

# 唯一约束语义化（蓝图 §6.1）：片段 ``repo_name`` 同时覆盖 blog_series 与
# blog_repo_quarantine 两处唯一键（后者的冲突也是「该仓库名已被占用」）。
register_unique_constraint("repo_name", BlogErr.REPO_NAME_TAKEN)
