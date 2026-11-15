from core.err import NS_PROJECTS, ErrCode, register, register_unique_constraint


class ProjectErr(ErrCode):
    PROJECT_NOT_FOUND = NS_PROJECTS.err(1)
    APPLICATION_NOT_FOUND = NS_PROJECTS.err(2)
    APPLICATION_ALREADY_REVIEWED = NS_PROJECTS.err(3)
    DUPLICATE_APPLICATION = NS_PROJECTS.err(4)
    MEMBER_USER_NOT_FOUND = NS_PROJECTS.err(5)
    # ———————— 项目协作：角色 / 邀请 / 加入申请（从 6 起） ————————
    FORBIDDEN = NS_PROJECTS.err(6)
    MEMBER_NOT_FOUND = NS_PROJECTS.err(7)
    ALREADY_MEMBER = NS_PROJECTS.err(8)
    DUPLICATE_INVITE = NS_PROJECTS.err(9)
    INVITE_NOT_FOUND = NS_PROJECTS.err(10)
    INVITE_NOT_PENDING = NS_PROJECTS.err(11)
    DUPLICATE_JOIN_REQUEST = NS_PROJECTS.err(12)
    JOIN_REQUEST_NOT_FOUND = NS_PROJECTS.err(13)
    JOIN_REQUEST_ALREADY_REVIEWED = NS_PROJECTS.err(14)
    CANNOT_REMOVE_OWNER = NS_PROJECTS.err(15)
    OWNER_CANNOT_LEAVE = NS_PROJECTS.err(16)
    INVALID_ROLE = NS_PROJECTS.err(17)


register(
    {
        ProjectErr.PROJECT_NOT_FOUND: (404, "项目不存在"),
        ProjectErr.APPLICATION_NOT_FOUND: (404, "孵化申请不存在"),
        ProjectErr.APPLICATION_ALREADY_REVIEWED: (409, "该申请已审核"),
        ProjectErr.DUPLICATE_APPLICATION: (409, "你对本项目已有待审申请"),
        ProjectErr.MEMBER_USER_NOT_FOUND: (404, "申请中的成员账号不存在"),
        ProjectErr.FORBIDDEN: (403, "无权操作该项目"),
        ProjectErr.MEMBER_NOT_FOUND: (404, "项目成员不存在"),
        ProjectErr.ALREADY_MEMBER: (409, "该用户已是项目成员"),
        ProjectErr.DUPLICATE_INVITE: (409, "该用户已有待接受的项目邀请"),
        ProjectErr.INVITE_NOT_FOUND: (404, "项目邀请不存在"),
        ProjectErr.INVITE_NOT_PENDING: (409, "该邀请已处理"),
        ProjectErr.DUPLICATE_JOIN_REQUEST: (409, "你对本项目已有待审的加入申请"),
        ProjectErr.JOIN_REQUEST_NOT_FOUND: (404, "加入申请不存在"),
        ProjectErr.JOIN_REQUEST_ALREADY_REVIEWED: (409, "该加入申请已审核"),
        ProjectErr.CANNOT_REMOVE_OWNER: (409, "不能移除项目负责人"),
        ProjectErr.OWNER_CANNOT_LEAVE: (409, "负责人不能直接退出，请先转让或归档"),
        ProjectErr.INVALID_ROLE: (422, "无效的项目角色"),
    }
)

# 唯一约束语义化（蓝图 §6.1）：服务层已先查后拒，这里兜住并发竞态。
register_unique_constraint(
    "project_applications_pending", ProjectErr.DUPLICATE_APPLICATION
)
register_unique_constraint("uq_project_invites_pending", ProjectErr.DUPLICATE_INVITE)
register_unique_constraint(
    "uq_project_join_requests_pending", ProjectErr.DUPLICATE_JOIN_REQUEST
)
register_unique_constraint(
    "uq_project_members_active_user", ProjectErr.ALREADY_MEMBER
)
