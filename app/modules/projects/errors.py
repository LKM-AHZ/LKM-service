from core.err import NS_PROJECTS, ErrCode, register, register_unique_constraint


class ProjectErr(ErrCode):
    PROJECT_NOT_FOUND = NS_PROJECTS.err(1)
    APPLICATION_NOT_FOUND = NS_PROJECTS.err(2)
    APPLICATION_ALREADY_REVIEWED = NS_PROJECTS.err(3)
    DUPLICATE_APPLICATION = NS_PROJECTS.err(4)
    MEMBER_USER_NOT_FOUND = NS_PROJECTS.err(5)


register(
    {
        ProjectErr.PROJECT_NOT_FOUND: (404, "项目不存在"),
        ProjectErr.APPLICATION_NOT_FOUND: (404, "孵化申请不存在"),
        ProjectErr.APPLICATION_ALREADY_REVIEWED: (409, "该申请已审核"),
        ProjectErr.DUPLICATE_APPLICATION: (409, "你对本项目已有待审申请"),
        ProjectErr.MEMBER_USER_NOT_FOUND: (404, "申请中的成员账号不存在"),
    }
)

# 唯一约束语义化（蓝图 §6.1）：uq_project_applications_pending（部分唯一索引：同一项目下
# 同一申请人至多一条待审申请）撞键即「重复申请」——服务层已先查后拒，这里兜住并发竞态。
register_unique_constraint(
    "project_applications_pending", ProjectErr.DUPLICATE_APPLICATION
)
